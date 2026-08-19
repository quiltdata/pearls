from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from conftest import MANIFEST_URI

from raja_sts import InvalidScopeError, ManifestObject, PolicySizeError, compile
from raja_sts.models import PinnedManifestURI


def _actions(policy: dict[str, object]) -> set[str]:
    actions: set[str] = set()
    for statement in policy["Statement"]:  # type: ignore[index,union-attr]
        value = statement["Action"]
        actions.update(value if isinstance(value, list) else [value])
    return actions


def test_compile_maps_logical_scope_to_exact_versioned_resources(
    manifest_loader, scope_spec: dict[str, object]
) -> None:
    result = compile(MANIFEST_URI, scope_spec, manifest_loader=manifest_loader)

    assert result.mode == "per-object-version"
    assert [item.logical_key for item in result.selected_objects] == [
        "input-content/data.csv",
        "input-content/notes.txt",
        "input-control/worker.json",
    ]
    assert "supervisor" not in result.policy_json
    assert "rubric" not in result.policy_json
    assert result.policy_json == json.dumps(result.document, separators=(",", ":"), sort_keys=True)
    assert result.size <= 2048
    assert result.warnings == ()

    statements = result.document["Statement"]
    reads = [
        item for item in statements if item["Effect"] == "Allow" and "Get" in str(item["Action"])
    ]
    assert len(reads) == 3
    assert all(item["Action"] == "s3:GetObjectVersion" for item in reads)
    assert reads[0]["Condition"]["StringEquals"]["s3:VersionId"] == "version-data"
    wrong_version_denies = [
        item
        for item in statements
        if item["Effect"] == "Deny" and "StringNotEquals" in item.get("Condition", {})
    ]
    assert len(wrong_version_denies) == 3
    assert wrong_version_denies[0]["Condition"]["StringNotEquals"]["s3:VersionId"] == (
        "version-data"
    )


def test_compile_allows_only_put_in_output_namespaces_and_explicitly_denies_bypass(
    manifest_loader, scope_spec: dict[str, object]
) -> None:
    result = compile(MANIFEST_URI, scope_spec, manifest_loader=manifest_loader)
    statements = result.document["Statement"]
    global_deny, read_boundary, write_boundary = statements[:3]
    write = statements[-1]

    assert global_deny == {
        "Effect": "Deny",
        "Action": ["s3:DeleteObject*", "s3:ListBucket*"],
        "Resource": "*",
    }
    assert read_boundary["Effect"] == "Deny"
    assert read_boundary["Action"] == ["s3:GetObject", "s3:GetObjectVersion"]
    assert "arn:aws:s3:::control-bucket/scenes/supervisor.json" not in read_boundary["NotResource"]
    assert write_boundary == {
        "Effect": "Deny",
        "Action": "s3:PutObject",
        "NotResource": [
            "arn:aws:s3:::output-bucket/project/scene-output/*",
            "arn:aws:s3:::output-bucket/.quilt/packages/*",
            "arn:aws:s3:::output-bucket/.quilt/named_packages/project/scene-output/*",
        ],
    }
    assert write == {
        "Effect": "Allow",
        "Action": "s3:PutObject",
        "Resource": write_boundary["NotResource"],
    }
    allowed_actions = {
        action
        for statement in statements
        if statement["Effect"] == "Allow"
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert "s3:ListBucket" not in allowed_actions
    assert not any(
        action.startswith("iam:") or action.startswith("sts:")
        for action in _actions(result.document)
    )  # type: ignore[arg-type]


def test_compile_is_deterministic(manifest_loader, scope_spec: dict[str, object]) -> None:
    first = compile(MANIFEST_URI, scope_spec, manifest_loader=manifest_loader)
    second = compile(MANIFEST_URI, scope_spec, manifest_loader=manifest_loader)
    assert first.policy_json == second.policy_json
    assert first.report() == second.report()
    assert first.report()["writable_package"] == "project/scene-output@output-bucket"


def test_exact_selector_does_not_include_sibling_control_files(manifest_loader) -> None:
    result = compile(
        MANIFEST_URI,
        {
            "readable": ["input-control/worker.json"],
            "writable_package": "project/output@output-bucket",
        },
        manifest_loader=manifest_loader,
    )
    assert [item.logical_key for item in result.selected_objects] == ["input-control/worker.json"]


def test_selector_without_trailing_slash_can_select_a_directory(manifest_loader) -> None:
    result = compile(
        MANIFEST_URI,
        {
            "readable": ["input-content"],
            "writable_package": "project/output@output-bucket",
        },
        manifest_loader=manifest_loader,
    )
    assert len(result.selected_objects) == 2


def test_unversioned_read_is_reported_honestly(manifest_loader) -> None:
    def add_unversioned(manifest: PinnedManifestURI) -> Sequence[ManifestObject]:
        return (
            *manifest_loader(manifest),
            ManifestObject("legacy/data", "input-bucket", "legacy/data", None),
        )

    result = compile(
        MANIFEST_URI,
        {
            "readable": ["legacy/data"],
            "writable_package": "project/output@output-bucket",
        },
        manifest_loader=add_unversioned,
    )
    assert result.mode == "per-object"
    allowed_reads = [
        statement
        for statement in result.document["Statement"]
        if statement["Effect"] == "Allow" and statement["Action"] == "s3:GetObject"
    ]
    assert len(allowed_reads) == 1
    assert "pins their keys but not their bytes" in result.warnings[0]


def test_mixed_version_mode_and_required_versions(manifest_loader) -> None:
    def add_unversioned(manifest: PinnedManifestURI) -> Sequence[ManifestObject]:
        return (
            *manifest_loader(manifest),
            ManifestObject("legacy/data", "input-bucket", "legacy/data", None),
        )

    scope = {
        "readable": ["input-content/data.csv", "legacy/data"],
        "writable_package": "project/output@output-bucket",
    }
    assert compile(MANIFEST_URI, scope, manifest_loader=add_unversioned).mode == "mixed"
    scope["require_version_ids"] = True
    with pytest.raises(InvalidScopeError, match="legacy/data"):
        compile(MANIFEST_URI, scope, manifest_loader=add_unversioned)


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"readable": "not-a-list", "writable_package": "a@valid-bucket"},
        {"readable": [], "writable_package": "a@valid-bucket"},
        {"readable": ["../escape"], "writable_package": "a@valid-bucket"},
        {"readable": ["missing"], "writable_package": "a@valid-bucket"},
        {"readable": ["input-content/"], "writable_package": "a@valid-bucket", "typo": True},
        {
            "readable": ["input-content/"],
            "writable_package": "a@valid-bucket",
            "allow_prefix_fallback": "yes",
        },
    ],
)
def test_compile_rejects_invalid_or_ambiguous_scopes(
    manifest_loader, scope: dict[str, object]
) -> None:
    with pytest.raises(InvalidScopeError):
        compile(MANIFEST_URI, scope, manifest_loader=manifest_loader)


def test_compile_rejects_non_mapping_scope(manifest_loader) -> None:
    with pytest.raises(InvalidScopeError):
        compile(MANIFEST_URI, [])  # type: ignore[arg-type]


def _large_loader(manifest: PinnedManifestURI) -> Sequence[ManifestObject]:
    return tuple(
        ManifestObject(
            logical_key=f"input/item-{number:02}.txt",
            bucket="large-input-bucket",
            key=f"sealed/input/item-{number:02}.txt",
            version_id=f"version-{number:02}",
        )
        for number in range(30)
    )


def test_policy_size_fails_closed_without_authorized_fallback() -> None:
    with pytest.raises(PolicySizeError, match="prefix fallback was not authorized"):
        compile(
            MANIFEST_URI,
            {
                "readable": ["input/"],
                "writable_package": "project/output@output-bucket",
            },
            manifest_loader=_large_loader,
        )


def test_explicit_prefix_fallback_reports_degraded_authority() -> None:
    result = compile(
        MANIFEST_URI,
        {
            "readable": ["input/"],
            "writable_package": "project/output@output-bucket",
            "allow_prefix_fallback": True,
        },
        manifest_loader=_large_loader,
    )
    assert result.mode == "per-prefix"
    assert "arn:aws:s3:::large-input-bucket/sealed/input/*" in result.policy_json
    assert len(result.warnings) == 3
    assert "widens" in result.warnings[0]
    assert result.size <= 2048


def test_prefix_policy_can_also_exceed_a_smaller_caller_limit() -> None:
    with pytest.raises(PolicySizeError, match="prefix session policy"):
        compile(
            MANIFEST_URI,
            {
                "readable": ["input/"],
                "writable_package": "project/output@output-bucket",
                "allow_prefix_fallback": True,
            },
            manifest_loader=_large_loader,
            max_policy_characters=100,
        )


@pytest.mark.parametrize("limit", [0, -1, 2049, 1.5, "2048"])
def test_policy_limit_must_stay_within_the_aws_hard_limit(
    manifest_loader, scope_spec, limit: object
) -> None:
    with pytest.raises(ValueError, match="between 1 and 2048"):
        compile(
            MANIFEST_URI,
            scope_spec,
            manifest_loader=manifest_loader,
            max_policy_characters=limit,  # type: ignore[arg-type]
        )
