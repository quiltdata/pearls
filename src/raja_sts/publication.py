"""No-list Quilt v1 publication through one caller-supplied S3 client."""

from __future__ import annotations

import io
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from quilt3 import Package, checksums
from quilt3.packages import PackageEntry
from quilt3.util import PhysicalKey

from .errors import PublicationError
from .models import OutputPackage


def _put_versioned(s3_client: Any, **request: Any) -> str:
    response = s3_client.put_object(**request)
    version_id = response.get("VersionId")
    if not isinstance(version_id, str) or not version_id or version_id == "null":
        raise PublicationError(f"S3 did not version output object {request['Key']!r}")
    return version_id


def publish_package(
    *,
    s3_client: Any,
    output: OutputPackage,
    objects: Mapping[str, bytes],
    timestamp: datetime,
) -> str:
    """Publish a Quilt v1 package to a versioned bucket without listing.

    Bucket versioning is checked before the first write and every write must
    return a version ID. Data objects are written first, followed by Quilt's
    canonical immutable manifest and a timestamped revision pointer. The
    create-only named ``latest`` pointer is always last.
    """
    try:
        versioning = s3_client.get_bucket_versioning(Bucket=output.bucket)
        if versioning.get("Status") != "Enabled":
            raise PublicationError(f"Output bucket {output.bucket!r} must have versioning enabled")

        package = Package()
        for logical_key in sorted(objects):
            body = objects[logical_key]
            physical_key = f"{output.name}/{logical_key}"
            version_id = _put_versioned(
                s3_client,
                Bucket=output.bucket,
                Key=physical_key,
                Body=body,
            )
            package.set(
                logical_key,
                PackageEntry(
                    PhysicalKey(output.bucket, physical_key, version_id),
                    len(body),
                    {
                        "type": checksums.SHA256_HASH_NAME,
                        "value": checksums.legacy_calculate_checksum_bytes(body),
                    },
                    {},
                ),
            )

        top_hash = package.top_hash
        if not isinstance(top_hash, str):
            raise TypeError("Quilt returned a non-string top hash")
        manifest_buffer = io.BytesIO()
        # Quilt's public dump() emits telemetry. _dump is the same canonical,
        # in-memory serializer without process-global or network side effects.
        package._dump(manifest_buffer)
        manifest = manifest_buffer.getvalue()

        pointer_body = top_hash.encode("ascii")
        pointer_prefix = f".quilt/named_packages/{output.name}"
        _put_versioned(
            s3_client,
            Bucket=output.bucket,
            Key=f".quilt/packages/{top_hash}",
            Body=manifest,
            ContentType="application/jsonl",
        )
        _put_versioned(
            s3_client,
            Bucket=output.bucket,
            Key=f"{pointer_prefix}/{int(timestamp.timestamp())}",
            Body=pointer_body,
        )
        _put_versioned(
            s3_client,
            Bucket=output.bucket,
            Key=f"{pointer_prefix}/latest",
            Body=pointer_body,
            IfNoneMatch="*",
        )
        return top_hash
    except PublicationError:
        raise
    except Exception as error:
        raise PublicationError(f"Unable to publish bounded output package: {error}") from error
