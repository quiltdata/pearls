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


def publish_package(
    *,
    s3_client: Any,
    output: OutputPackage,
    objects: Mapping[str, bytes],
    timestamp: datetime,
) -> str:
    """Publish a Quilt v1 package without listing or ambient AWS credentials.

    Data objects are written first, followed by Quilt's canonical immutable
    manifest and a timestamped revision pointer. The create-only named
    ``latest`` pointer is always last.
    """
    try:
        package = Package()
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
