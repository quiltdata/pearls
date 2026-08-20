"""No-list Quilt v1 publication through one caller-supplied S3 client."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode, urlunparse

from .errors import PublicationError
from .models import OutputPackage


def _physical_uri(bucket: str, key: str, version_id: str | None) -> str:
    query = urlencode({"versionId": version_id}) if version_id is not None else ""
    return urlunparse(("s3", bucket, quote(f"/{key}"), "", query, ""))


def _top_hash(records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":")).encode
    digest.update(encoder({}).encode())
    for record in records:
        digest.update(
            encoder(
                {
                    "hash": record["hash"],
                    "logical_key": record["logical_key"],
                    "meta": record["meta"],
                    "size": record["size"],
                }
            ).encode()
        )
    return digest.hexdigest()


def _manifest_bytes(records: list[dict[str, Any]]) -> bytes:
    lines = [{}, *records]
    return b"".join(
        (json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        for line in lines
    )


def publish_package(
    *,
    s3_client: Any,
    output: OutputPackage,
    objects: Mapping[str, bytes],
    timestamp: datetime,
) -> str:
    """Publish a Quilt v1 package without listing or ambient AWS credentials.

    Data objects are written first, followed by the immutable manifest and a
    timestamped revision pointer. The named ``latest`` pointer is always last.
    """
    try:
        records: list[dict[str, Any]] = []
        for logical_key in sorted(objects):
            body = objects[logical_key]
            physical_key = f"{output.name}/{logical_key}"
            response = s3_client.put_object(
                Bucket=output.bucket,
                Key=physical_key,
                Body=body,
            )
            version_id = response.get("VersionId")
            if version_id is not None and not isinstance(version_id, str):
                raise TypeError("S3 returned a non-string VersionId")
            records.append(
                {
                    "logical_key": logical_key,
                    "physical_keys": [_physical_uri(output.bucket, physical_key, version_id)],
                    "size": len(body),
                    "hash": {"type": "SHA256", "value": hashlib.sha256(body).hexdigest()},
                    "meta": {},
                }
            )

        top_hash = _top_hash(records)
        manifest = _manifest_bytes(records)
        pointer_body = top_hash.encode("ascii")
        pointer_prefix = f".quilt/named_packages/{output.name}"
        s3_client.put_object(
            Bucket=output.bucket,
            Key=f".quilt/packages/{top_hash}",
            Body=manifest,
            ContentType="application/jsonl",
        )
        s3_client.put_object(
            Bucket=output.bucket,
            Key=f"{pointer_prefix}/{int(timestamp.timestamp())}",
            Body=pointer_body,
        )
        s3_client.put_object(
            Bucket=output.bucket,
            Key=f"{pointer_prefix}/latest",
            Body=pointer_body,
            IfNoneMatch="*",
        )
        return top_hash
    except Exception as error:
        raise PublicationError(f"Unable to publish bounded output package: {error}") from error
