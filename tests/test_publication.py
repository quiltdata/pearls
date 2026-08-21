import hashlib
import io
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import quilt3.packages as quilt_packages
from botocore.exceptions import ClientError
from quilt3 import Package
from quilt3.telemetry import ApiTelemetry
from quilt3.util import QuiltException

from raja_sts import (
    DomainInvariantError,
    PublicationConflictError,
    PublicationContext,
    PublicationError,
    WorkflowContractError,
)
from raja_sts.models import OutputPackage
from raja_sts.publication import publish_package


@dataclass(frozen=True)
class _StoredObject:
    body: bytes
    version_id: str
    etag: str
    metadata: Mapping[str, str]


class RecordingS3:
    """A versioned current-object store with real conditional-put behavior."""

    def __init__(
        self,
        responses: list[dict[str, Any]] | None = None,
        *,
        versioning_status: str | None = "Enabled",
        fail_suffix: str | None = None,
    ) -> None:
        self.versioning_requests: list[dict[str, Any]] = []
        self.get_requests: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.responses = iter(responses or [])
        self.versioning_status = versioning_status
        self.fail_suffix = fail_suffix
        self.current: dict[str, _StoredObject] = {}

    def seed(
        self,
        key: str,
        body: bytes,
        *,
        version_id: str = "seed-version",
        etag: str = '"seed-etag"',
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        self.current[key] = _StoredObject(body, version_id, etag, dict(metadata or {}))

    def get_bucket_versioning(self, **kwargs: Any) -> dict[str, str]:
        self.versioning_requests.append(kwargs)
        return {"Status": self.versioning_status} if self.versioning_status else {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_requests.append(kwargs)
        stored = self.current.get(str(kwargs["Key"]))
        if stored is None:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "absent"}},
                "GetObject",
            )
        return {
            "Body": io.BytesIO(stored.body),
            "VersionId": stored.version_id,
            "ETag": stored.etag,
            "Metadata": dict(stored.metadata),
        }

    @staticmethod
    def _precondition_failed() -> ClientError:
        return ClientError(
            {
                "Error": {"Code": "PreconditionFailed", "Message": "condition failed"},
                "ResponseMetadata": {"HTTPStatusCode": 412},
            },
            "PutObject",
        )

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        key = str(kwargs["Key"])
        current = self.current.get(key)
        if self.fail_suffix is not None and key.endswith(self.fail_suffix):
            raise self._precondition_failed()
        if kwargs.get("IfNoneMatch") == "*" and current is not None:
            raise self._precondition_failed()
        if "IfMatch" in kwargs and (current is None or kwargs["IfMatch"] != current.etag):
            raise self._precondition_failed()

        response = next(self.responses, {"VersionId": f"version-{len(self.requests)}"})
        version_id = response.get("VersionId")
        if isinstance(version_id, str) and version_id and version_id != "null":
            body = kwargs["Body"]
            assert isinstance(body, bytes)
            etag = response.get("ETag")
            if not isinstance(etag, str) or not etag:
                etag = f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'  # noqa: S324
            metadata = kwargs.get("Metadata", {})
            assert isinstance(metadata, Mapping)
            self.current[key] = _StoredObject(body, version_id, etag, dict(metadata))
        return response


def _publish(
    s3: RecordingS3,
    *,
    revision_id: str = "scene-123",
    output: OutputPackage | None = None,
    objects: Mapping[str, bytes] | None = None,
    timestamp: datetime = datetime(2026, 8, 19, 12, tzinfo=UTC),
    **kwargs: Any,
) -> Any:
    return publish_package(
        s3_client=s3,
        output=output or OutputPackage("project/output", "output-bucket"),
        objects=objects if objects is not None else {"result.txt": b"result"},
        timestamp=timestamp,
        revision_id=revision_id,
        **kwargs,
    )


def test_publish_package_uses_quilt_canonical_manifest_without_ambient_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_dump = Package._dump
    dump_calls: list[Package] = []

    def record_dump(package: Package, writable_file: Any) -> None:
        dump_calls.append(package)
        real_dump(package, writable_file)

    def forbid_ambient_io(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("publication attempted ambient Quilt I/O")

    monkeypatch.setattr(Package, "_dump", record_dump)
    monkeypatch.setattr(ApiTelemetry, "report_api_use", forbid_ambient_io)
    for name in (
        "get_size_and_version",
        "get_bytes",
        "put_bytes",
        "list_url",
        "copy_file",
        "copy_file_list",
    ):
        monkeypatch.setattr(quilt_packages, name, forbid_ambient_io)

    s3 = RecordingS3([{"VersionId": "v 1"}])
    publication = _publish(
        s3,
        objects={"z.txt": b"z", "dir/a b.txt": b"alpha"},
    )
    top_hash = publication.top_hash

    assert s3.versioning_requests == [{"Bucket": "output-bucket"}]
    assert len(dump_calls) == 1
    keys = [request["Key"] for request in s3.requests]
    assert keys[:2] == ["project/output/dir/a b.txt", "project/output/z.txt"]
    assert keys[2] == f".quilt/packages/{top_hash}"
    assert keys[3] == ".quilt/named_packages/project/output/scene-123"
    assert keys[4] == ".quilt/named_packages/project/output/latest"
    assert "IfNoneMatch" not in s3.requests[0]
    assert s3.requests[3]["IfNoneMatch"] == "*"
    assert s3.requests[4]["IfNoneMatch"] == "*"
    assert s3.requests[2]["ContentType"] == "application/jsonl"
    assert s3.requests[3]["Body"] == s3.requests[4]["Body"] == top_hash.encode()
    assert publication.status == "published"
    assert publication.is_latest
    assert not publication.reused_revision
    assert publication.write_disposition == "package-writes"

    manifest = s3.requests[2]["Body"]
    records = [json.loads(line) for line in manifest.decode().splitlines()]
    assert records[0] == {"version": "v0"}
    assert records[1]["logical_key"] == "dir/a b.txt"
    assert records[1]["physical_keys"] == [
        "s3://output-bucket/project/output/dir/a%20b.txt?versionId=v+1"
    ]
    assert records[2]["physical_keys"] == [
        "s3://output-bucket/project/output/z.txt?versionId=version-2"
    ]

    loaded = Package._load(io.StringIO(manifest.decode()))
    assert loaded.top_hash == top_hash
    assert [(key, entry.size, str(entry.physical_key)) for key, entry in loaded.walk()] == [
        (
            "dir/a b.txt",
            5,
            "s3://output-bucket/project/output/dir/a%20b.txt?versionId=v+1",
        ),
        (
            "z.txt",
            1,
            "s3://output-bucket/project/output/z.txt?versionId=version-2",
        ),
    ]
    round_trip = io.BytesIO()
    real_dump(loaded, round_trip)
    assert round_trip.getvalue() == manifest


def test_quilt_loaded_manifest_hashes_verify_published_objects() -> None:
    s3 = RecordingS3()
    bodies = {"a.txt": b"alpha", "b.txt": b"beta"}
    publication = _publish(s3, objects=bodies)
    loaded = Package._load(io.StringIO(s3.requests[2]["Body"].decode()))

    assert loaded.top_hash == publication.top_hash
    for logical_key, entry in loaded.walk():
        entry._verify_hash(bodies[logical_key])
    with pytest.raises(QuiltException, match="Hash validation failed"):
        loaded["a.txt"]._verify_hash(b"changed")


@pytest.mark.parametrize("status", [None, "Suspended"])
def test_publication_rejects_unversioned_bucket_before_writes(status: str | None) -> None:
    s3 = RecordingS3(versioning_status=status)

    with pytest.raises(PublicationError, match="must have versioning enabled"):
        _publish(s3)

    assert not s3.requests


def test_existing_latest_is_advanced_with_compare_and_swap() -> None:
    s3 = RecordingS3()
    latest_key = ".quilt/named_packages/project/output/latest"
    s3.seed(latest_key, b"a" * 64, etag='"parent-etag"')

    publication = _publish(s3)

    assert publication.status == "published"
    assert publication.previous_top_hash == "a" * 64
    assert publication.latest_top_hash == publication.top_hash
    assert s3.requests[-1]["IfMatch"] == '"parent-etag"'
    assert "IfNoneMatch" not in s3.requests[-1]
    assert s3.current[latest_key].body == publication.top_hash.encode()


def test_concurrent_latest_conflict_is_published_not_latest() -> None:
    s3 = RecordingS3(
        [
            {"VersionId": "loser-data-version"},
            {"VersionId": "loser-manifest-version"},
            {"VersionId": "loser-revision-version"},
        ],
        fail_suffix="/latest",
    )

    publication = _publish(s3)

    assert publication.status == "published-not-latest"
    assert not publication.is_latest
    assert publication.latest_top_hash is None
    assert s3.requests[-1]["Key"] == ".quilt/named_packages/project/output/latest"
    assert s3.requests[-1]["IfNoneMatch"] == "*"
    assert ".quilt/named_packages/project/output/scene-123" in s3.current
    manifest = json.loads(s3.requests[1]["Body"].decode().splitlines()[1])
    assert manifest["physical_keys"] == [
        "s3://output-bucket/project/output/result.txt?versionId=loser-data-version"
    ]


def test_same_second_new_invocations_create_distinct_revisions() -> None:
    s3 = RecordingS3()
    timestamp = datetime(2026, 8, 19, 12, tzinfo=UTC)

    first = _publish(s3, revision_id="scene-one", timestamp=timestamp)
    second = _publish(s3, revision_id="scene-two", timestamp=timestamp)

    assert first.revision_id == "scene-one"
    assert second.revision_id == "scene-two"
    assert first.revision_version_id != second.revision_version_id
    assert ".quilt/named_packages/project/output/scene-one" in s3.current
    assert ".quilt/named_packages/project/output/scene-two" in s3.current
    assert s3.current[".quilt/named_packages/project/output/latest"].body == (
        second.top_hash.encode()
    )


def test_same_revision_and_intent_is_an_idempotent_retry() -> None:
    s3 = RecordingS3()
    first = _publish(s3)
    write_count = len(s3.requests)

    retry = _publish(s3, timestamp=datetime(2026, 8, 20, 9, tzinfo=UTC))

    assert len(s3.requests) == write_count
    assert retry.top_hash == first.top_hash
    assert retry.revision_version_id == first.revision_version_id
    assert retry.reused_revision
    assert retry.status == "published"
    assert retry.write_disposition == "none"


def test_retry_after_interruption_finishes_only_original_latest_cas() -> None:
    s3 = RecordingS3(fail_suffix="/latest")
    interrupted = _publish(s3)
    write_count = len(s3.requests)
    assert interrupted.status == "published-not-latest"

    s3.fail_suffix = None
    recovered = _publish(s3)

    assert len(s3.requests) == write_count + 1
    assert recovered.top_hash == interrupted.top_hash
    assert recovered.reused_revision
    assert recovered.status == "published"
    assert recovered.is_latest
    assert recovered.write_disposition == "latest-only"


class _PeerWinsRecoveryLatestS3(RecordingS3):
    peer_wins_latest = False

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        if self.peer_wins_latest and key.endswith("/latest"):
            self.requests.append(kwargs)
            body = kwargs["Body"]
            assert isinstance(body, bytes)
            self.current[key] = _StoredObject(
                body,
                "peer-latest-version",
                f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"',  # noqa: S324
                {},
            )
            raise self._precondition_failed()
        return super().put_object(**kwargs)


def test_recovery_lost_to_same_revision_reports_no_invocation_write() -> None:
    s3 = _PeerWinsRecoveryLatestS3(fail_suffix="/latest")
    interrupted = _publish(s3)
    assert interrupted.status == "published-not-latest"

    s3.fail_suffix = None
    s3.peer_wins_latest = True
    retry = _publish(s3)

    assert retry.status == "published"
    assert retry.reused_revision
    assert retry.write_disposition == "none"
    assert retry.latest_top_hash == interrupted.top_hash


def test_retry_does_not_roll_an_advanced_latest_pointer_back() -> None:
    s3 = RecordingS3(fail_suffix="/latest")
    interrupted = _publish(s3)
    latest_key = ".quilt/named_packages/project/output/latest"
    s3.fail_suffix = None
    s3.seed(latest_key, b"f" * 64, version_id="newer-version", etag='"newer-etag"')
    write_count = len(s3.requests)

    retry = _publish(s3)

    assert len(s3.requests) == write_count
    assert retry.top_hash == interrupted.top_hash
    assert retry.status == "published-not-latest"
    assert retry.latest_top_hash == "f" * 64
    assert retry.write_disposition == "none"
    assert s3.current[latest_key].body == b"f" * 64


def test_declared_intent_is_canonical_and_bound_to_revision_identity() -> None:
    s3 = RecordingS3()
    first = _publish(
        s3,
        declared_intent={
            "$schema": "https://example.test/publication-intent/v1",
            "handler_key": "protocol/handler.py",
            "evidence": {"source": "one", "version": 1},
        },
    )
    write_count = len(s3.requests)

    retry = _publish(
        s3,
        declared_intent={
            "evidence": {"version": 1, "source": "one"},
            "handler_key": "protocol/handler.py",
            "$schema": "https://example.test/publication-intent/v1",
        },
    )

    assert retry.top_hash == first.top_hash
    assert retry.reused_revision
    assert len(s3.requests) == write_count

    with pytest.raises(PublicationConflictError, match="different publication intent"):
        _publish(
            s3,
            declared_intent={
                "$schema": "https://example.test/publication-intent/v1",
                "handler_key": "protocol/supervisor.py",
                "evidence": {"source": "one", "version": 1},
            },
        )

    assert len(s3.requests) == write_count


def test_same_revision_with_different_intent_refuses_before_writes() -> None:
    s3 = RecordingS3()
    _publish(s3)
    write_count = len(s3.requests)

    with pytest.raises(PublicationConflictError, match="different publication intent"):
        _publish(s3, objects={"result.txt": b"different"})

    assert len(s3.requests) == write_count


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        ([{}], "did not version output object 'project/output/result.txt'"),
        (
            [{"VersionId": "data-version"}, {}],
            "did not version output object '.quilt/packages/",
        ),
        (
            [{"VersionId": "data-version"}, {"VersionId": "manifest-version"}, {}],
            "did not version output object '.quilt/named_packages/project/output/scene-123'",
        ),
        (
            [
                {"VersionId": "data-version"},
                {"VersionId": "manifest-version"},
                {"VersionId": "revision-version"},
                {},
            ],
            "did not version output object '.quilt/named_packages/project/output/latest'",
        ),
    ],
)
def test_every_publication_write_must_return_a_version_id(
    responses: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(PublicationError, match=message):
        _publish(RecordingS3(responses))


def test_publish_package_wraps_write_failures() -> None:
    class BrokenS3:
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "absent"}},
                "GetObject",
            )

        def get_bucket_versioning(self, **kwargs: Any) -> dict[str, str]:
            return {"Status": "Enabled"}

        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("write denied")

    with pytest.raises(PublicationError, match="Unable to publish") as raised:
        publish_package(
            s3_client=BrokenS3(),
            output=OutputPackage("project/output", "output-bucket"),
            objects={"result.txt": b"result"},
            timestamp=datetime.now(UTC),
            revision_id="scene-123",
        )
    assert isinstance(raised.value.__cause__, RuntimeError)


GOVERNED_CONFIG = b"""
version: "1"
is_workflow_required: true
default_workflow: trial-run
workflows:
  trial-run:
    metadata_schema: trial-run-v1
    is_message_required: true
schemas:
  trial-run-v1:
    url: s3://output-bucket/.quilt/workflows/trial-run-v1.json
"""

GOVERNED_SCHEMA = json.dumps(
    {
        "type": "object",
        "required": ["trial"],
        "properties": {"trial": {"type": "integer"}},
    }
).encode()


class GovernedS3(RecordingS3):
    """RecordingS3 plus a workflow config and one metadata schema."""

    workflow_objects = {
        ".quilt/workflows/config.yml": (GOVERNED_CONFIG, "config-v1"),
        ".quilt/workflows/trial-run-v1.json": (GOVERNED_SCHEMA, "schema-v1"),
    }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        entry = self.workflow_objects.get(kwargs["Key"])
        if entry is None:
            return super().get_object(**kwargs)
        content, version = entry
        return {"Body": io.BytesIO(content), "VersionId": version}


def _publish_governed(s3: GovernedS3) -> Any:
    return publish_package(
        s3_client=s3,
        output=OutputPackage("trials/trial001", "output-bucket"),
        objects={"content/observations.csv": b"a,b\n"},
        timestamp=datetime(2026, 8, 20, 12, tzinfo=UTC),
        revision_id="scene-123",
        user_meta={"trial": 1},
        message="scene-native run",
        workflow="trial-run",
    )


def test_completed_retry_ignores_changed_workflow_when_no_write_is_needed() -> None:
    s3 = GovernedS3()
    first = _publish_governed(s3)
    write_count = len(s3.requests)
    s3.workflow_objects = {
        ".quilt/workflows/config.yml": (GOVERNED_CONFIG, "config-v2"),
        ".quilt/workflows/trial-run-v1.json": (
            b'{"type":"object","required":["replacement"]}',
            "schema-v2",
        ),
    }

    retry = _publish_governed(s3)

    assert len(s3.requests) == write_count
    assert retry.status == "published"
    assert retry.reused_revision
    assert retry.top_hash == first.top_hash
    assert retry.revision_version_id == first.revision_version_id


def test_status_only_retry_ignores_deleted_workflow_and_preserves_newer_latest() -> None:
    s3 = GovernedS3(fail_suffix="/latest")
    interrupted = _publish_governed(s3)
    latest_key = ".quilt/named_packages/trials/trial001/latest"
    s3.fail_suffix = None
    s3.seed(latest_key, b"f" * 64, version_id="newer-version", etag='"newer-etag"')
    s3.workflow_objects = {}
    write_count = len(s3.requests)

    retry = _publish_governed(s3)

    assert len(s3.requests) == write_count
    assert retry.status == "published-not-latest"
    assert retry.reused_revision
    assert retry.top_hash == interrupted.top_hash
    assert retry.latest_top_hash == "f" * 64
    assert s3.current[latest_key].body == b"f" * 64
    assert s3.current[latest_key].version_id == "newer-version"


class _SameIntentWinnerChangesWorkflowS3(GovernedS3):
    """Inject a same-intent revision winner and then reject its current contract."""

    def __init__(self) -> None:
        super().__init__()
        self.workflow_objects = dict(GovernedS3.workflow_objects)
        self.workflow_reads = 0
        self.injected_winner = False

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        if str(kwargs["Key"]).startswith(".quilt/workflows/"):
            self.workflow_reads += 1
        return super().get_object(**kwargs)

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        if (
            not self.injected_winner
            and key.endswith("/scene-123")
            and kwargs.get("IfNoneMatch") == "*"
        ):
            self.requests.append(kwargs)
            body = kwargs["Body"]
            metadata = kwargs["Metadata"]
            assert isinstance(body, bytes)
            assert isinstance(metadata, Mapping)
            self.current[key] = _StoredObject(
                body,
                "concurrent-revision-version",
                f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"',  # noqa: S324
                dict(metadata),
            )
            self.workflow_objects = {
                ".quilt/workflows/config.yml": (GOVERNED_CONFIG, "config-v2"),
                ".quilt/workflows/trial-run-v1.json": (
                    b'{"type":"object","required":["replacement"]}',
                    "schema-v2",
                ),
            }
            self.injected_winner = True
            raise self._precondition_failed()
        return super().put_object(**kwargs)


def test_same_intent_revision_winner_revalidates_before_advancing_latest() -> None:
    s3 = _SameIntentWinnerChangesWorkflowS3()

    with pytest.raises(WorkflowContractError, match="replacement"):
        _publish_governed(s3)

    assert s3.injected_winner
    assert s3.workflow_reads == 4
    assert not any(str(request["Key"]).endswith("/latest") for request in s3.requests)
    assert ".quilt/named_packages/trials/trial001/scene-123" in s3.current


def test_publish_into_governed_bucket_validates_and_stamps() -> None:
    s3 = GovernedS3()
    publication = publish_package(
        s3_client=s3,
        output=OutputPackage("trials/trial001", "output-bucket"),
        objects={"content/observations.csv": b"a,b\n"},
        timestamp=datetime(2026, 8, 20, 12, tzinfo=UTC),
        revision_id="scene-123",
        user_meta={"trial": 1},
        message="scene-native run",
    )

    manifest_request = next(
        request
        for request in s3.requests
        if request["Key"] == f".quilt/packages/{publication.top_hash}"
    )
    header = json.loads(manifest_request["Body"].decode().splitlines()[0])
    assert header["user_meta"] == {"trial": 1}
    assert header["message"] == "scene-native run"
    assert header["workflow"] == {
        "id": "trial-run",
        "config": "s3://output-bucket/.quilt/workflows/config.yml?versionId=config-v1",
        "schemas": {
            "trial-run-v1": (
                "s3://output-bucket/.quilt/workflows/trial-run-v1.json?versionId=schema-v1"
            )
        },
    }

    rebuilt = Package.load(io.BytesIO(manifest_request["Body"]))
    assert rebuilt.top_hash == publication.top_hash


def test_governed_refusal_happens_before_any_write() -> None:
    s3 = GovernedS3()
    with pytest.raises(WorkflowContractError, match="Package metadata failed validation"):
        publish_package(
            s3_client=s3,
            output=OutputPackage("trials/trial001", "output-bucket"),
            objects={"content/observations.csv": b"a,b\n"},
            timestamp=datetime(2026, 8, 20, 12, tzinfo=UTC),
            revision_id="scene-123",
            user_meta={},
            message="scene-native run",
        )
    assert s3.requests == []


def test_governed_bucket_refuses_explicit_no_workflow() -> None:
    s3 = GovernedS3()
    with pytest.raises(WorkflowContractError, match="Workflow required, but none specified."):
        publish_package(
            s3_client=s3,
            output=OutputPackage("trials/trial001", "output-bucket"),
            objects={"content/observations.csv": b"a,b\n"},
            timestamp=datetime(2026, 8, 20, 12, tzinfo=UTC),
            revision_id="scene-123",
            user_meta={"trial": 1},
            message="scene-native run",
            workflow=None,
        )
    assert s3.requests == []


def test_governed_bucket_requires_message_when_workflow_says_so() -> None:
    s3 = GovernedS3()
    with pytest.raises(WorkflowContractError, match="Commit message is required"):
        publish_package(
            s3_client=s3,
            output=OutputPackage("trials/trial001", "output-bucket"),
            objects={"content/observations.csv": b"a,b\n"},
            timestamp=datetime(2026, 8, 20, 12, tzinfo=UTC),
            revision_id="scene-123",
            user_meta={"trial": 1},
        )
    assert s3.requests == []


def test_non_serializable_user_meta_is_refused_before_writes() -> None:
    s3 = RecordingS3()
    with pytest.raises(PublicationError, match="JSON-serializable"):
        _publish(s3, user_meta={"bad": object()})
    assert s3.requests == []


def test_ungoverned_bucket_still_publishes_with_meta_and_message() -> None:
    s3 = RecordingS3()
    publication = _publish(
        s3,
        objects={"z.txt": b"z"},
        user_meta={"note": "ungoverned"},
        message="still recorded",
    )
    manifest_request = next(
        request
        for request in s3.requests
        if request["Key"] == f".quilt/packages/{publication.top_hash}"
    )
    header = json.loads(manifest_request["Body"].decode().splitlines()[0])
    assert header["user_meta"] == {"note": "ungoverned"}
    assert header["message"] == "still recorded"
    assert "workflow" not in header


class _Invariant:
    def __init__(
        self,
        identifier: str,
        check: Callable[[PublicationContext], Sequence[str]],
    ) -> None:
        self.identifier = identifier
        self._check = check

    def check(self, context: PublicationContext, /) -> Sequence[str]:
        return self._check(context)


def test_passing_invariant_sees_final_material_and_does_not_change_manifest() -> None:
    observed: list[PublicationContext] = []
    checker = _Invariant("content-contract/v1", lambda context: observed.append(context) or ())
    plain_s3 = RecordingS3()
    checked_s3 = RecordingS3()

    plain = _publish(
        plain_s3,
        objects={"result.txt": b"result"},
        user_meta={"trial": 1},
        message="complete",
    )
    checked = _publish(
        checked_s3,
        objects={"result.txt": b"result"},
        user_meta={"trial": 1},
        message="complete",
        invariants=(checker,),
    )

    assert len(observed) == 1
    assert observed[0].name == "project/output"
    assert observed[0].objects == {"result.txt": b"result"}
    assert observed[0].user_meta == {"trial": 1}
    assert observed[0].message == "complete"
    assert checked.top_hash == plain.top_hash
    plain_manifest = next(
        request["Body"] for request in plain_s3.requests if ".quilt/packages/" in request["Key"]
    )
    checked_manifest = next(
        request["Body"] for request in checked_s3.requests if ".quilt/packages/" in request["Key"]
    )
    assert checked_manifest == plain_manifest


def test_invariant_findings_and_unexpected_errors_are_aggregated_before_writes() -> None:
    def raises(_: PublicationContext) -> Sequence[str]:
        raise RuntimeError("parser failed")

    s3 = RecordingS3()
    with pytest.raises(DomainInvariantError) as raised_error:
        _publish(
            s3,
            invariants=(
                _Invariant("headers/v1", lambda _: ("missing Kind", "invalid Status")),
                _Invariant("join/v2", raises),
            ),
        )

    assert [(finding.invariant, finding.message) for finding in raised_error.value.findings] == [
        ("headers/v1", "missing Kind"),
        ("headers/v1", "invalid Status"),
        ("join/v2", "checker raised RuntimeError: parser failed"),
    ]
    assert s3.requests == []


def test_workflow_refusal_precedes_domain_invariants() -> None:
    calls: list[str] = []
    checker = _Invariant("domain/v1", lambda _: calls.append("domain") or ())
    s3 = GovernedS3()

    with pytest.raises(WorkflowContractError, match="Package metadata failed validation"):
        publish_package(
            s3_client=s3,
            output=OutputPackage("trials/trial001", "output-bucket"),
            objects={"content/observations.csv": b"a,b\n"},
            timestamp=datetime(2026, 8, 20, 12, tzinfo=UTC),
            revision_id="scene-123",
            user_meta={},
            message="scene-native run",
            invariants=(checker,),
        )

    assert calls == []
    assert s3.requests == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"revision_id": "latest"}, "revision_id"),
        ({"revision_id": "x"}, "revision_id"),
        ({"timestamp": datetime(2026, 8, 19, 12)}, "timezone-aware"),
        ({"objects": {}}, "non-empty mapping"),
        ({"objects": []}, "non-empty mapping"),
        ({"objects": {"": b"x"}}, "logical keys"),
        ({"objects": {"result.txt": "text"}}, "must be bytes"),
        ({"user_meta": []}, "user_meta must be a mapping"),
        ({"user_meta": {"nested": {1: "bad"}}}, "keys must be strings"),
        ({"message": 3}, "message must be a string"),
        ({"invariants": "checker"}, "invariants must be a sequence"),
        ({"workflow": 3}, "workflow must be"),
    ],
)
def test_publication_arguments_fail_closed_before_writes(
    kwargs: dict[str, Any], message: str
) -> None:
    s3 = RecordingS3()

    with pytest.raises((PublicationError, WorkflowContractError), match=message):
        _publish(s3, **kwargs)

    assert s3.requests == []


def test_revision_identity_must_match_compiled_read_authority() -> None:
    s3 = RecordingS3()

    with pytest.raises(PublicationError, match="does not match"):
        _publish(
            s3,
            revision_id="scene-other",
            output=OutputPackage("project/output", "output-bucket", revision_id="scene-authorized"),
        )

    assert s3.requests == []


class _ValueBody:
    def __init__(self, value: object) -> None:
        self.value = value

    def read(self) -> object:
        return self.value

    def close(self) -> None:
        return None


class _MalformedPointerS3(RecordingS3):
    def __init__(self, response: Mapping[str, Any]) -> None:
        super().__init__()
        self.response = dict(response)

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        if str(kwargs["Key"]).endswith("/scene-123"):
            return dict(self.response)
        return super().get_object(**kwargs)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({}, "no immutable version ID"),
        ({"VersionId": "v1"}, "no ETag"),
        (
            {"VersionId": "v1", "ETag": '"etag"', "Body": _ValueBody("not-bytes")},
            "did not return bytes",
        ),
        (
            {"VersionId": "v1", "ETag": '"etag"', "Body": _ValueBody(b"\xff")},
            "not an ASCII",
        ),
        (
            {"VersionId": "v1", "ETag": '"etag"', "Body": _ValueBody(b"short")},
            "not a complete Quilt top hash",
        ),
        (
            {
                "VersionId": "v1",
                "ETag": '"etag"',
                "Body": _ValueBody(b"a" * 64),
                "Metadata": [],
            },
            "invalid metadata",
        ),
    ],
)
def test_malformed_existing_revision_pointer_is_refused(
    response: Mapping[str, Any], message: str
) -> None:
    with pytest.raises(PublicationError, match=message):
        _publish(_MalformedPointerS3(response))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("manifest", "no manifest version identity"),
        ("state", "no valid latest-pointer base"),
        ("base", "invalid latest-pointer base"),
    ],
)
def test_malformed_existing_revision_metadata_is_a_typed_conflict(
    mutation: str, message: str
) -> None:
    s3 = RecordingS3()
    first = _publish(s3)
    key = ".quilt/named_packages/project/output/scene-123"
    stored = s3.current[key]
    metadata = dict(stored.metadata)
    if mutation == "manifest":
        metadata.pop("raja-manifest-version-id")
    elif mutation == "state":
        metadata["raja-previous-state"] = "unknown"
    else:
        metadata["raja-previous-state"] = "present"
        metadata["raja-previous-top-hash"] = "bad"
        metadata["raja-previous-etag"] = '"etag"'
    s3.current[key] = _StoredObject(stored.body, stored.version_id, stored.etag, metadata)
    write_count = len(s3.requests)

    with pytest.raises(PublicationConflictError, match=message):
        _publish(s3)

    assert first.revision_key == "scene-123"
    assert len(s3.requests) == write_count


def test_invalid_invariant_identifiers_are_aggregated_before_writes() -> None:
    class MissingIdentifier:
        def check(self, context: PublicationContext, /) -> Sequence[str]:
            return ()

    s3 = RecordingS3()
    with pytest.raises(DomainInvariantError) as raised_error:
        _publish(
            s3,
            invariants=(
                MissingIdentifier(),  # type: ignore[arg-type]
                _Invariant("duplicate/v1", lambda _: ()),
                _Invariant("duplicate/v1", lambda _: ()),
            ),
        )

    assert [finding.message for finding in raised_error.value.findings] == [
        "identifier must be a non-empty string",
        "identifier must be unique",
    ]
    assert s3.requests == []


def test_invalid_invariant_results_are_aggregated_before_writes() -> None:
    s3 = RecordingS3()
    with pytest.raises(DomainInvariantError) as raised_error:
        _publish(
            s3,
            invariants=(
                _Invariant("string/v1", lambda _: "not-a-sequence"),  # type: ignore[arg-type,return-value]
                _Invariant("findings/v1", lambda _: (None, "")),  # type: ignore[arg-type,return-value]
            ),
        )

    assert [finding.message for finding in raised_error.value.findings] == [
        "checker must return a sequence of finding strings",
        "checker returned an invalid finding",
        "checker returned an invalid finding",
    ]
    assert s3.requests == []


class _HttpConflictS3(RecordingS3):
    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        if str(kwargs["Key"]).endswith("/latest"):
            self.requests.append(kwargs)
            raise ClientError(
                {
                    "Error": {"Code": "Conflict", "Message": "raced"},
                    "ResponseMetadata": {"HTTPStatusCode": 409},
                },
                "PutObject",
            )
        return super().put_object(**kwargs)


def test_http_409_latest_race_is_published_not_latest() -> None:
    publication = _publish(_HttpConflictS3())

    assert publication.status == "published-not-latest"
    assert not publication.is_latest


class _BrokenLatestS3(RecordingS3):
    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        if str(kwargs["Key"]).endswith("/latest"):
            raise RuntimeError("latest unavailable")
        return super().put_object(**kwargs)


def test_nonconditional_latest_failure_is_not_misreported_as_a_cas_loss() -> None:
    with pytest.raises(PublicationError, match="Unable to publish") as raised:
        _publish(_BrokenLatestS3())

    assert isinstance(raised.value.__cause__, RuntimeError)


class _AbaOnRecoveryWriteS3(RecordingS3):
    """Install a same-ETag newer version immediately before an unsafe replay."""

    aba_armed = False
    aba_triggered = False

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        if self.aba_armed and key.endswith("/latest") and "IfMatch" in kwargs:
            current = self.current[key]
            self.current[key] = _StoredObject(
                current.body,
                "same-body-newer-version",
                current.etag,
                current.metadata,
            )
            self.aba_triggered = True
        return super().put_object(**kwargs)


def test_present_base_interrupted_retry_fails_closed_before_etag_aba_window() -> None:
    s3 = _AbaOnRecoveryWriteS3(fail_suffix="/latest")
    latest_key = ".quilt/named_packages/project/output/latest"
    s3.seed(
        latest_key,
        b"a" * 64,
        version_id="base-version",
        etag='"same-body-etag"',
    )
    interrupted = _publish(s3)
    assert interrupted.status == "published-not-latest"

    s3.fail_suffix = None
    s3.aba_armed = True
    write_count = len(s3.requests)
    retry = _publish(s3)

    assert len(s3.requests) == write_count
    assert retry.status == "published-not-latest"
    assert retry.reused_revision
    assert retry.write_disposition == "none"
    assert retry.latest_top_hash == "a" * 64
    assert not s3.aba_triggered
    assert s3.current[latest_key].version_id == "base-version"


def test_interrupted_retry_rejects_same_body_etag_with_newer_version_id() -> None:
    s3 = RecordingS3(fail_suffix="/latest")
    latest_key = ".quilt/named_packages/project/output/latest"
    s3.seed(
        latest_key,
        b"a" * 64,
        version_id="base-version",
        etag='"same-body-etag"',
    )
    interrupted = _publish(s3)
    assert interrupted.status == "published-not-latest"

    s3.fail_suffix = None
    s3.seed(
        latest_key,
        b"a" * 64,
        version_id="restored-newer-version",
        etag='"same-body-etag"',
    )
    write_count = len(s3.requests)
    retry = _publish(s3)

    assert len(s3.requests) == write_count
    assert retry.status == "published-not-latest"
    assert retry.latest_top_hash == "a" * 64
    assert s3.current[latest_key].version_id == "restored-newer-version"


class _SameIntentRevisionWinsS3(RecordingS3):
    def __init__(self) -> None:
        super().__init__()
        self.injected_winner = False

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        if (
            not self.injected_winner
            and key.endswith("/scene-123")
            and kwargs.get("IfNoneMatch") == "*"
        ):
            self.requests.append(kwargs)
            body = kwargs["Body"]
            metadata = kwargs["Metadata"]
            assert isinstance(body, bytes)
            assert isinstance(metadata, Mapping)
            self.current[key] = _StoredObject(
                body,
                "concurrent-revision-version",
                f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"',  # noqa: S324
                dict(metadata),
            )
            self.injected_winner = True
            raise self._precondition_failed()
        return super().put_object(**kwargs)


def test_same_intent_revision_winner_preserves_prior_package_writes() -> None:
    s3 = _SameIntentRevisionWinsS3()

    publication = _publish(s3)

    assert s3.injected_winner
    assert publication.status == "published"
    assert publication.reused_revision
    assert publication.write_disposition == "package-writes"


class _SameRevisionWinsLatestS3(RecordingS3):
    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        if key.endswith("/latest"):
            self.requests.append(kwargs)
            body = kwargs["Body"]
            assert isinstance(body, bytes)
            self.current[key] = _StoredObject(
                body,
                "concurrent-latest-version",
                f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"',  # noqa: S324
                {},
            )
            raise self._precondition_failed()
        return super().put_object(**kwargs)


def test_latest_conflict_reconciles_same_revision_as_published() -> None:
    s3 = _SameRevisionWinsLatestS3()

    publication = _publish(s3)

    assert publication.status == "published"
    assert publication.is_latest
    assert publication.latest_top_hash == publication.top_hash
    assert publication.write_disposition == "package-writes"


def test_invariant_metadata_is_recursively_read_only_between_checkers() -> None:
    observations: list[tuple[bool, tuple[str, ...]]] = []

    def cannot_mutate(context: PublicationContext) -> Sequence[str]:
        nested = context.user_meta["nested"]
        values = context.user_meta["values"]
        with pytest.raises(TypeError):
            nested["approved"] = True
        with pytest.raises(AttributeError):
            values.append("changed")
        return ()

    def observe(context: PublicationContext) -> Sequence[str]:
        nested = context.user_meta["nested"]
        values = context.user_meta["values"]
        observations.append((nested["approved"], tuple(values)))
        return ()

    publication = _publish(
        RecordingS3(),
        user_meta={"nested": {"approved": False}, "values": ["original"]},
        invariants=(
            _Invariant("mutator/v1", cannot_mutate),
            _Invariant("observer/v1", observe),
        ),
    )

    assert publication.status == "published"
    assert observations == [(False, ("original",))]
