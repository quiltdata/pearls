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
    RUNTIME_REQUIREMENTS_MAX_BYTES,
    RUNTIME_REQUIREMENTS_SCHEMA,
    AuthorityModeError,
    HandlerContractError,
    InputIntegrityError,
    InvalidScopeError,
    ManifestLoadError,
    ManifestObject,
    PublicationContext,
    PublicationError,
    RuntimeRequirementsError,
    SceneContext,
    SceneHandlerResult,
    run_scene,
    run_scene_from_genesis,
)
from raja_sts.models import PinnedManifestURI

MODEL_ARN = "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0"
ROLE_ARN = "arn:aws:iam::123456789012:role/scene-execution"
CONTENT = b"Tell me hello."
RUNTIME_REQUIREMENTS = json.dumps(
    {
        "$schema": RUNTIME_REQUIREMENTS_SCHEMA,
        "python": ">=3.11",
        "imports": [],
    },
    sort_keys=True,
).encode()


class _NoWorkflowsConfig(Exception):
    def __init__(self) -> None:
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


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
        self.versioning_requests: list[dict[str, Any]] = []
        self.put_requests: list[dict[str, Any]] = []
        self.body: Body | None = None

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs.get("Key", ""))
        if key.startswith((".quilt/workflows/", ".quilt/named_packages/")):
            raise _NoWorkflowsConfig()
        self.events.append("get")
        self.get_requests.append(kwargs)
        self.body = Body(self.content) if isinstance(self.content, bytes) else self.content  # type: ignore[assignment]
        return {"Body": self.body, "VersionId": self.returned_version}

    def get_bucket_versioning(self, **kwargs: Any) -> dict[str, str]:
        self.events.append("get_bucket_versioning")
        self.versioning_requests.append(kwargs)
        return {"Status": "Enabled"}

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
    **run_scene_kwargs: Any,
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
        **run_scene_kwargs,
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
    assert s3.versioning_requests == [{"Bucket": "output-bucket"}]
    assert keys[:4] == [
        "project/scene-output/result/prompt.txt",
        "project/scene-output/result/response.txt",
        "project/scene-output/run/invocation.json",
        "project/scene-output/run/scene.json",
    ]
    assert all("IfNoneMatch" not in request for request in s3.put_requests[:-2])
    assert s3.put_requests[-2]["IfNoneMatch"] == "*"
    assert s3.put_requests[-1]["IfNoneMatch"] == "*"
    assert keys[-1] == ".quilt/named_packages/project/scene-output/latest"
    assert keys[-3].startswith(".quilt/packages/")
    assert keys[-2] == ".quilt/named_packages/project/scene-output/scene-3"

    by_key = {request["Key"]: request["Body"] for request in s3.put_requests}
    scene_record = json.loads(by_key["project/scene-output/run/scene.json"])
    invocation_body = by_key["project/scene-output/run/invocation.json"]
    invocation = json.loads(invocation_body)
    assert scene_record["$schema"].endswith("/v1")
    assert scene_record["scene_id"] == "scene-3"
    assert scene_record["input"]["top_hash"] == "a" * 64
    assert scene_record["input"]["selected_objects"][0]["version_id"] == "input-version"
    assert scene_record["authority"]["bedrock_model_arn"] == MODEL_ARN
    assert scene_record["assumed_role"]["id"] == "AROATEST:scene-3"
    assert invocation["request_id"] == "request-1"
    assert invocation_body.endswith(b"\n")
    assert (
        invocation_body
        == (
            json.dumps(invocation, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
    )
    serialized_records = json.dumps([scene_record, invocation])
    assert "temporary-access" not in serialized_records
    assert "temporary-secret" not in serialized_records
    assert "temporary-token" not in serialized_records

    assert result.top_hash == keys[-3].rsplit("/", 1)[-1]
    assert result.runtime_requirements is None
    assert result.publication.revision_id == "scene-3"
    assert result.publication.status == "published"
    assert result.publication.is_latest
    assert result.assumed_role_arn.endswith("/scene-3")
    assert result.package_uri.endswith(f"&top_hash={result.top_hash}")
    manifest = by_key[keys[-3]].decode().splitlines()
    assert json.loads(manifest[0]) == {"version": "v0"}
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
        (
            SceneHandlerResult(
                {"result.txt": b"x"},
                {"a": 1, 2: "b"},  # type: ignore[dict-item]
            ),
            "JSON-serializable",
        ),
        (SceneHandlerResult({"result.txt": b"x"}, {"value": float("nan")}), "JSON-serializable"),
        (
            SceneHandlerResult({"result.txt": b"x"}, {}, output_meta=[]),  # type: ignore[arg-type]
            "output metadata must be a mapping",
        ),
        (
            SceneHandlerResult({"result.txt": b"x"}, {}, output_meta={"session_token": "x"}),
            "output metadata contains credential",
        ),
        (
            SceneHandlerResult({"result.txt": b"x"}, {}, output_meta={"temporary-token": "label"}),
            "output metadata contains credential",
        ),
        (
            SceneHandlerResult({"result.txt": b"x"}, {}, output_meta={"score": float("nan")}),
            "output metadata must be JSON-serializable",
        ),
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


class GenesisS3:
    def __init__(self, events: list[str], contents: Mapping[str, bytes]) -> None:
        self.events = events
        self.contents = contents
        self.requests: list[dict[str, Any]] = []
        self.returned_versions: dict[str, str] = {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        self.events.append(f"control:{key}")
        self.requests.append(kwargs)
        return {
            "Body": Body(self.contents[key]),
            "VersionId": self.returned_versions.get(key, str(kwargs["VersionId"])),
        }


def _runtime_requirements(
    *,
    python: str = ">=3.11",
    imports: Sequence[Mapping[str, str]] = (),
) -> bytes:
    return json.dumps(
        {
            "$schema": RUNTIME_REQUIREMENTS_SCHEMA,
            "python": python,
            "imports": list(imports),
        },
        sort_keys=True,
    ).encode()


def _genesis_loader(
    events: list[str],
    *,
    readme: bytes = b"Follow the sealed instructions.",
    outputs: bytes | None = None,
    requirements: bytes = RUNTIME_REQUIREMENTS,
    corrupt_requirements: bool = False,
) -> tuple[Callable[[PinnedManifestURI], Sequence[ManifestObject]], GenesisS3]:
    outputs_content = (
        outputs
        or json.dumps(
            {"quilt+s3://output-bucket/result/#package=project%2Fscene-output": "generated results"}
        ).encode()
    )
    contents = {
        "genesis/README.md": readme,
        "genesis/outputs.json": outputs_content,
        "genesis/protocol/requirements.json": requirements,
    }

    def item(
        logical_key: str,
        key: str,
        content: bytes,
        *,
        declared_content: bytes | None = None,
    ) -> ManifestObject:
        return ManifestObject(
            logical_key=logical_key,
            bucket="manifest-bucket",
            key=key,
            version_id=f"version-{logical_key}",
            hash_type="SHA256",
            hash_value=hashlib.sha256(declared_content or content).hexdigest(),
        )

    def load(manifest: PinnedManifestURI) -> Sequence[ManifestObject]:
        events.append("genesis-load")
        assert manifest.original == MANIFEST_URI
        declared_requirements = b"different bytes" if corrupt_requirements else None
        return (
            item("README.md", "genesis/README.md", readme),
            item("outputs.json", "genesis/outputs.json", outputs_content),
            item(
                "protocol/requirements.json",
                "genesis/protocol/requirements.json",
                requirements,
                declared_content=declared_requirements,
            ),
            ManifestObject(
                logical_key="input/prompt.txt",
                bucket="input-bucket",
                key="sealed/prompt.txt",
                version_id="input-version",
                hash_type="SHA256",
                hash_value=hashlib.sha256(CONTENT).hexdigest(),
            ),
        )

    return load, GenesisS3(events, contents)


def _execute_genesis(
    handler: Callable[[Mapping[str, Any], Any, SceneContext], Any],
    *,
    outputs: bytes | None = None,
) -> tuple[Any, list[str], FakeS3, FakeSTS, GenesisS3]:
    events: list[str] = []
    loader, genesis_s3 = _genesis_loader(events, outputs=outputs)
    worker_s3 = FakeS3(events)
    sts = FakeSTS(events)
    session = FakeSession(events, worker_s3)
    times = iter(
        [
            datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 19, 12, 1, tzinfo=UTC),
        ]
    )
    result = run_scene_from_genesis(
        MANIFEST_URI,
        role_arn=ROLE_ARN,
        scene_id="scene-genesis",
        handler=handler,
        bedrock_model=MODEL_ARN,
        manifest_loader=loader,
        genesis_s3_client=genesis_s3,
        sts_client=sts,
        session_factory=lambda **_: session,
        clock=lambda: next(times),
    )
    return result, events, worker_s3, sts, genesis_s3


def test_run_scene_from_genesis_derives_prompt_read_and_write_scope() -> None:
    contexts: list[SceneContext] = []

    def handler(
        inputs: Mapping[str, Any], session: FakeSession, context: SceneContext
    ) -> SceneHandlerResult:
        contexts.append(context)
        session.events.append("handler")
        assert list(inputs) == ["input/prompt.txt"]
        return SceneHandlerResult({"result/answer.txt": "Hello."}, {"source": "genesis"})

    result, events, worker_s3, sts, genesis_s3 = _execute_genesis(handler)

    assert events.count("genesis-load") == 1
    assert events.index("genesis-load") < events.index("control:genesis/README.md")
    assert events.index("control:genesis/protocol/requirements.json") < events.index("mint")
    assert events.index("mint") < events.index("handler")
    assert contexts[0].control_prompt == "Follow the sealed instructions."
    assert [request["Key"] for request in genesis_s3.requests] == [
        "genesis/README.md",
        "genesis/outputs.json",
        "genesis/protocol/requirements.json",
    ]
    assert all(request["VersionId"] for request in genesis_s3.requests)
    assert result.runtime_requirements is not None
    assert result.runtime_requirements.descriptor.logical_key == "protocol/requirements.json"
    assert (
        result.runtime_requirements.source_sha256
        == hashlib.sha256(RUNTIME_REQUIREMENTS).hexdigest()
    )
    assert result.runtime_requirements.source_size == len(RUNTIME_REQUIREMENTS)
    assert result.runtime_requirements.compatibility.checks == ()
    assert worker_s3.get_requests == [
        {"Bucket": "input-bucket", "Key": "sealed/prompt.txt", "VersionId": "input-version"}
    ]
    policy = json.loads(sts.requests[0]["Policy"])
    write_boundary = next(
        statement
        for statement in policy["Statement"]
        if statement.get("Effect") == "Deny" and statement.get("Action") == "s3:PutObject"
    )
    assert (
        "arn:aws:s3:::output-bucket/project/scene-output/result/*" in write_boundary["NotResource"]
    )
    assert "arn:aws:s3:::output-bucket/project/scene-output/*" not in write_boundary["NotResource"]
    write_allow = next(
        statement
        for statement in policy["Statement"]
        if statement.get("Effect") == "Allow" and statement.get("Action") == "s3:PutObject"
    )
    assert write_allow["Resource"] == "*"
    assert result.output_package.name == "project/scene-output"


def test_genesis_write_scope_rejects_handler_output_before_publication() -> None:
    events: list[str] = []
    loader, genesis_s3 = _genesis_loader(events)
    worker_s3 = FakeS3(events)
    session = FakeSession(events, worker_s3)
    with pytest.raises(HandlerContractError, match="outside the genesis write scope"):
        run_scene_from_genesis(
            MANIFEST_URI,
            role_arn=ROLE_ARN,
            scene_id="scene-genesis",
            handler=lambda *_: SceneHandlerResult({"other/answer.txt": "no"}, {}),
            bedrock_model=MODEL_ARN,
            manifest_loader=loader,
            genesis_s3_client=genesis_s3,
            sts_client=FakeSTS(events),
            session_factory=lambda **_: session,
        )
    assert not worker_s3.put_requests


@pytest.mark.parametrize(
    ("objects", "message"),
    [
        ((), "missing required"),
        (
            (
                ManifestObject("README.md", "bucket-name", "readme"),
                ManifestObject("outputs.json", "bucket-name", "outputs"),
                ManifestObject(
                    "protocol/requirements.json",
                    "bucket-name",
                    "requirements",
                ),
            ),
            "input/",
        ),
        (
            (
                ManifestObject("README.md", "bucket-name", "one"),
                ManifestObject("README.md", "bucket-name", "two"),
            ),
            "duplicate",
        ),
    ],
)
def test_genesis_rejects_incomplete_or_duplicate_layouts(
    objects: Sequence[ManifestObject], message: str
) -> None:
    with pytest.raises(ManifestLoadError, match=message):
        run_scene_from_genesis(
            MANIFEST_URI,
            role_arn=ROLE_ARN,
            scene_id="scene-genesis",
            handler=lambda *_: SceneHandlerResult({"result/a": "x"}, {}),
            bedrock_model=MODEL_ARN,
            manifest_loader=lambda _: objects,
        )


@pytest.mark.parametrize("outputs", [b"not json", b"[]"])
def test_genesis_rejects_invalid_outputs_json(outputs: bytes) -> None:
    events: list[str] = []
    loader, genesis_s3 = _genesis_loader(events, outputs=outputs)
    with pytest.raises(InvalidScopeError, match="outputs.json"):
        run_scene_from_genesis(
            MANIFEST_URI,
            role_arn=ROLE_ARN,
            scene_id="scene-genesis",
            handler=lambda *_: SceneHandlerResult({"result/a": "x"}, {}),
            bedrock_model=MODEL_ARN,
            manifest_loader=loader,
            genesis_s3_client=genesis_s3,
        )


def test_incompatible_runtime_requirements_refuse_before_mint_or_execution() -> None:
    events: list[str] = []
    loader, genesis_s3 = _genesis_loader(
        events,
        requirements=_runtime_requirements(python="<0"),
    )
    worker_s3 = FakeS3(events)
    sts = FakeSTS(events)
    handler_calls: list[str] = []

    with pytest.raises(RuntimeRequirementsError, match="Python"):
        run_scene_from_genesis(
            MANIFEST_URI,
            role_arn=ROLE_ARN,
            scene_id="scene-genesis",
            handler=lambda *_: handler_calls.append("handler"),
            bedrock_model=MODEL_ARN,
            manifest_loader=loader,
            genesis_s3_client=genesis_s3,
            sts_client=sts,
            session_factory=lambda **_: (_ for _ in ()).throw(
                AssertionError("worker session must not be created")
            ),
        )

    assert [request["Key"] for request in genesis_s3.requests] == [
        "genesis/README.md",
        "genesis/outputs.json",
        "genesis/protocol/requirements.json",
    ]
    assert sts.requests == []
    assert worker_s3.get_requests == []
    assert worker_s3.put_requests == []
    assert handler_calls == []
    assert "mint" not in events


@pytest.mark.parametrize("failure", ["hash", "version", "size"])
def test_runtime_requirements_are_read_by_exact_version_hash_and_size(
    failure: str,
) -> None:
    events: list[str] = []
    requirements = (
        b" " * (RUNTIME_REQUIREMENTS_MAX_BYTES + 1) if failure == "size" else RUNTIME_REQUIREMENTS
    )
    loader, genesis_s3 = _genesis_loader(
        events,
        requirements=requirements,
        corrupt_requirements=failure == "hash",
    )
    if failure == "version":
        genesis_s3.returned_versions["genesis/protocol/requirements.json"] = "wrong-version"
    sts = FakeSTS(events)

    with pytest.raises(InputIntegrityError, match="hash mismatch|wrong version|exceeds"):
        run_scene_from_genesis(
            MANIFEST_URI,
            role_arn=ROLE_ARN,
            scene_id="scene-genesis",
            handler=lambda *_: SceneHandlerResult({"result/a": "x"}, {}),
            bedrock_model=MODEL_ARN,
            manifest_loader=loader,
            genesis_s3_client=genesis_s3,
            sts_client=sts,
        )

    request = genesis_s3.requests[-1]
    assert request == {
        "Bucket": "manifest-bucket",
        "Key": "genesis/protocol/requirements.json",
        "VersionId": "version-protocol/requirements.json",
    }
    assert sts.requests == []
    assert "mint" not in events


def test_run_scene_merges_handler_output_meta_before_publication() -> None:
    result, _events, fake_s3, _sts, _session, _kwargs = _execute(
        handler=lambda inputs, session, context: SceneHandlerResult(
            objects={"out.txt": b"ok"},
            invocation={"model": "m"},
            output_meta={"pass_count": 7},
        ),
        output_meta={"trial": 1, "limitations": "offline test"},
        output_message="threaded through",
    )
    manifest_put = next(
        request
        for request in fake_s3.put_requests
        if request["Key"] == f".quilt/packages/{result.top_hash}"
    )
    header = json.loads(manifest_put["Body"].decode().splitlines()[0])
    assert header["user_meta"] == {
        "trial": 1,
        "limitations": "offline test",
        "pass_count": 7,
    }
    assert header["message"] == "threaded through"
    assert "workflow" not in header
    assert result.publication.metadata_keys == ("limitations", "pass_count", "trial")


def test_handler_output_meta_collision_refuses_before_publication() -> None:
    events: list[str] = []
    s3 = FakeS3(events)

    with pytest.raises(HandlerContractError, match="collides.*'trial'"):
        _execute(
            handler=lambda *_: SceneHandlerResult({"out.txt": b"ok"}, {}, output_meta={"trial": 2}),
            s3=s3,
            output_meta={"trial": 1},
        )

    assert not s3.put_requests


@pytest.mark.parametrize(
    "output_meta",
    [
        {"nested": "temporary-token"},
        {"temporary-token": "label"},
    ],
)
def test_caller_output_meta_is_secret_screened_before_publication(
    output_meta: Mapping[str, Any],
) -> None:
    events: list[str] = []
    s3 = FakeS3(events)

    with pytest.raises(PublicationError, match="credential material"):
        _execute(
            handler=lambda *_: SceneHandlerResult({"out.txt": b"ok"}, {}),
            s3=s3,
            output_meta=output_meta,
        )

    assert not s3.put_requests


class _CapturingInvariant:
    identifier = "runner-final-material/v1"

    def __init__(self) -> None:
        self.contexts: list[PublicationContext] = []

    def check(self, context: PublicationContext, /) -> Sequence[str]:
        self.contexts.append(context)
        return ()


def test_output_invariant_sees_merged_metadata_and_runner_owned_records() -> None:
    checker = _CapturingInvariant()

    result, _events, _s3, _sts, _session, _kwargs = _execute(
        handler=lambda *_: SceneHandlerResult({"out.txt": b"ok"}, {}, output_meta={"derived": 2}),
        output_meta={"declared": 1},
        output_invariants=(checker,),
    )

    assert result.publication.status == "published"
    assert len(checker.contexts) == 1
    context = checker.contexts[0]
    assert context.user_meta == {"declared": 1, "derived": 2}
    assert set(context.objects) == {
        "out.txt",
        "run/invocation.json",
        "run/scene.json",
    }


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


def test_run_scene_uses_selected_profile_for_model_id_region_and_report() -> None:
    contexts: list[SceneContext] = []

    def handler(
        inputs: Mapping[str, Any], session: FakeSession, context: SceneContext
    ) -> SceneHandlerResult:
        contexts.append(context)
        session.client("bedrock-runtime").converse(
            modelId=context.bedrock_model_id,
            messages=[],
        )
        return SceneHandlerResult({"result.txt": "ok"}, {"model": context.bedrock_model_id})

    scope = _scope(model=PROFILE_ARN)
    scope["bedrock_invoke_resources"] = [
        PROFILE_MODEL_WEST,
        PROFILE_ARN,
        PROFILE_MODEL_EAST,
    ]
    _result, _events, s3, sts, session, session_kwargs = _execute(
        handler=handler,
        scope=scope,
        bedrock_profile_loader=lambda arn: (
            (
                PROFILE_MODEL_EAST,
                PROFILE_MODEL_WEST,
            )
            if arn == PROFILE_ARN
            else ()
        ),
    )

    assert session_kwargs["region_name"] == "us-east-1"
    assert session.bedrock.requests[0]["modelId"] == PROFILE_ARN
    assert contexts[0].bedrock_model_id == PROFILE_ARN
    assert contexts[0].bedrock_model_arn == PROFILE_ARN
    assert contexts[0].bedrock_invoke_resources == (
        PROFILE_ARN,
        PROFILE_MODEL_EAST,
        PROFILE_MODEL_WEST,
    )
    policy = json.loads(sts.requests[0]["Policy"])
    model_statements = [
        statement
        for statement in policy["Statement"]
        if statement.get("Action") == "bedrock:InvokeModel"
    ]
    assert model_statements == [
        {
            "Action": "bedrock:InvokeModel",
            "Effect": "Allow",
            "Resource": PROFILE_ARN,
        },
        {
            "Action": "bedrock:InvokeModel",
            "Condition": {
                "StringLike": {
                    "bedrock:InferenceProfileArn": PROFILE_ARN,
                }
            },
            "Effect": "Allow",
            "Resource": [PROFILE_MODEL_EAST, PROFILE_MODEL_WEST],
        },
    ]
    scene_put = next(
        request for request in s3.put_requests if request["Key"].endswith("run/scene.json")
    )
    authority = json.loads(scene_put["Body"])["authority"]
    assert authority["bedrock"]["selected_model_id"] == PROFILE_ARN
    assert authority["bedrock"]["invoke_resources"] == [
        PROFILE_ARN,
        PROFILE_MODEL_EAST,
        PROFILE_MODEL_WEST,
    ]
