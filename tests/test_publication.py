from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from raja_sts import PublicationError
from raja_sts.models import OutputPackage
from raja_sts.publication import publish_package


class RecordingS3:
    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses = iter(responses or [])

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return next(self.responses, {})


def test_publish_package_is_quilt_compatible_no_list_and_latest_last() -> None:
    s3 = RecordingS3([{"VersionId": "v 1"}, {}])
    output = OutputPackage("project/output", "output-bucket")
    top_hash = publish_package(
        s3_client=s3,
        output=output,
        objects={"z.txt": b"z", "dir/a b.txt": b"alpha"},
        timestamp=datetime(2026, 8, 19, 12, tzinfo=UTC),
    )

    keys = [request["Key"] for request in s3.requests]
    assert keys[:2] == ["project/output/dir/a b.txt", "project/output/z.txt"]
    assert keys[2] == f".quilt/packages/{top_hash}"
    assert keys[3] == ".quilt/named_packages/project/output/1787140800"
    assert keys[4] == ".quilt/named_packages/project/output/latest"
    assert s3.requests[4]["IfNoneMatch"] == "*"
    assert s3.requests[2]["ContentType"] == "application/jsonl"
    assert s3.requests[3]["Body"] == s3.requests[4]["Body"] == top_hash.encode()

    records = [json.loads(line) for line in s3.requests[2]["Body"].decode().splitlines()]
    assert records[0] == {}
    assert records[1]["logical_key"] == "dir/a b.txt"
    assert records[1]["physical_keys"] == [
        "s3://output-bucket/project/output/dir/a%20b.txt?versionId=v+1"
    ]
    assert records[2]["physical_keys"] == ["s3://output-bucket/project/output/z.txt"]
    assert len(top_hash) == 64


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("write denied"), TypeError("bad response")],
)
def test_publish_package_wraps_write_and_version_failures(failure: Exception) -> None:
    class BrokenS3:
        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            if isinstance(failure, RuntimeError):
                raise failure
            return {"VersionId": 42}

    with pytest.raises(PublicationError, match="Unable to publish") as raised:
        publish_package(
            s3_client=BrokenS3(),
            output=OutputPackage("project/output", "output-bucket"),
            objects={"result.txt": b"result"},
            timestamp=datetime.now(UTC),
        )
    assert raised.value.__cause__ is failure or isinstance(raised.value.__cause__, TypeError)
