"""Compile logical Scene scopes into deterministic AWS session policies."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
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
from .models import CompiledPolicy, ManifestObject, OutputPackage, OutputSelector, PolicyMode

DEFAULT_MAX_POLICY_CHARACTERS = 2048
_ALLOWED_SCOPE_KEYS = {
    "readable",
    "writable_package",
    "writable",
    "bedrock_model",
    "bedrock_invoke_resources",
    "allow_prefix_fallback",
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


def _guardrail_statements(
    current_read_resources: Sequence[str], output: OutputPackage
) -> list[dict[str, Any]]:
    """Return explicit denies that resource policies cannot bypass."""
    return [
        {
            "Effect": "Deny",
            "Action": ["s3:DeleteObject*", "s3:ListBucket*"],
            "Resource": "*",
        },
        {
            "Effect": "Deny",
            "Action": "s3:GetObject",
            "NotResource": _deduplicate(current_read_resources),
        },
        {
            "Effect": "Deny",
            "Action": "s3:PutObject",
            "NotResource": list(output.write_resources),
        },
    ]


def _write_statement(output: OutputPackage) -> dict[str, Any]:
    """Allow writes only where the paired exact NotResource deny does not apply.

    The wildcard avoids duplicating a potentially large resource list in the STS
    plaintext policy. ``_guardrail_statements`` always emits the matching explicit
    deny, so IAM's deny precedence leaves the effective write set unchanged.
    """
    return {
        "Effect": "Allow",
        "Action": "s3:PutObject",
        "Resource": "*",
    }


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


def _current_read_statement(resources: Sequence[str]) -> dict[str, Any]:
    """Allow current-object reads for exact versionless resources."""
    return {
        "Effect": "Allow",
        "Action": "s3:GetObject",
        "Resource": _deduplicate(resources),
    }


def _exact_read_statements(
    objects: Sequence[ManifestObject], metadata_resources: Sequence[str]
) -> tuple[list[dict[str, Any]], PolicyMode]:
    versioned = [item for item in objects if item.version_id is not None]
    unversioned = [item for item in objects if item.version_id is None]
    statements: list[dict[str, Any]] = []

    if versioned:
        resources = _deduplicate([item.arn for item in versioned])
        version_ids = list(dict.fromkeys(item.version_id for item in versioned))
        statements.extend(
            [
                {
                    "Effect": "Deny",
                    "Action": "s3:GetObjectVersion",
                    "Resource": "*",
                    "Condition": {"StringNotEquals": {"s3:VersionId": version_ids}},
                },
                {
                    "Effect": "Allow",
                    "Action": "s3:GetObjectVersion",
                    "Resource": resources,
                },
            ]
        )
    else:
        statements.append(
            {
                "Effect": "Deny",
                "Action": "s3:GetObjectVersion",
                "Resource": "*",
            }
        )

    current_resources = [item.arn for item in unversioned]
    current_resources.extend(metadata_resources)
    statements.append(_current_read_statement(current_resources))

    if versioned and unversioned:
        mode: PolicyMode = "mixed"
    elif versioned:
        mode = "per-object-version"
    else:
        mode = "per-object"
    return statements, mode


def _directory_prefix(keys: Sequence[str]) -> str:
    """Return a common S3 directory prefix, never a partial path component."""
    if not keys:
        return ""
    shared = commonprefix(list(keys))
    if "/" not in shared:
        return ""
    return shared[: shared.rfind("/") + 1]


def _prefix_read_statements(
    objects: Sequence[ManifestObject],
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    by_bucket: dict[str, list[str]] = defaultdict(list)
    for item in objects:
        by_bucket[item.bucket].append(item.key)

    resources: list[str] = []
    for bucket in sorted(by_bucket):
        prefix = _directory_prefix(sorted(by_bucket[bucket]))
        resources.append(f"arn:aws:s3:::{bucket}/{prefix}*")
    statement = {
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:GetObjectVersion"],
        "Resource": resources,
    }
    warnings = (
        "Prefix fallback widens readable S3 resources beyond the pinned manifest selection.",
        "Prefix fallback cannot enforce manifest version IDs; "
        "verify content hashes after download.",
        f"Widened read resources: {', '.join(resources)}",
    )
    return [statement], warnings


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
) -> CompiledPolicy:
    """Compile a pinned Quilt manifest and logical scope into an STS policy.

    Exact object resources are always attempted first. If they exceed the STS
    plaintext policy limit, compilation fails closed unless the caller explicitly
    sets ``allow_prefix_fallback`` in the sealed scope specification.
    ``include_manifest_read=False`` is reserved for runners that already resolved
    the manifest and carry every selected object descriptor in memory.
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

    manifest = parse_manifest_uri(manifest_uri)
    selectors = _parse_readable(scope_spec.get("readable"))
    output, writable_selectors = _parse_output_scope(scope_spec)
    bedrock = parse_bedrock_invocation(
        scope_spec.get("bedrock_model"),
        scope_spec.get("bedrock_invoke_resources"),
        profile_loader=bedrock_profile_loader,
    )
    allow_prefix_fallback = _parse_boolean(scope_spec, "allow_prefix_fallback")
    require_version_ids = _parse_boolean(scope_spec, "require_version_ids")

    loader = manifest_loader or load_quilt_manifest
    selected = _resolve_selectors(selectors, tuple(loader(manifest)))
    missing_versions = [item.logical_key for item in selected if item.version_id is None]
    if require_version_ids and missing_versions:
        raise InvalidScopeError(
            "selected objects have no S3 version ID: " + ", ".join(missing_versions)
        )

    metadata_read_resources = list(output.read_resources)
    if include_manifest_read:
        metadata_read_resources.insert(0, manifest.manifest_arn)
    read_statements, mode = _exact_read_statements(selected, metadata_read_resources)
    current_read_resources = [item.arn for item in selected if item.version_id is None]
    current_read_resources.extend(metadata_read_resources)
    guardrails = _guardrail_statements(current_read_resources, output)
    model_statements = _bedrock_statements(bedrock)
    document, policy_json = _serialize(
        [
            *guardrails,
            *read_statements,
            _output_versioning_statement(output),
            *model_statements,
            _write_statement(output),
        ]
    )
    warnings: tuple[str, ...] = ()
    if missing_versions:
        warnings = (
            "Some selected objects have no S3 version ID; IAM pins their keys but not their bytes. "
            "Verify Quilt hashes after download.",
        )

    if len(policy_json) > max_policy_characters:
        if not allow_prefix_fallback:
            raise PolicySizeError(
                f"inline session policy is {len(policy_json)} characters; "
                f"limit is {max_policy_characters}; prefix fallback was not authorized"
            )
        prefix_statements, prefix_warnings = _prefix_read_statements(selected)
        mode = "per-prefix"
        warnings += prefix_warnings
        prefix_resources = prefix_statements[0]["Resource"]
        read_statements = [
            {
                "Effect": "Deny",
                "Action": "s3:GetObjectVersion",
                "NotResource": prefix_resources,
            },
            *prefix_statements,
            _current_read_statement(metadata_read_resources),
        ]
        guardrails = _guardrail_statements([*prefix_resources, *metadata_read_resources], output)
        document, policy_json = _serialize(
            [
                *guardrails,
                *read_statements,
                _output_versioning_statement(output),
                *model_statements,
                _write_statement(output),
            ]
        )
        if len(policy_json) > max_policy_characters:
            raise PolicySizeError(
                f"prefix session policy is {len(policy_json)} characters; "
                f"limit is {max_policy_characters}"
            )

    return CompiledPolicy(
        document=document,
        policy_json=policy_json,
        mode=mode,
        manifest=manifest,
        readable_selectors=selectors,
        selected_objects=selected,
        output_package=output,
        bedrock_model_arn=bedrock.selected_model_id if bedrock is not None else None,
        warnings=warnings,
        writable_selectors=writable_selectors,
        bedrock_invoke_resources=bedrock.invoke_resources if bedrock is not None else (),
    )
