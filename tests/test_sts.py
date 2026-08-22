from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import MANIFEST_URI

from pearls import (
    InvalidAssumeRoleRequest,
    MintError,
    PackedPolicyTooLargeError,
    compile,
    mint,
    mint_compiled,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/scene-execution"
EXPIRATION = datetime(2026, 8, 19, 18, 0, tzinfo=UTC)


class FakeSTS:
    def __init__(
        self, response: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response = response or {
            "Credentials": {
                "AccessKeyId": "ASIAEXAMPLE",
                "SecretAccessKey": "secret-value",
                "SessionToken": "session-value",
                "Expiration": EXPIRATION,
            },
            "AssumedRoleUser": {
                "Arn": "arn:aws:sts::123456789012:assumed-role/scene-execution/scene-123",
                "AssumedRoleId": "AROAEXAMPLE:scene-123",
            },
            "PackedPolicySize": 12,
        }
        self.error = error

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class PackedPolicyError(Exception):
    response = {"Error": {"Code": "PackedPolicyTooLarge"}}


def test_mint_compiles_and_sends_the_inline_policy(
    manifest_loader, scope_spec: dict[str, object]
) -> None:
    sts = FakeSTS()
    result = mint(
        MANIFEST_URI,
        scope_spec,
        role_arn=ROLE_ARN,
        scene_id="scene-123",
        duration_seconds=900,
        external_id="orchestrator-tenant",
        manifest_loader=manifest_loader,
        sts_client=sts,
    )

    assert sts.requests == [
        {
            "RoleArn": ROLE_ARN,
            "RoleSessionName": "scene-123",
            "Policy": result.compilation.policy_json,
            "DurationSeconds": 900,
            "ExternalId": "orchestrator-tenant",
        }
    ]
    assert result.expiration == EXPIRATION
    assert result.assumed_role_id == "AROAEXAMPLE:scene-123"
    assert result.packed_policy_size == 12
    assert result.boto3_kwargs() == {
        "aws_access_key_id": "ASIAEXAMPLE",
        "aws_secret_access_key": "secret-value",
        "aws_session_token": "session-value",
    }
    representation = repr(result)
    assert "secret-value" not in representation
    assert "session-value" not in representation
    assert "ASIAEXAMPLE" not in representation


def test_mint_compiled_uses_default_sts_client(
    monkeypatch: pytest.MonkeyPatch, manifest_loader, scope_spec: dict[str, object]
) -> None:
    compilation = compile(MANIFEST_URI, scope_spec, manifest_loader=manifest_loader)
    sts = FakeSTS()
    monkeypatch.setattr(
        "pearls.sts.boto3.client", lambda service: sts if service == "sts" else None
    )
    result = mint_compiled(compilation, role_arn=ROLE_ARN, scene_id="scene-default")
    assert result.assumed_role_arn.endswith("/scene-123")
    assert "ExternalId" not in sts.requests[0]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"role_arn": "not-an-arn", "scene_id": "scene-ok"}, "role_arn"),
        (
            {
                "role_arn": "arn:aws-us-gov:iam::123456789012:role/scene-execution",
                "scene_id": "scene-ok",
            },
            "role_arn",
        ),
        ({"role_arn": ROLE_ARN, "scene_id": "bad scene"}, "scene_id"),
        ({"role_arn": ROLE_ARN, "scene_id": "x"}, "scene_id"),
        ({"role_arn": ROLE_ARN, "scene_id": "scene", "duration_seconds": 899}, "duration"),
        ({"role_arn": ROLE_ARN, "scene_id": "scene", "duration_seconds": 43201}, "duration"),
        ({"role_arn": ROLE_ARN, "scene_id": "scene", "external_id": "x"}, "external_id"),
    ],
)
def test_mint_rejects_invalid_assume_role_parameters(
    manifest_loader, scope_spec: dict[str, object], kwargs: dict[str, Any], message: str
) -> None:
    compilation = compile(MANIFEST_URI, scope_spec, manifest_loader=manifest_loader)
    with pytest.raises(InvalidAssumeRoleRequest, match=message):
        mint_compiled(compilation, sts_client=FakeSTS(), **kwargs)


def test_mint_wraps_sts_failures(manifest_loader, scope_spec: dict[str, object]) -> None:
    compilation = compile(MANIFEST_URI, scope_spec, manifest_loader=manifest_loader)
    with pytest.raises(MintError, match="AccessDenied"):
        mint_compiled(
            compilation,
            role_arn=ROLE_ARN,
            scene_id="scene-denied",
            sts_client=FakeSTS(error=RuntimeError("AccessDenied")),
        )

    with pytest.raises(
        PackedPolicyTooLargeError,
        match=(
            rf"3 selected object\(s\) across 2 read prefix\(es\); plaintext size "
            rf"{compilation.size} of 2048 characters"
        ),
    ):
        mint_compiled(
            compilation,
            role_arn=ROLE_ARN,
            scene_id="scene-packed",
            sts_client=FakeSTS(error=PackedPolicyError("packed")),
        )


def test_mint_rejects_malformed_sts_response(
    manifest_loader, scope_spec: dict[str, object]
) -> None:
    compilation = compile(MANIFEST_URI, scope_spec, manifest_loader=manifest_loader)
    malformed = FakeSTS(
        response={
            "Credentials": {
                "AccessKeyId": "key",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": "not-a-datetime",
            },
            "AssumedRoleUser": {"Arn": "arn", "AssumedRoleId": "id"},
        }
    )
    with pytest.raises(MintError, match="not a datetime"):
        mint_compiled(
            compilation,
            role_arn=ROLE_ARN,
            scene_id="scene-malformed",
            sts_client=malformed,
        )
