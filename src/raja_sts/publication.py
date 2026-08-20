"""No-list Quilt v1 publication through one caller-supplied S3 client."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from quilt3 import Package, checksums
from quilt3.packages import PackageEntry
from quilt3.util import PhysicalKey

from .errors import PublicationError, WorkflowContractError
from .models import OutputPackage
from .workflows import resolve_workflow_stamp


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
    user_meta: Mapping[str, Any] | None = None,
    message: str | None = None,
    workflow: str | None | object = ...,
) -> str:
    """Publish a Quilt v1 package to a versioned bucket without listing.

    The destination registry's workflow contract is honored with quilt3
    semantics before anything is written: ``workflow=...`` (the default)
    resolves the registry's default workflow and respects
    ``is_workflow_required``; ``workflow=None`` explicitly opts out and is
    refused when the registry requires one; a named workflow must exist.
    Metadata, entries, message, and package-name requirements are validated
    client-side against the registry's schemas, and the resulting workflow
    stamp is written into the manifest header exactly as quilt3 writes it.
    A contract refusal raises :class:`WorkflowContractError` before any write.

    Bucket versioning is checked before the first write and every write must
    return a version ID. Data objects are written first, followed by Quilt's
    canonical immutable manifest and a timestamped revision pointer. The
    create-only named ``latest`` pointer is always last.
    """
    try:
        versioning = s3_client.get_bucket_versioning(Bucket=output.bucket)
        if versioning.get("Status") != "Enabled":
            raise PublicationError(f"Output bucket {output.bucket!r} must have versioning enabled")

        if user_meta is not None and not isinstance(user_meta, Mapping):
            raise PublicationError("user_meta must be a mapping")
        meta = dict(user_meta) if user_meta else {}
        try:
            json.dumps(meta, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise PublicationError(
                f"Package metadata must be JSON-serializable: {error}"
            ) from error
        if message is not None and not isinstance(message, str):
            raise PublicationError("message must be a string")

        stamp = resolve_workflow_stamp(
            s3_client=s3_client,
            bucket=output.bucket,
            package_name=output.name,
            workflow=workflow,
            user_meta=meta,
            entries=[
                {"logical_key": key, "size": len(objects[key]), "meta": {}}
                for key in sorted(objects)
            ],
            message=message,
        )

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

        if meta:
            package.set_meta(meta)
        if message is not None:
            package._meta["message"] = message
        if stamp is not None:
            # The workflow stamp participates in the top hash and the manifest
            # header, exactly as quilt3's push writes it.
            package._workflow = stamp

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
    except (PublicationError, WorkflowContractError):
        raise
    except Exception as error:
        raise PublicationError(f"Unable to publish bounded output package: {error}") from error
