from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from conftest import MANIFEST_URI, TOP_HASH

from raja_sts import InvalidScopeError, ManifestObject, PolicySizeError, compile
from raja_sts.models import CompiledPolicy, PinnedManifestURI


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
    versioning_read = next(item for item in reads if item["Action"] == "s3:GetBucketVersioning")
    assert versioning_read == {
        "Effect": "Allow",
        "Action": "s3:GetBucketVersioning",
        "Resource": "arn:aws:s3:::output-bucket",
    }
    version_read = next(item for item in reads if item["Action"] == "s3:GetObjectVersion")
    assert version_read["Resource"] == [
        "arn:aws:s3:::input-bucket/sealed/data.csv",
        "arn:aws:s3:::input-bucket/sealed/notes.txt",
        "arn:aws:s3:::control-bucket/scenes/worker.json",
    ]
    assert "Condition" not in version_read
    metadata_read = next(item for item in reads if item["Action"] == "s3:GetObject")
    assert metadata_read["Resource"] == [
        f"arn:aws:s3:::manifest-bucket/.quilt/packages/{TOP_HASH}",
        "arn:aws:s3:::output-bucket/.quilt/workflows/*",
        "arn:aws:s3:::output-bucket/.quilt/named_packages/project/scene-output/latest",
    ]
    wrong_version_denies = [
        item
        for item in statements
        if item["Effect"] == "Deny" and "StringNotEquals" in item.get("Condition", {})
    ]
    assert len(wrong_version_denies) == 1
    assert wrong_version_denies[0]["Resource"] == "*"
    assert wrong_version_denies[0]["Condition"]["StringNotEquals"]["s3:VersionId"] == [
        "version-data",
        "version-notes",
        "version-worker",
    ]


def test_five_object_scene_stays_exact_and_under_sts_limit(manifest_loader) -> None:
    result = compile(
        MANIFEST_URI,
        {
            "readable": ["input-content/", "input-control/", "rubric/secret.json"],
            "writable_package": "project/scene-output@output-bucket",
            "require_version_ids": True,
        },
        manifest_loader=manifest_loader,
    )

    assert result.mode == "per-object-version"
    assert len(result.selected_objects) == 5
    assert result.size == 1767
    version_denies = [
        statement
        for statement in result.document["Statement"]
        if statement.get("Condition", {}).get("StringNotEquals")
    ]
    assert len(version_denies) == 1
    assert version_denies[0]["Resource"] == "*"
    assert set(version_denies[0]["Condition"]["StringNotEquals"]["s3:VersionId"]) == {
        "version-data",
        "version-notes",
        "version-worker",
        "version-supervisor",
        "version-secret",
    }
    version_allows = [
        statement
        for statement in result.document["Statement"]
        if statement["Effect"] == "Allow" and statement["Action"] == "s3:GetObjectVersion"
    ]
    assert len(version_allows) == 1
    assert len(version_allows[0]["Resource"]) == 5


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
    assert read_boundary == {
        "Effect": "Deny",
        "Action": "s3:GetObject",
        "NotResource": [
            f"arn:aws:s3:::manifest-bucket/.quilt/packages/{TOP_HASH}",
            "arn:aws:s3:::output-bucket/.quilt/workflows/*",
            "arn:aws:s3:::output-bucket/.quilt/named_packages/project/scene-output/latest",
        ],
    }
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
    assert "s3:GetBucketVersioning" in allowed_actions
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


MODEL_ARN = "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0"


def test_compile_admits_only_one_exact_bedrock_model(manifest_loader) -> None:
    result = compile(
        MANIFEST_URI,
        {
            "readable": ["input-content/data.csv"],
            "writable_package": "project/output@output-bucket",
            "require_version_ids": True,
            "bedrock_model": MODEL_ARN,
        },
        manifest_loader=manifest_loader,
    )

    statements = result.document["Statement"]
    model_statements = [
        statement for statement in statements if statement.get("Action") == "bedrock:InvokeModel"
    ]
    assert model_statements == [
        {"Effect": "Allow", "Action": "bedrock:InvokeModel", "Resource": MODEL_ARN}
    ]
    assert result.bedrock_model_arn == MODEL_ARN
    assert result.report()["bedrock_model_arn"] == MODEL_ARN
    assert "bedrock:*" not in result.policy_json
    assert "anthropic.claude" not in result.policy_json


@pytest.mark.parametrize(
    "model",
    [
        "*",
        "arn:aws:bedrock:us-east-1::foundation-model/*",
        "arn:aws-cn:bedrock:cn-north-1::foundation-model/example",
        "arn:aws:bedrock:us-east-1:123456789012:custom-model/example",
        "arn:aws:bedrock:us-east-1::inference-profile/example",
        3,
    ],
)
def test_compile_rejects_non_exact_or_unsupported_bedrock_resources(
    manifest_loader, model: object
) -> None:
    with pytest.raises(InvalidScopeError, match="bedrock_model"):
        compile(
            MANIFEST_URI,
            {
                "readable": ["input-content/data.csv"],
                "writable_package": "project/output@output-bucket",
                "bedrock_model": model,
            },
            manifest_loader=manifest_loader,
        )


def test_compiled_policy_preserves_legacy_positional_warnings(manifest_loader) -> None:
    compiled = compile(
        MANIFEST_URI,
        {
            "readable": ["input-content/data.csv"],
            "writable_package": "project/output@output-bucket",
        },
        manifest_loader=manifest_loader,
    )
    legacy = CompiledPolicy(
        compiled.document,
        compiled.policy_json,
        compiled.mode,
        compiled.manifest,
        compiled.readable_selectors,
        compiled.selected_objects,
        compiled.output_package,
        ("legacy warning",),
    )

    assert legacy.warnings == ("legacy warning",)
    assert legacy.bedrock_model_arn is None
