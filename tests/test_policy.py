from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from conftest import MANIFEST_URI, TOP_HASH

from pearls import BedrockInvocation, InvalidScopeError, ManifestObject, PolicySizeError, compile
from pearls.bedrock import (
    bedrock_model_account,
    bedrock_model_region,
    bedrock_profile_resources_from_response,
    parse_bedrock_inventory,
    parse_bedrock_invocation,
    parse_bedrock_invocation_declaration,
)
from pearls.models import CompiledPolicy, PinnedManifestURI


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
    # The exact NotResource deny below bounds this compact wildcard allow.
    assert metadata_read["Resource"] == "*"
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


def test_runner_revision_read_is_exact_and_deny_bounded(
    manifest_loader, scope_spec: dict[str, object]
) -> None:
    result = compile(
        MANIFEST_URI,
        scope_spec,
        manifest_loader=manifest_loader,
        output_revision_id="scene-123",
    )

    assert result.output_package.revision_id == "scene-123"
    assert result.report()["output_revision_id"] == "scene-123"
    read_boundary = next(
        statement
        for statement in result.document["Statement"]
        if statement.get("Effect") == "Deny" and statement.get("Action") == "s3:GetObject"
    )
    assert read_boundary["NotResource"] == [
        f"arn:aws:s3:::manifest-bucket/.quilt/packages/{TOP_HASH}",
        "arn:aws:s3:::output-bucket/.quilt/workflows/*",
        "arn:aws:s3:::output-bucket/.quilt/named_packages/project/scene-output/latest",
        "arn:aws:s3:::output-bucket/.quilt/named_packages/project/scene-output/scene-123",
    ]
    assert not any(
        resource.endswith("/project/scene-output/*") for resource in read_boundary["NotResource"]
    )
    current_allow = next(
        statement
        for statement in result.document["Statement"]
        if statement.get("Effect") == "Allow" and statement.get("Action") == "s3:GetObject"
    )
    assert current_allow["Resource"] == "*"


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
    assert result.size == 1359
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
    # The exact NotResource deny and IAM deny precedence keep the wildcard
    # allow's effective write set equal to this declared output boundary.
    assert write == {
        "Effect": "Allow",
        "Action": "s3:PutObject",
        "Resource": "*",
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


def test_compile_accepts_narrow_uri_write_selectors(manifest_loader) -> None:
    writable = {
        "quilt+s3://output-bucket/results/#package=project%2Fscene-output": "result tree",
        "quilt+s3://output-bucket/summary.json#package=project%2Fscene-output": "summary",
    }
    result = compile(
        MANIFEST_URI,
        {
            "readable": ["input-content/data.csv"],
            "writable": writable,
            "require_version_ids": True,
        },
        manifest_loader=manifest_loader,
    )

    expected_data = [
        "arn:aws:s3:::output-bucket/project/scene-output/results/*",
        "arn:aws:s3:::output-bucket/project/scene-output/summary.json",
        "arn:aws:s3:::output-bucket/project/scene-output/run/invocation.json",
        "arn:aws:s3:::output-bucket/project/scene-output/run/scene.json",
    ]
    assert list(result.output_package.resources) == [
        *expected_data,
        "arn:aws:s3:::output-bucket/.quilt/packages/*",
        "arn:aws:s3:::output-bucket/.quilt/named_packages/project/scene-output/*",
    ]
    write_boundary = result.document["Statement"][2]
    assert write_boundary["NotResource"] == list(result.output_package.resources)
    assert result.document["Statement"][-1]["Resource"] == "*"
    assert [item["kind"] for item in result.report()["writable_selectors"]] == [
        "subtree",
        "object",
    ]


def test_compile_rejects_missing_or_competing_write_grammars(manifest_loader) -> None:
    with pytest.raises(InvalidScopeError, match="exactly one"):
        compile(
            MANIFEST_URI,
            {"readable": ["input-content/data.csv"]},
            manifest_loader=manifest_loader,
        )
    with pytest.raises(InvalidScopeError, match="exactly one"):
        compile(
            MANIFEST_URI,
            {
                "readable": ["input-content/data.csv"],
                "writable_package": "project/output@output-bucket",
                "writable": {"quilt+s3://output-bucket#package=project%2Foutput": "all"},
            },
            manifest_loader=manifest_loader,
        )


PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:123456789012:"
    "inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0"
)
PROFILE_MODEL_EAST = (
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
)
PROFILE_MODEL_WEST = (
    "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
)


def _profile_loader(profile_arn: str) -> tuple[str, ...]:
    assert profile_arn == PROFILE_ARN
    return (PROFILE_MODEL_EAST, PROFILE_MODEL_WEST)


def test_compile_inference_profile_separates_selection_from_exact_iam_closure(
    manifest_loader,
) -> None:
    result = compile(
        MANIFEST_URI,
        {
            "readable": ["input-content/data.csv"],
            "writable_package": "project/output@output-bucket",
            "require_version_ids": True,
            "bedrock_model": PROFILE_ARN,
            "bedrock_invoke_resources": [PROFILE_MODEL_WEST, PROFILE_ARN, PROFILE_MODEL_EAST],
        },
        manifest_loader=manifest_loader,
        bedrock_profile_loader=_profile_loader,
    )

    assert result.bedrock_model_id == PROFILE_ARN
    assert result.bedrock_invoke_resources == (
        PROFILE_ARN,
        PROFILE_MODEL_EAST,
        PROFILE_MODEL_WEST,
    )
    assert result.bedrock_invocation == BedrockInvocation(
        PROFILE_ARN,
        (PROFILE_ARN, PROFILE_MODEL_EAST, PROFILE_MODEL_WEST),
    )
    model_statements = [
        statement
        for statement in result.document["Statement"]
        if statement.get("Action") == "bedrock:InvokeModel"
    ]
    assert model_statements == [
        {
            "Effect": "Allow",
            "Action": "bedrock:InvokeModel",
            "Resource": PROFILE_ARN,
        },
        {
            "Effect": "Allow",
            "Action": "bedrock:InvokeModel",
            "Resource": [PROFILE_MODEL_EAST, PROFILE_MODEL_WEST],
            "Condition": {
                "StringLike": {
                    "bedrock:InferenceProfileArn": PROFILE_ARN,
                }
            },
        },
    ]
    assert all(
        PROFILE_MODEL_EAST not in statement.get("Resource", ())
        and PROFILE_MODEL_WEST not in statement.get("Resource", ())
        for statement in model_statements
        if "Condition" not in statement
    )
    assert not any(
        statement.get("Effect") == "Deny"
        and str(statement.get("Action", "")).startswith("bedrock:")
        for statement in result.document["Statement"]
    )
    assert result.report()["bedrock"] == {
        "selected_model_id": PROFILE_ARN,
        "selected_resource_type": "inference-profile",
        "invoke_resources": [PROFILE_ARN, PROFILE_MODEL_EAST, PROFILE_MODEL_WEST],
    }
    assert result.size <= 2048


def test_inference_profile_closure_counts_toward_the_sts_plaintext_limit(
    manifest_loader,
) -> None:
    scope = {
        "readable": ["input-content/data.csv"],
        "writable_package": "project/output@output-bucket",
        "require_version_ids": True,
        "bedrock_model": PROFILE_ARN,
        "bedrock_invoke_resources": [PROFILE_ARN, PROFILE_MODEL_EAST, PROFILE_MODEL_WEST],
    }
    compiled = compile(
        MANIFEST_URI,
        scope,
        manifest_loader=manifest_loader,
        bedrock_profile_loader=_profile_loader,
    )

    with pytest.raises(PolicySizeError, match="prefix fallback was not authorized"):
        compile(
            MANIFEST_URI,
            scope,
            manifest_loader=manifest_loader,
            max_policy_characters=compiled.size - 1,
            bedrock_profile_loader=_profile_loader,
        )


def test_bedrock_arn_helpers_preserve_selected_profile_identity() -> None:
    invocation = parse_bedrock_invocation(
        PROFILE_ARN,
        [PROFILE_MODEL_WEST, PROFILE_ARN, PROFILE_MODEL_EAST],
        profile_loader=_profile_loader,
    )

    assert invocation is not None
    assert invocation.selected_model_id == PROFILE_ARN
    assert invocation.supporting_foundation_models == (PROFILE_MODEL_EAST, PROFILE_MODEL_WEST)
    assert bedrock_model_region(PROFILE_ARN) == "us-east-1"
    assert bedrock_model_account(PROFILE_ARN) == "123456789012"
    assert bedrock_model_account(MODEL_ARN) is None


@pytest.mark.parametrize(
    ("selected", "resources", "message"),
    [
        (PROFILE_ARN, None, "must explicitly include"),
        (PROFILE_ARN, PROFILE_MODEL_EAST, "non-empty sequence"),
        (PROFILE_ARN, [PROFILE_MODEL_EAST], "include selected_model_id"),
        (
            PROFILE_ARN,
            [PROFILE_ARN, MODEL_ARN],
            "same exact foundation model",
        ),
        (
            PROFILE_ARN,
            [PROFILE_ARN, PROFILE_MODEL_EAST, PROFILE_MODEL_EAST],
            "duplicate",
        ),
        (
            MODEL_ARN,
            [MODEL_ARN, PROFILE_MODEL_EAST],
            "exactly its selected model",
        ),
    ],
)
def test_bedrock_invocation_rejects_incomplete_or_widened_closures(
    selected: str,
    resources: object,
    message: str,
) -> None:
    with pytest.raises(InvalidScopeError, match=message):
        parse_bedrock_invocation(selected, resources)


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:bedrock:us-east-1:12345678901:inference-profile/us.model",
        "arn:aws:bedrock:us-east-1::inference-profile/us.model",
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/us.model",
        "arn:aws:bedrock:us-east-1:123456789012:custom-model/model",
        "arn:aws-us-gov:bedrock:us-gov-west-1:123456789012:inference-profile/us.model",
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/*",
        "arn:aws:bedrock::123456789012:inference-profile/us.model",
    ],
)
def test_bedrock_parser_rejects_malformed_or_unsupported_profile_arns(arn: str) -> None:
    with pytest.raises(InvalidScopeError, match="account-scoped inference-profile"):
        parse_bedrock_invocation(arn)


def test_base_inventory_is_nonempty_exact_and_deterministic() -> None:
    assert parse_bedrock_inventory([PROFILE_MODEL_WEST, PROFILE_ARN, PROFILE_MODEL_EAST]) == (
        PROFILE_ARN,
        PROFILE_MODEL_EAST,
        PROFILE_MODEL_WEST,
    )
    with pytest.raises(InvalidScopeError, match="at least one"):
        parse_bedrock_inventory([])
    with pytest.raises(InvalidScopeError, match="duplicate"):
        parse_bedrock_inventory([MODEL_ARN, MODEL_ARN])


def test_runner_can_conservatively_omit_an_already_resolved_manifest_read(
    manifest_loader,
) -> None:
    scope = {
        "readable": ["input-content/data.csv"],
        "writable_package": "project/output@output-bucket",
    }

    default = compile(MANIFEST_URI, scope, manifest_loader=manifest_loader)
    runner_policy = compile(
        MANIFEST_URI,
        scope,
        manifest_loader=manifest_loader,
        include_manifest_read=False,
    )

    assert default.manifest.manifest_arn in default.policy_json
    assert runner_policy.manifest.manifest_arn not in runner_policy.policy_json
    assert runner_policy.size < default.size


def test_manifest_read_option_requires_a_boolean(manifest_loader) -> None:
    with pytest.raises(ValueError, match="include_manifest_read must be a boolean"):
        compile(
            MANIFEST_URI,
            {
                "readable": ["input-content/data.csv"],
                "writable_package": "project/output@output-bucket",
            },
            manifest_loader=manifest_loader,
            include_manifest_read="no",  # type: ignore[arg-type]
        )


PROFILE_MODEL_EU = (
    "arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
)
PROFILE_MODEL_REGIONLESS = (
    "arn:aws:bedrock:::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
)
GLOBAL_PROFILE_ARN = (
    "arn:aws:bedrock:us-east-2:123456789012:"
    "inference-profile/global.anthropic.claude-sonnet-4-20250514-v1:0"
)
GLOBAL_MODEL_EAST = (
    "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0"
)
GLOBAL_MODEL_REGIONLESS = (
    "arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0"
)


def _global_profile_loader(profile_arn: str) -> tuple[str, ...]:
    assert profile_arn == GLOBAL_PROFILE_ARN
    return (GLOBAL_MODEL_REGIONLESS, GLOBAL_MODEL_EAST)


@pytest.mark.parametrize("support", [PROFILE_MODEL_EU, PROFILE_MODEL_REGIONLESS])
def test_geographic_profile_rejects_same_model_outside_its_routing_geography(
    support: str,
) -> None:
    with pytest.raises(InvalidScopeError, match="routing geography"):
        parse_bedrock_invocation(PROFILE_ARN, [PROFILE_ARN, support])


def test_global_profile_accepts_regionless_support_but_never_as_selection(
    manifest_loader,
) -> None:
    invocation = parse_bedrock_invocation(
        GLOBAL_PROFILE_ARN,
        [GLOBAL_MODEL_EAST, GLOBAL_PROFILE_ARN, GLOBAL_MODEL_REGIONLESS],
        profile_loader=_global_profile_loader,
    )

    assert invocation is not None
    assert invocation.invoke_resources == (
        GLOBAL_PROFILE_ARN,
        GLOBAL_MODEL_REGIONLESS,
        GLOBAL_MODEL_EAST,
    )
    assert bedrock_model_region(GLOBAL_PROFILE_ARN) == "us-east-2"
    with pytest.raises(InvalidScopeError, match="regionless.*support"):
        parse_bedrock_invocation(GLOBAL_MODEL_REGIONLESS)
    with pytest.raises(InvalidScopeError, match="regionless"):
        parse_bedrock_invocation(
            GLOBAL_PROFILE_ARN,
            [GLOBAL_PROFILE_ARN, GLOBAL_MODEL_EAST],
        )

    result = compile(
        MANIFEST_URI,
        {
            "readable": ["input-content/data.csv"],
            "writable_package": "project/output@output-bucket",
            "require_version_ids": True,
            "bedrock_model": GLOBAL_PROFILE_ARN,
            "bedrock_invoke_resources": invocation.invoke_resources,
        },
        manifest_loader=manifest_loader,
        bedrock_profile_loader=_global_profile_loader,
    )
    support_statement = next(
        statement
        for statement in result.document["Statement"]
        if statement.get("Condition")
        == {
            "StringLike": {
                "bedrock:InferenceProfileArn": GLOBAL_PROFILE_ARN,
            }
        }
    )
    assert support_statement["Resource"] == [GLOBAL_MODEL_REGIONLESS, GLOBAL_MODEL_EAST]
    assert result.size <= 2048


def test_global_inventory_requires_justified_regionless_and_regional_support() -> None:
    assert parse_bedrock_inventory(
        [GLOBAL_MODEL_EAST, GLOBAL_PROFILE_ARN, GLOBAL_MODEL_REGIONLESS]
    ) == (
        GLOBAL_MODEL_REGIONLESS,
        GLOBAL_PROFILE_ARN,
        GLOBAL_MODEL_EAST,
    )
    with pytest.raises(InvalidScopeError, match="no matching global"):
        parse_bedrock_inventory([GLOBAL_MODEL_REGIONLESS])
    with pytest.raises(InvalidScopeError, match="regionless"):
        parse_bedrock_inventory([GLOBAL_PROFILE_ARN, GLOBAL_MODEL_EAST])
    with pytest.raises(InvalidScopeError, match="regional"):
        parse_bedrock_inventory([GLOBAL_PROFILE_ARN, GLOBAL_MODEL_REGIONLESS])


def test_commercial_partition_rejects_a_government_region_spelling() -> None:
    with pytest.raises(InvalidScopeError, match="commercial-partition"):
        parse_bedrock_invocation(
            "arn:aws:bedrock:us-gov-west-1:123456789012:"
            "inference-profile/us.anthropic.claude-3-haiku-20240307-v1:0"
        )


def test_profile_declaration_rejects_destinations_not_returned_by_aws() -> None:
    extra = "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
    declaration = parse_bedrock_invocation_declaration(
        PROFILE_ARN,
        [PROFILE_ARN, PROFILE_MODEL_EAST, PROFILE_MODEL_WEST, extra],
    )

    assert declaration is not None
    with pytest.raises(
        InvalidScopeError, match="exact GetInferenceProfile destinations.*unexpected"
    ):
        parse_bedrock_invocation(
            PROFILE_ARN,
            declaration.invoke_resources,
            profile_loader=_profile_loader,
        )


def test_profile_declaration_rejects_a_stale_incomplete_destination_set() -> None:
    with pytest.raises(InvalidScopeError, match="exact GetInferenceProfile destinations.*missing"):
        parse_bedrock_invocation(
            PROFILE_ARN,
            [PROFILE_ARN, PROFILE_MODEL_EAST],
            profile_loader=_profile_loader,
        )


def test_get_inference_profile_response_proves_exact_destination_identity() -> None:
    response = {
        "inferenceProfileArn": PROFILE_ARN,
        "status": "ACTIVE",
        "type": "SYSTEM_DEFINED",
        "models": [
            {"modelArn": PROFILE_MODEL_WEST},
            {"modelArn": PROFILE_MODEL_EAST},
        ],
    }

    assert bedrock_profile_resources_from_response(PROFILE_ARN, response) == (
        PROFILE_MODEL_EAST,
        PROFILE_MODEL_WEST,
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"inferenceProfileArn": GLOBAL_PROFILE_ARN}, "identity different"),
        ({"status": "INACTIVE"}, "ACTIVE"),
        ({"type": "APPLICATION"}, "SYSTEM_DEFINED"),
        ({"models": []}, "no destination models"),
        ({"models": [{}]}, "no exact modelArn"),
    ],
)
def test_get_inference_profile_response_fails_closed(
    change: dict[str, object], message: str
) -> None:
    response: dict[str, object] = {
        "inferenceProfileArn": PROFILE_ARN,
        "status": "ACTIVE",
        "type": "SYSTEM_DEFINED",
        "models": [{"modelArn": PROFILE_MODEL_EAST}, {"modelArn": PROFILE_MODEL_WEST}],
    }
    response.update(change)

    with pytest.raises(InvalidScopeError, match=message):
        bedrock_profile_resources_from_response(PROFILE_ARN, response)
