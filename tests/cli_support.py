"""Hand-written fakes for offline ``pearls`` command tests.

Every AWS seam the CLI exposes is filled here, so no test in this suite performs
a network call. Fakes record what was asked of them, which is how the tests
assert non-mutation and call ordering rather than trusting a return value.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from raja_sts.cli import Runtime
from raja_sts.models import ManifestObject, PinnedManifestURI

TOP_HASH = "b" * 64
GENESIS_URI = f"quilt+s3://genesis-bucket#package=proj%2Ftrial001-worker&top_hash={TOP_HASH}"
ROLE_ARN = "arn:aws:iam::123456789012:role/scene-execution-haiku"
MODEL_ARN = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"
ORCHESTRATOR_ARN = "arn:aws:iam::123456789012:user/orchestrator"
BUCKET_PATTERN = "arn:aws:s3:::quilt-dev-*"
INPUT_CONTENT = b"Assess the trial output against the rubric."
README = b"You are the Trial 001 worker Scene."
OUTPUTS_JSON = json.dumps(
    {
        "quilt+s3://output-bucket/result/#package=proj%2Ftrial001-worker-output": (
            "worker results"
        ),
    }
).encode()
HANDLER_SOURCE = b'''"""Sealed Trial 001 worker protocol."""

from raja_sts import SceneHandlerResult


def handler(inputs, session, context):
    return SceneHandlerResult(
        {"result/answer.txt": context.control_prompt or ""},
        {"input_count": len(inputs)},
    )
'''

TIMESTAMPS = [
    datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    datetime(2026, 8, 20, 12, 1, tzinfo=UTC),
    datetime(2026, 8, 20, 12, 2, tzinfo=UTC),
    datetime(2026, 8, 20, 12, 3, tzinfo=UTC),
]


class Body(io.BytesIO):
    """S3 body that records that the reader closed it."""

    was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


class MissingKey(Exception):
    """The absent-object error shape botocore raises."""

    def __init__(self, code: str = "NoSuchKey") -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def manifest_entry(logical_key: str, key: str, content: bytes) -> ManifestObject:
    """Build one versioned, content-hashed manifest entry."""
    return ManifestObject(
        logical_key=logical_key,
        bucket="genesis-bucket",
        key=key,
        version_id=f"version-{logical_key}",
        hash_type="SHA256",
        hash_value=hashlib.sha256(content).hexdigest(),
    )


class GenesisFixture:
    """One pinned genesis revision: its manifest entries and its object bytes."""

    def __init__(
        self,
        *,
        readme: bytes = README,
        outputs: bytes = OUTPUTS_JSON,
        handler_source: bytes | None = HANDLER_SOURCE,
        handler_key: str = "protocol/handler.py",
        corrupt_handler: bool = False,
    ) -> None:
        self.handler_key = handler_key
        self.contents: dict[str, bytes] = {
            "genesis/README.md": readme,
            "genesis/outputs.json": outputs,
            "genesis/input/prompt.txt": INPUT_CONTENT,
        }
        self.entries: list[ManifestObject] = [
            manifest_entry("README.md", "genesis/README.md", readme),
            manifest_entry("outputs.json", "genesis/outputs.json", outputs),
            ManifestObject(
                logical_key="input/prompt.txt",
                bucket="genesis-bucket",
                key="genesis/input/prompt.txt",
                version_id="version-input/prompt.txt",
                hash_type="SHA256",
                hash_value=hashlib.sha256(INPUT_CONTENT).hexdigest(),
            ),
        ]
        if handler_source is not None:
            physical = f"genesis/{handler_key}"
            self.contents[physical] = handler_source
            declared = b"different bytes" if corrupt_handler else handler_source
            self.entries.append(manifest_entry(handler_key, physical, declared))
        self.loads = 0

    def loader(self) -> Callable[[PinnedManifestURI], Sequence[ManifestObject]]:
        """Return a manifest loader that counts how often it resolved the revision."""

        def load(manifest: PinnedManifestURI) -> Sequence[ManifestObject]:
            self.loads += 1
            assert manifest.top_hash == TOP_HASH
            return tuple(self.entries)

        return load


class FakeGenesisS3:
    """Bootstrap-identity S3 reader for sealed genesis control objects."""

    def __init__(self, fixture: GenesisFixture) -> None:
        self.fixture = fixture
        self.get_requests: list[dict[str, Any]] = []
        self.other_calls: list[str] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_requests.append(kwargs)
        key = str(kwargs["Key"])
        if key not in self.fixture.contents:
            raise MissingKey()
        return {"Body": Body(self.fixture.contents[key]), "VersionId": kwargs.get("VersionId")}

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.other_calls.append("put_object")
        raise AssertionError("a read-only command must not write")

    @property
    def read_keys(self) -> list[str]:
        """Return the logical order of object keys this identity read."""
        return [str(request["Key"]) for request in self.get_requests]


class FakeWorkerS3:
    """Scene-credential S3 client: reads inputs and publishes the output package."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.get_requests: list[dict[str, Any]] = []
        self.put_requests: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs.get("Key", ""))
        if key.startswith(".quilt/workflows/"):
            raise MissingKey()
        self.events.append("worker-get")
        self.get_requests.append(kwargs)
        return {"Body": Body(INPUT_CONTENT), "VersionId": kwargs.get("VersionId")}

    def get_bucket_versioning(self, **kwargs: Any) -> dict[str, str]:
        self.events.append("versioning")
        return {"Status": "Enabled"}

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.events.append(f"put:{kwargs['Key']}")
        self.put_requests.append(kwargs)
        return {"VersionId": f"version-{len(self.put_requests)}"}


class FakeSTS:
    """AssumeRole that returns fixed, obviously fake credentials."""

    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append("assume_role")
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return {
            "Credentials": {
                "AccessKeyId": "temporary-access",
                "SecretAccessKey": "temporary-secret",
                "SessionToken": "temporary-token",
                "Expiration": datetime(2026, 8, 20, 13, tzinfo=UTC),
            },
            "AssumedRoleUser": {
                "Arn": f"arn:aws:sts::123456789012:assumed-role/scene-execution-haiku/"
                f"{kwargs['RoleSessionName']}",
                "AssumedRoleId": f"AROAEXAMPLE:{kwargs['RoleSessionName']}",
            },
            "PackedPolicySize": 31,
        }


class FakeSession:
    """Session that hands out recorded per-service fakes."""

    def __init__(
        self,
        clients: Mapping[str, Any],
        *,
        region_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.clients = dict(clients)
        self.region_name = region_name
        self.kwargs = kwargs
        self.factory_kwargs: dict[str, Any] = {}
        self.requested: list[str] = []

    def client(self, service: str, **_: Any) -> Any:
        self.requested.append(service)
        try:
            return self.clients[service]
        except KeyError as error:
            raise AssertionError(f"unexpected client requested: {service}") from error


class FakeWorkerSession:
    """Scene session: S3 for inputs and publication, Bedrock for invocation."""

    def __init__(self, s3: FakeWorkerS3, events: list[str], **kwargs: Any) -> None:
        self.s3 = s3
        self.events = events
        self.kwargs = kwargs

    def client(self, service: str, **_: Any) -> Any:
        self.events.append(f"worker-client:{service}")
        if service == "s3":
            return self.s3
        raise AssertionError(f"unexpected worker client: {service}")


class Recorder:
    """Counts calls to a delegate so tests can assert 'exactly once'."""

    def __init__(self, delegate: Callable[..., Any]) -> None:
        self.delegate = delegate
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.delegate(*args, **kwargs)

    @property
    def kwargs(self) -> dict[str, Any]:
        """Return the keyword arguments of the single recorded call."""
        assert len(self.calls) == 1, f"expected exactly one call, got {len(self.calls)}"
        return self.calls[0][1]


class Streams:
    """Captured stdout and stderr."""

    def __init__(self) -> None:
        self.out = io.StringIO()
        self.err = io.StringIO()

    @property
    def stdout(self) -> str:
        return self.out.getvalue()

    @property
    def stderr(self) -> str:
        return self.err.getvalue()

    def json(self) -> Any:
        """Parse stdout as the command's JSON document."""
        return json.loads(self.stdout)


def clock_from(values: Sequence[datetime] = tuple(TIMESTAMPS)) -> Callable[[], datetime]:
    """Return a deterministic clock that advances through ``values``."""
    remaining = iter(values)
    last = values[-1]

    def clock() -> datetime:
        nonlocal last
        try:
            last = next(remaining)
        except StopIteration:
            pass
        return last

    return clock


def make_runtime(
    *,
    streams: Streams,
    session: Any,
    manifest_loader: Callable[..., Any] | None = None,
    worker_session: Any = None,
    answers: Sequence[str] = (),
    **overrides: Any,
) -> Runtime:
    """Build a fully offline :class:`Runtime`."""
    replies = iter(answers)

    def prompt(_: str) -> str:
        try:
            return next(replies)
        except StopIteration as error:
            raise EOFError("no answer available") from error

    def build_worker(**kwargs: Any) -> Any:
        if worker_session is None:
            raise AssertionError("this command must not build a Scene session")
        # Record how the runner asked for the Scene session, so tests can prove
        # the operator's profile never reaches it.
        worker_session.kwargs = kwargs
        return worker_session

    def build_operator(**kwargs: Any) -> Any:
        # Record how the command asked for the operator session, so tests can
        # prove --profile and --region are actually delivered.
        session.factory_kwargs = kwargs
        return session

    return Runtime(
        stdout=streams.out,
        stderr=streams.err,
        session_factory=build_operator,
        worker_session_factory=build_worker,
        prompt=prompt,
        clock=clock_from(),
        sleep=lambda _: None,
        manifest_loader=manifest_loader,
        **overrides,
    )
