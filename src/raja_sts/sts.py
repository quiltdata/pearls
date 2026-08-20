"""Mint temporary Scene credentials with AWS STS AssumeRole."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import boto3

from .errors import InvalidAssumeRoleRequest, MintError
from .manifest import ManifestLoader
from .models import CompiledPolicy, ScopedCredentials
from .policy import DEFAULT_MAX_POLICY_CHARACTERS, compile

_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+$")
_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_+=,.@-]{2,64}$")


def validate_assume_role_request(
    *,
    role_arn: str,
    scene_id: str,
    duration_seconds: int = 3600,
    external_id: str | None = None,
) -> None:
    """Check every value that would be sent to ``sts:AssumeRole``.

    Raises :class:`InvalidAssumeRoleRequest` without contacting AWS, so callers
    can validate an operator's arguments before any network call.
    """
    if not isinstance(role_arn, str) or not _ROLE_ARN_RE.fullmatch(role_arn):
        raise InvalidAssumeRoleRequest("role_arn must be a complete IAM role ARN")
    if not isinstance(scene_id, str) or not _SESSION_NAME_RE.fullmatch(scene_id):
        raise InvalidAssumeRoleRequest(
            "scene_id must be 2-64 AWS RoleSessionName characters: A-Z a-z 0-9 _ + = , . @ -"
        )
    if not isinstance(duration_seconds, int) or not 900 <= duration_seconds <= 43200:
        raise InvalidAssumeRoleRequest("duration_seconds must be between 900 and 43200")
    if external_id is not None and (
        not isinstance(external_id, str) or not 2 <= len(external_id) <= 1224
    ):
        raise InvalidAssumeRoleRequest("external_id must contain 2-1224 characters")


def mint_compiled(
    compilation: CompiledPolicy,
    *,
    role_arn: str,
    scene_id: str,
    duration_seconds: int = 3600,
    external_id: str | None = None,
    sts_client: Any | None = None,
) -> ScopedCredentials:
    """Attach a compiled policy to one STS role session and return its credentials."""
    validate_assume_role_request(
        role_arn=role_arn,
        scene_id=scene_id,
        duration_seconds=duration_seconds,
        external_id=external_id,
    )

    request: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": scene_id,
        "Policy": compilation.policy_json,
        "DurationSeconds": duration_seconds,
    }
    if external_id is not None:
        request["ExternalId"] = external_id

    client = sts_client or boto3.client("sts")
    try:
        response = client.assume_role(**request)
        credentials = response["Credentials"]
        assumed_role = response["AssumedRoleUser"]
        expiration = credentials["Expiration"]
        if not isinstance(expiration, datetime):
            raise TypeError("Credentials.Expiration is not a datetime")
        return ScopedCredentials(
            access_key_id=credentials["AccessKeyId"],
            secret_access_key=credentials["SecretAccessKey"],
            session_token=credentials["SessionToken"],
            expiration=expiration,
            assumed_role_arn=assumed_role["Arn"],
            assumed_role_id=assumed_role["AssumedRoleId"],
            packed_policy_size=int(response.get("PackedPolicySize", 0)),
            compilation=compilation,
        )
    except Exception as error:
        raise MintError(f"Unable to mint scoped STS credentials: {error}") from error


def mint(
    manifest_uri: str,
    scope_spec: Mapping[str, object],
    *,
    role_arn: str,
    scene_id: str,
    duration_seconds: int = 3600,
    external_id: str | None = None,
    manifest_loader: ManifestLoader | None = None,
    sts_client: Any | None = None,
    max_policy_characters: int = DEFAULT_MAX_POLICY_CHARACTERS,
) -> ScopedCredentials:
    """Compile a sealed Scene scope and mint its temporary AWS credentials."""
    compilation = compile(
        manifest_uri,
        scope_spec,
        manifest_loader=manifest_loader,
        max_policy_characters=max_policy_characters,
    )
    return mint_compiled(
        compilation,
        role_arn=role_arn,
        scene_id=scene_id,
        duration_seconds=duration_seconds,
        external_id=external_id,
        sts_client=sts_client,
    )
