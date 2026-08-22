"""Compile logical Scene scopes into deterministic AWS session policies."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from os.path import commonprefix
from typing import Any

from .bedrock import BedrockInvocation, BedrockProfileLoader, parse_bedrock_invocation
from .bedrock import bedrock_model_account as bedrock_model_account
from .bedrock import bedrock_model_region as bedrock_model_region
from .bedrock import parse_bedrock_model as parse_bedrock_model
from .errors import InvalidScopeError, PolicySizeError
from .manifest import (
    ManifestLoader,
    load_quilt_manifest,
    parse_manifest_uri,
    parse_output_package,
    parse_output_selectors,
)
from .models import CompiledPolicy, ManifestObject, OutputPackage, OutputSelector

DEFAULT_MAX_POLICY_CHARACTERS = 2048
_OUTPUT_REVISION_ID_RE = re.compile(r"^[A-Za-z0-9_+=,.@-]{2,64}$")
_ALLOWED_SCOPE_KEYS = {
    "readable",
    "writable_package",
    "writable",
    "bedrock_model",
    "bedrock_invoke_resources",
    "require_version_ids",
}
_RUNTIME_OUTPUT_KEYS = frozenset({"run/scene.json", "run/invocation.json"})


def _parse_readable(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise InvalidScopeError("readable must be a non-empty sequence of logical keys or prefixes")

    selectors: list[str] = []
    for selector in value:
        if not isinstance(selector, str) or not selector:
            raise InvalidScopeError("each readable selector must be a non-empty string")
        if selector.startswith("/") or "\\" in selector or "*" in selector or "?" in selector:
            raise InvalidScopeError(f"unsafe readable selector: {selector!r}")
        if any(part in {"", ".", ".."} for part in selector.rstrip("/").split("/")):
            raise InvalidScopeError(f"unsafe readable selector: {selector!r}")
        selectors.append(selector)
    return tuple(dict.fromkeys(selectors))


def _parse_boolean(scope: Mapping[str, object], key: str) -> bool:
    value = scope.get(key, False)
    if not isinstance(value, bool):
        raise InvalidScopeError(f"{key} must be a boolean")
    return value


def _parse_output_scope(
    scope: Mapping[str, object],
) -> tuple[OutputPackage, tuple[OutputSelector, ...]]:
    has_legacy = "writable_package" in scope
    has_selectors = "writable" in scope
    if has_legacy == has_selectors:
        raise InvalidScopeError("scope must contain exactly one of writable_package or writable")
    if has_legacy:
        return parse_output_package(scope["writable_package"]), ()

    selectors = parse_output_selectors(scope["writable"])
    first = selectors[0]
    data_resources = [selector.data_resource for selector in selectors]
    if not any(selector.kind == "package" for selector in selectors):
        root = f"arn:aws:s3:::{first.bucket}/{first.package_name}/"
        data_resources.extend(f"{root}{key}" for key in sorted(_RUNTIME_OUTPUT_KEYS))
    output = OutputPackage(
        name=first.package_name,
        bucket=first.bucket,
        data_write_resources=tuple(_deduplicate(data_resources)),
    )
    return output, selectors


def _resolve_selectors(
    selectors: tuple[str, ...], objects: Sequence[ManifestObject]
) -> tuple[ManifestObject, ...]:
    by_logical_key = {item.logical_key: item for item in objects}
    selected: dict[str, ManifestObject] = {}
    for selector in selectors:
        matches: list[ManifestObject]
        if not selector.endswith("/") and selector in by_logical_key:
            matches = [by_logical_key[selector]]
        else:
            prefix = selector if selector.endswith("/") else f"{selector}/"
            matches = [item for item in objects if item.logical_key.startswith(prefix)]
        if not matches:
            raise InvalidScopeError(f"readable selector matched no manifest object: {selector!r}")
        selected.update((item.logical_key, item) for item in matches)
    return tuple(selected[key] for key in sorted(selected))


def _deduplicate(resources: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(resources))


def _output_versioning_statement(output: OutputPackage) -> dict[str, Any]:
    return {
        "Effect": "Allow",
        "Action": "s3:GetBucketVersioning",
        "Resource": output.bucket_arn,
    }


def _bedrock_statements(invocation: BedrockInvocation | None) -> list[dict[str, Any]]:
    """Pin one model ID and condition profile supports on that exact profile."""
    if invocation is None:
        return []
    if invocation.selected.kind == "foundation-model":
        return [
            {
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": invocation.selected_model_id,
            }
        ]

    supporting: str | list[str]
    if len(invocation.supporting_foundation_models) == 1:
        supporting = invocation.supporting_foundation_models[0]
    else:
        supporting = list(invocation.supporting_foundation_models)
    return [
        {
            "Effect": "Allow",
            "Action": "bedrock:InvokeModel",
            "Resource": invocation.selected_model_id,
        },
        {
            "Effect": "Allow",
            "Action": "bedrock:InvokeModel",
            "Resource": supporting,
            "Condition": {
                "StringLike": {
                    "bedrock:InferenceProfileArn": invocation.selected_model_id,
                }
            },
        },
    ]


def _directory_prefix(keys: Sequence[str]) -> str:
    """Return a common S3 directory prefix, never a partial path component."""
    shared = commonprefix(list(keys))
    if "/" not in shared:
        return ""
    return shared[: shared.rfind("/") + 1]


def _read_prefix_resources(objects: Sequence[ManifestObject]) -> tuple[str, ...]:
    """Return one bounded physical prefix per selected source registry."""
    by_bucket: dict[str, list[str]] = defaultdict(list)
    for item in objects:
        by_bucket[item.bucket].append(item.key)

    resources: list[str] = []
    for bucket in sorted(by_bucket):
        prefix = _directory_prefix(sorted(by_bucket[bucket]))
        if not prefix:
            raise InvalidScopeError(
                f"selected objects in source bucket {bucket!r} have no bounded common directory "
                "prefix; refusing bucket-root read authority"
            )
        if prefix.startswith(".quilt/"):
            raise InvalidScopeError(
                f"selected objects in source bucket {bucket!r} require common prefix "
                f"{prefix!r}, which overlaps the reserved .quilt/ metadata namespace"
            )
        resources.append(f"arn:aws:s3:::{bucket}/{prefix}*")
    return tuple(resources)


def _read_statements(
    prefix_resources: Sequence[str], metadata_resources: Sequence[str]
) -> list[dict[str, Any]]:
    """Allow versioned data reads and exact current Quilt metadata reads."""
    return [
        {
            "Effect": "Allow",
            "Action": "s3:GetObjectVersion",
            "Resource": list(prefix_resources),
        },
        {
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": _deduplicate(metadata_resources),
        },
        {
            "Effect": "Deny",
            "Action": ["s3:DeleteObject*", "s3:ListBucket*"],
            "Resource": "*",
        },
    ]


def _write_statement(output: OutputPackage) -> dict[str, Any]:
    """Allow writes only to the exact package and Quilt publication resources."""
    return {
        "Effect": "Allow",
        "Action": "s3:PutObject",
        "Resource": list(output.write_resources),
    }


def _serialize(statements: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
    document = {"Version": "2012-10-17", "Statement": list(statements)}
    return document, json.dumps(document, separators=(",", ":"), sort_keys=True)


def compile(
    manifest_uri: str,
    scope_spec: Mapping[str, object],
    *,
    manifest_loader: ManifestLoader | None = None,
    max_policy_characters: int = DEFAULT_MAX_POLICY_CHARACTERS,
    include_manifest_read: bool = True,
    bedrock_profile_loader: BedrockProfileLoader | None = None,
    output_revision_id: str | None = None,
) -> CompiledPolicy:
    """Compile a pinned Quilt manifest and logical scope into an STS policy.

    Selected data is authorized by one physical S3 prefix per source registry,
    independent of object count. The runner requests every selected object by its
    exact VersionId and verifies its declared Quilt hash before exposing bytes to
    the handler. ``include_manifest_read=False`` is reserved for runners that
    already resolved the manifest and carry every selected descriptor in memory.
    """
    if not isinstance(scope_spec, Mapping):
        raise InvalidScopeError("scope_spec must be a mapping")
    unknown = set(scope_spec) - _ALLOWED_SCOPE_KEYS
    if unknown:
        raise InvalidScopeError(f"unknown scope fields: {', '.join(sorted(unknown))}")
    if (
        not isinstance(max_policy_characters, int)
        or not 0 < max_policy_characters <= DEFAULT_MAX_POLICY_CHARACTERS
    ):
        raise ValueError(
            f"max_policy_characters must be between 1 and {DEFAULT_MAX_POLICY_CHARACTERS}"
        )
    if not isinstance(include_manifest_read, bool):
        raise ValueError("include_manifest_read must be a boolean")
    if output_revision_id is not None and (
        not isinstance(output_revision_id, str)
        or not _OUTPUT_REVISION_ID_RE.fullmatch(output_revision_id)
        or output_revision_id == "latest"
    ):
        raise InvalidScopeError(
            "output_revision_id must be 2-64 safe Scene ID characters and not 'latest'"
        )

    manifest = parse_manifest_uri(manifest_uri)
    selectors = _parse_readable(scope_spec.get("readable"))
    output, writable_selectors = _parse_output_scope(scope_spec)
    if output_revision_id is not None:
        output = replace(output, revision_id=output_revision_id)
    bedrock = parse_bedrock_invocation(
        scope_spec.get("bedrock_model"),
        scope_spec.get("bedrock_invoke_resources"),
        profile_loader=bedrock_profile_loader,
    )
    require_version_ids = _parse_boolean(scope_spec, "require_version_ids")

    loader = manifest_loader or load_quilt_manifest
    selected = _resolve_selectors(selectors, tuple(loader(manifest)))
    missing_versions = [item.logical_key for item in selected if item.version_id is None]
    if require_version_ids and missing_versions:
        raise InvalidScopeError(
            "selected objects have no S3 version ID: " + ", ".join(missing_versions)
        )

    read_prefixes = _read_prefix_resources(selected)
    metadata_read_resources = list(output.read_resources)
    if include_manifest_read:
        metadata_read_resources.insert(0, manifest.manifest_arn)
    model_statements = _bedrock_statements(bedrock)
    document, policy_json = _serialize(
        [
            *_read_statements(read_prefixes, metadata_read_resources),
            _output_versioning_statement(output),
            *model_statements,
            _write_statement(output),
        ]
    )
    if len(policy_json) > max_policy_characters:
        raise PolicySizeError(
            f"inline session policy is {len(policy_json)} characters; "
            f"plaintext limit is {max_policy_characters}"
        )

    warnings: tuple[str, ...] = (
        "Prefix-scoped credentials can read objects under the reported source prefixes "
        "that are not selected by the manifest scope.",
        "Selected input identity must be enforced by exact VersionId requests and Quilt hash "
        "verification in a runner; IAM does not enforce it.",
    )
    if missing_versions:
        warnings += (
            "Some selected objects have no S3 version ID; a Scene runner will refuse them.",
        )

    return CompiledPolicy(
        document=document,
        policy_json=policy_json,
        mode="prefix-scoped",
        manifest=manifest,
        readable_selectors=selectors,
        selected_objects=selected,
        output_package=output,
        bedrock_model_arn=bedrock.selected_model_id if bedrock is not None else None,
        warnings=warnings,
        writable_selectors=writable_selectors,
        bedrock_invoke_resources=bedrock.invoke_resources if bedrock is not None else (),
        read_prefixes=read_prefixes,
    )
