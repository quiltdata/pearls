from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import MANIFEST_URI

import raja_sts.runner as runner_module
from raja_sts import (
    AuthorityModeError,
    HandlerContractError,
    InputIntegrityError,
    ManifestObject,
    SceneContext,
    SceneHandlerResult,
    run_scene,
)
from raja_sts.models import PinnedManifestURI

MODEL_ARN = "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0"
ROLE_ARN = "arn:aws:iam::123456789012:role/scene-execution"
CONTENT = b"Tell me hello."


class Body(io.BytesIO):
    was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


class FakeS3:
    def __init__(
        self,
        events: list[str],
        *,
        content: object = CONTENT,
        returned_version: str = "input-version",
        put_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.content = content
        self.returned_version = returned_version
        self.put_error = put_error
        self.get_requests: list[dict[str, Any]] = []
        self.put_requests: list[dict[str, Any]] = []
        self.body: Body | None = None

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append("get")
        self.get_requests.append(kwargs)
        self.body = Body(self.content) if isinstance(self.content, bytes) else self.content  # type: ignore[assignment]
        return {"Body": self.body, "VersionId": self.returned_version}

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.events.append(f"put:{kwargs['Key']}")
        if self.put_error is not None:
            raise self.put_error
        self.put_requests.append(kwargs)
        return {"VersionId": f"version-{len(self.put_requests)}"}


class FakeBedrock:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append("converse")
        self.requests.append(kwargs)
        return {
            "modelId": MODEL_ARN,
            "output": {"message": {"content": [{"text": "Hello."}]}},
            "usage": {"inputTokens": 4, "outputTokens": 2},
            "stopReason": "end_turn",
            "ResponseMetadata": {"RequestId": "request-1"},
        }


class FakeSession:
    def __init__(self, events: list[str], s3: FakeS3) -> None:
        self.events = events
        self.s3 = s3
        self.bedrock = FakeBedrock(events)

    def client(self, service: str) -> Any:
        self.events.append(f"client:{service}")
        if service == "s3":
            return self.s3
        if service == "bedrock-runtime":
            return self.bedrock
        raise AssertionError(f"unexpected service: {service}")


class FakeSTS:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests: list[dict[str, Any]] = []

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append("mint")
        self.requests.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "temporary-access",
                "SecretAccessKey": "temporary-secret",
                "SessionToken": "temporary-token",
                "Expiration": datetime(2026, 8, 19, 13, tzinfo=UTC),
            },
            "AssumedRoleUser": {
                "Arn": "arn:aws:sts::123456789012:assumed-role/scene-execution/scene-3",
                "AssumedRoleId": "AROATEST:scene-3",
            },
            "PackedPolicySize": 12,
        }


def _loader(
    events: list[str],
    *,
    content_hash: str | None = None,
    hash_type: str = "SHA256",
    version_id: str | None = "input-version",
) -> Callable[[PinnedManifestURI], Sequence[ManifestObject]]:
    def load(manifest: PinnedManifestURI) -> Sequence[ManifestObject]:
        events.append("compile")
        assert manifest.original == MANIFEST_URI
        return (
            ManifestObject(
                logical_key="input/prompt.txt",
                bucket="input-bucket",
                key="sealed/prompt.txt",
                version_id=version_id,
                hash_type=hash_type,
                hash_value=content_hash or hashlib.sha256(CONTENT).hexdigest(),
            ),
        )

    return load


def _scope(*, model: object = MODEL_ARN, require_versions: bool = True) -> dict[str, object]:
    scope: dict[str, object] = {
        "readable": ["input/prompt.txt"],
        "writable_package": "project/scene-output@output-bucket",
        "require_version_ids": require_versions,
    }
    if model is not None:
        scope["bedrock_model"] = model
    return scope


def _execute(
    *,
    handler: Callable[[Mapping[str, Any], Any, SceneContext], Any],
    loader: Callable[[PinnedManifestURI], Sequence[ManifestObject]] | None = None,
    s3: FakeS3 | None = None,
    scope: dict[str, object] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[Any, list[str], FakeS3, FakeSTS, FakeSession, dict[str, Any]]:
    events: list[str] = []
    fake_s3 = s3 or FakeS3(events)
    fake_s3.events = events
    sts = FakeSTS(events)
    session = FakeSession(events, fake_s3)
    session_kwargs: dict[str, Any] = {}

    def session_factory(**kwargs: Any) -> FakeSession:
        events.append("session")
        session_kwargs.update(kwargs)
        return session

    times = iter(
        [
            datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 19, 12, 1, tzinfo=UTC),
        ]
    )
    result = run_scene(
        manifest_uri=MANIFEST_URI,
        scope_spec=scope or _scope(),
        role_arn=ROLE_ARN,
        scene_id="scene-3",
        handler=handler,
        manifest_loader=loader or _loader(events),
        sts_client=sts,
        session_factory=session_factory,
        clock=clock or (lambda: next(times)),
    )
    return result, events, fake_s3, sts, session, session_kwargs


def test_run_scene_executes_one_bounded_lifecycle_and_publishes_records() -> None:
    handler_calls: list[tuple[Mapping[str, Any], Any, SceneContext]] = []

    def handler(
        inputs: Mapping[str, Any], session: FakeSession, context: SceneContext
    ) -> SceneHandlerResult:
        handler_calls.append((inputs, session, context))
        session.events.append("handler")
        prompt = inputs["input/prompt.txt"].content.decode()
        response = session.client("bedrock-runtime").converse(
            modelId=context.bedrock_model_arn,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
        )
        return SceneHandlerResult(
            objects={"result/prompt.txt": prompt, "result/response.txt": "Hello."},
            invocation={
                "model_id": response["modelId"],
                "request_id": response["ResponseMetadata"]["RequestId"],
                "usage": response["usage"],
                "stop_reason": response["stopReason"],
            },
        )

    result, events, s3, sts, session, session_kwargs = _execute(handler=handler)

    assert events.count("compile") == events.count("mint") == events.count("handler") == 1
    assert events.index("compile") < events.index("mint") < events.index("session")
    assert events.index("get") < events.index("handler") < events.index("converse")
    assert len(handler_calls) == 1
    assert s3.body is not None and s3.body.was_closed
    assert s3.get_requests == [
        {"Bucket": "input-bucket", "Key": "sealed/prompt.txt", "VersionId": "input-version"}
    ]
    assert sts.requests[0]["RoleSessionName"] == "scene-3"
    assert sts.requests[0]["Policy"].count("bedrock:InvokeModel") == 1
    assert session_kwargs == {
        "region_name": "us-east-1",
        "aws_access_key_id": "temporary-access",
        "aws_secret_access_key": "temporary-secret",
        "aws_session_token": "temporary-token",
    }
    assert session.bedrock.requests[0]["modelId"] == MODEL_ARN

    keys = [request["Key"] for request in s3.put_requests]
    assert keys[:4] == [
        "project/scene-output/result/prompt.txt",
        "project/scene-output/result/response.txt",
        "project/scene-output/run/invocation.json",
        "project/scene-output/run/scene.json",
    ]
    assert keys[-1] == ".quilt/named_packages/project/scene-output/latest"
    assert keys[-3].startswith(".quilt/packages/")
    assert keys[-2] == ".quilt/named_packages/project/scene-output/1787140860"

    by_key = {request["Key"]: request["Body"] for request in s3.put_requests}
    scene_record = json.loads(by_key["project/scene-output/run/scene.json"])
    invocation = json.loads(by_key["project/scene-output/run/invocation.json"])
    assert scene_record["$schema"].endswith("/v1")
    assert scene_record["scene_id"] == "scene-3"
    assert scene_record["input"]["top_hash"] == "a" * 64
    assert scene_record["input"]["selected_objects"][0]["version_id"] == "input-version"
    assert scene_record["authority"]["bedrock_model_arn"] == MODEL_ARN
    assert scene_record["assumed_role"]["id"] == "AROATEST:scene-3"
    assert invocation["request_id"] == "request-1"
    serialized_records = json.dumps([scene_record, invocation])
    assert "temporary-access" not in serialized_records
    assert "temporary-secret" not in serialized_records
    assert "temporary-token" not in serialized_records

    assert result.top_hash == keys[-3].rsplit("/", 1)[-1]
    assert result.assumed_role_arn.endswith("/scene-3")
    assert result.package_uri.endswith(f"&top_hash={result.top_hash}")
    manifest = by_key[keys[-3]].decode().splitlines()
    assert json.loads(manifest[0]) == {}
    assert {json.loads(line)["logical_key"] for line in manifest[1:]} == {
        "result/prompt.txt",
        "result/response.txt",
        "run/invocation.json",
        "run/scene.json",
    }


@pytest.mark.parametrize(
    ("scope", "loader", "message"),
    [
        (_scope(model=None), None, "bedrock_model"),
        (_scope(require_versions=False), _loader([], version_id=None), "per-object-version"),
    ],
)
def test_runner_rejects_weakened_authority_before_mint(
    scope: dict[str, object],
    loader: Callable[[PinnedManifestURI], Sequence[ManifestObject]] | None,
    message: str,
) -> None:
    events: list[str] = []
    with pytest.raises(AuthorityModeError, match=message):
        run_scene(
            manifest_uri=MANIFEST_URI,
            scope_spec=scope,
            role_arn=ROLE_ARN,
            scene_id="scene-3",
            handler=lambda *_: SceneHandlerResult({"result.txt": b"x"}, {}),
            manifest_loader=loader or _loader(events),
            sts_client=FakeSTS(events),
        )
    assert "mint" not in events


def test_runner_rejects_missing_hash_before_read() -> None:
    events: list[str] = []
    loader = _loader(events, content_hash="")

    def missing_hash(_: PinnedManifestURI) -> Sequence[ManifestObject]:
        item = loader(PinnedManifestURI("manifest-bucket", "x", "a" * 64, MANIFEST_URI))[0]
        return (ManifestObject(item.logical_key, item.bucket, item.key, item.version_id),)

    with pytest.raises(AuthorityModeError, match="lacks a version ID or content hash"):
        _execute(
            handler=lambda *_: SceneHandlerResult({"result.txt": b"x"}, {}),
            loader=missing_hash,
        )


@pytest.mark.parametrize(
    ("s3", "loader", "message"),
    [
        (FakeS3([], content=b"changed"), None, "hash mismatch"),
        (FakeS3([], returned_version="wrong"), None, "wrong version"),
        (FakeS3([], content="not-a-body"), None, "Unable to read"),
        (FakeS3([]), _loader([], hash_type="unknown"), "Unsupported Quilt"),
    ],
)
def test_runner_fails_closed_on_input_integrity(
    s3: FakeS3,
    loader: Callable[[PinnedManifestURI], Sequence[ManifestObject]] | None,
    message: str,
) -> None:
    with pytest.raises(InputIntegrityError, match=message):
        _execute(
            handler=lambda *_: SceneHandlerResult({"result.txt": b"x"}, {}),
            s3=s3,
            loader=loader,
        )
    assert not s3.put_requests


@pytest.mark.parametrize(
    ("handler_result", "message"),
    [
        ({"objects": {}}, "SceneHandlerResult"),
        (SceneHandlerResult({}, {}), "at least one"),
        (SceneHandlerResult({"../escape": b"x"}, {}), "unsafe"),
        (SceneHandlerResult({"run/scene.json": b"x"}, {}), "reserved"),
        (SceneHandlerResult({"result.bin": 3}, {}), "bytes or str"),  # type: ignore[dict-item]
        (SceneHandlerResult({"result.txt": b"x"}, []), "must be a mapping"),  # type: ignore[arg-type]
        (SceneHandlerResult({"result.txt": b"x"}, {"secret_access_key": "x"}), "credential"),
        (SceneHandlerResult({"result.txt": b"x"}, {"nested": ["temporary-token"]}), "credential"),
        (
            SceneHandlerResult(
                {"result.txt": b"x"},
                {"authorization": "Bearer temporary-token"},
            ),
            "credential",
        ),
        (SceneHandlerResult({"result.txt": b"x"}, {"value": float("nan")}), "JSON-serializable"),
    ],
)
def test_handler_contract_is_validated_before_publication(
    handler_result: object, message: str
) -> None:
    events: list[str] = []
    s3 = FakeS3(events)
    with pytest.raises(HandlerContractError, match=message):
        _execute(handler=lambda *_: handler_result, s3=s3)
    assert not s3.put_requests


def test_context_without_model_and_naive_clock_fail_closed() -> None:
    events: list[str] = []
    compilation = runner_module.compile(
        MANIFEST_URI,
        _scope(model=None),
        manifest_loader=_loader(events),
    )
    with pytest.raises(AuthorityModeError, match="does not select"):
        _ = SceneContext("scene-3", compilation).bedrock_model_arn

    times = iter([datetime(2026, 8, 19, 12), datetime(2026, 8, 19, 13)])
    with pytest.raises(ValueError, match="timezone-aware"):
        _execute(
            handler=lambda *_: SceneHandlerResult({"result.txt": b"x"}, {}),
            clock=lambda: next(times),
        )
