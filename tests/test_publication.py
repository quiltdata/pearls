from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest
import quilt3.packages as quilt_packages
from botocore.exceptions import ClientError
from quilt3 import Package
from quilt3.telemetry import ApiTelemetry
from quilt3.util import QuiltException

from raja_sts import PublicationError
from raja_sts.models import OutputPackage
from raja_sts.publication import publish_package


class RecordingS3:
    def __init__(
        self,
        responses: list[dict[str, Any]] | None = None,
        *,
        versioning_status: str | None = "Enabled",
        fail_suffix: str | None = None,
    ) -> None:
        self.versioning_requests: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.responses = iter(responses or [])
        self.versioning_status = versioning_status
        self.fail_suffix = fail_suffix

    def get_bucket_versioning(self, **kwargs: Any) -> dict[str, str]:
        self.versioning_requests.append(kwargs)
        return {"Status": self.versioning_status} if self.versioning_status else {}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        if self.fail_suffix is not None and kwargs["Key"].endswith(self.fail_suffix):
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed", "Message": "already exists"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        return next(self.responses, {"VersionId": f"version-{len(self.requests)}"})


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
    output = OutputPackage("project/output", "output-bucket")
    top_hash = publish_package(
        s3_client=s3,
        output=output,
        objects={"z.txt": b"z", "dir/a b.txt": b"alpha"},
        timestamp=datetime(2026, 8, 19, 12, tzinfo=UTC),
    )

    assert s3.versioning_requests == [{"Bucket": "output-bucket"}]
    assert len(dump_calls) == 1
    keys = [request["Key"] for request in s3.requests]
    assert keys[:2] == ["project/output/dir/a b.txt", "project/output/z.txt"]
    assert keys[2] == f".quilt/packages/{top_hash}"
    assert keys[3] == ".quilt/named_packages/project/output/1787140800"
    assert keys[4] == ".quilt/named_packages/project/output/latest"
    assert "IfNoneMatch" not in s3.requests[0]
    assert s3.requests[4]["IfNoneMatch"] == "*"
    assert s3.requests[2]["ContentType"] == "application/jsonl"
    assert s3.requests[3]["Body"] == s3.requests[4]["Body"] == top_hash.encode()

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
    top_hash = publish_package(
        s3_client=s3,
        output=OutputPackage("project/output", "output-bucket"),
        objects=bodies,
        timestamp=datetime(2026, 8, 19, 12, tzinfo=UTC),
    )
    loaded = Package._load(io.StringIO(s3.requests[2]["Body"].decode()))

    assert loaded.top_hash == top_hash
    for logical_key, entry in loaded.walk():
        entry._verify_hash(bodies[logical_key])
    with pytest.raises(QuiltException, match="Hash validation failed"):
        loaded["a.txt"]._verify_hash(b"changed")


@pytest.mark.parametrize("status", [None, "Suspended"])
def test_publication_rejects_unversioned_bucket_before_writes(status: str | None) -> None:
    s3 = RecordingS3(versioning_status=status)

    with pytest.raises(PublicationError, match="must have versioning enabled"):
        publish_package(
            s3_client=s3,
            output=OutputPackage("project/output", "output-bucket"),
            objects={"result.txt": b"result"},
            timestamp=datetime(2026, 8, 19, 12, tzinfo=UTC),
        )

    assert not s3.requests


def test_concurrent_latest_conflict_leaves_version_pinned_data() -> None:
    s3 = RecordingS3(
        [
            {"VersionId": "loser-data-version"},
            {"VersionId": "loser-manifest-version"},
            {"VersionId": "loser-revision-version"},
        ],
        fail_suffix="/latest",
    )

    with pytest.raises(PublicationError, match="PreconditionFailed"):
        publish_package(
            s3_client=s3,
            output=OutputPackage("project/output", "output-bucket"),
            objects={"result.txt": b"result"},
            timestamp=datetime(2026, 8, 19, 12, tzinfo=UTC),
        )

    assert s3.requests[-1]["Key"] == ".quilt/named_packages/project/output/latest"
    assert s3.requests[-1]["IfNoneMatch"] == "*"
    manifest = json.loads(s3.requests[1]["Body"].decode().splitlines()[1])
    assert manifest["physical_keys"] == [
        "s3://output-bucket/project/output/result.txt?versionId=loser-data-version"
    ]


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        ([{}], "did not version output object 'project/output/result.txt'"),
        (
            [{"VersionId": "data-version"}, {}],
            "did not version output object '.quilt/packages/",
        ),
    ],
)
def test_every_publication_write_must_return_a_version_id(
    responses: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(PublicationError, match=message):
        publish_package(
            s3_client=RecordingS3(responses),
            output=OutputPackage("project/output", "output-bucket"),
            objects={"result.txt": b"result"},
            timestamp=datetime.now(UTC),
        )


def test_publish_package_wraps_write_failures() -> None:
    class BrokenS3:
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
        )
    assert isinstance(raised.value.__cause__, RuntimeError)
