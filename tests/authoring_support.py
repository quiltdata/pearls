"""Offline fixtures shared by genesis authoring API and CLI tests."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cli_support import HANDLER_SOURCE, OUTPUTS_JSON, MissingKey
from quilt3 import Package, checksums
from quilt3.packages import PackageEntry
from quilt3.util import PhysicalKey

AUTHOR_DESTINATION = "proj/genesis@output-bucket"
AUTHOR_METADATA = {"owner": "operator"}
AUTHOR_MESSAGE = "Author immutable genesis"
WORKFLOW_CONFIG = b"""\
is_workflow_required: true
workflows:
  genesis:
    handle_pattern: '^proj/'
    is_message_required: true
    metadata_schema: genesis-metadata
    entries_schema: genesis-entries
schemas:
  genesis-metadata:
    url: s3://output-bucket/.quilt/workflows/genesis-metadata.json
  genesis-entries:
    url: s3://output-bucket/.quilt/workflows/genesis-entries.json
"""
METADATA_SCHEMA = json.dumps(
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["owner"],
        "properties": {"owner": {"type": "string"}},
        "additionalProperties": True,
    }
).encode()
ENTRIES_SCHEMA = json.dumps(
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "array",
        "minItems": 4,
        "items": {
            "type": "object",
            "required": ["logical_key", "size", "meta"],
        },
    }
).encode()


class TrackedBody(io.BytesIO):
    """Streaming body that records close without losing the flag."""

    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


@dataclass(frozen=True)
class Stored:
    body: bytes
    version_id: str
    etag: str = '"etag"'
    metadata: Mapping[str, str] | None = None


class AuthoringS3:
    """Versioned S3 fake supporting workflow reads, sealing, and publication."""

    def __init__(self) -> None:
        self.versioning_status: str | None = "Enabled"
        self.versioning_requests: list[dict[str, Any]] = []
        self.get_requests: list[dict[str, Any]] = []
        self.put_requests: list[dict[str, Any]] = []
        self.bodies: list[TrackedBody] = []
        self.remote: dict[tuple[str, str, str | None], Stored] = {}
        self.current: dict[tuple[str, str], Stored] = {}
        self.fail_latest = False
        self.lose_revision_response = False
        self._seed_workflow()

    def _seed_workflow(self) -> None:
        self.remote[("output-bucket", ".quilt/workflows/config.yml", None)] = Stored(
            WORKFLOW_CONFIG, "workflow-config-v1"
        )
        self.remote[("output-bucket", ".quilt/workflows/genesis-metadata.json", None)] = Stored(
            METADATA_SCHEMA, "metadata-schema-v1"
        )
        self.remote[("output-bucket", ".quilt/workflows/genesis-entries.json", None)] = Stored(
            ENTRIES_SCHEMA, "entries-schema-v1"
        )

    def add_source(
        self,
        *,
        logical_key: str = "evidence/source.txt",
        content: bytes = b"sealed evidence",
        registry_bucket: str = "foreign-registry",
        package_name: str = "foreign/source",
    ) -> str:
        data_bucket = "foreign-data"
        physical_key = f"published/{logical_key}"
        version_id = "source-object-v1"
        package = Package()
        package.set(
            logical_key,
            PackageEntry(
                PhysicalKey(data_bucket, physical_key, version_id),
                len(content),
                {
                    "type": checksums.SHA256_HASH_NAME,
                    "value": checksums.legacy_calculate_checksum_bytes(content),
                },
                {},
            ),
        )
        top_hash = package.top_hash
        manifest = io.BytesIO()
        package._dump(manifest)
        self.remote[(registry_bucket, f".quilt/packages/{top_hash}", None)] = Stored(
            manifest.getvalue(), "source-manifest-v1"
        )
        self.remote[(data_bucket, physical_key, version_id)] = Stored(content, version_id)
        return (
            f"quilt+s3://{registry_bucket}#package={quote(package_name, safe='')}"
            f"&top_hash={top_hash}&path={quote(logical_key, safe='')}"
        )

    def get_bucket_versioning(self, **kwargs: Any) -> dict[str, str]:
        self.versioning_requests.append(kwargs)
        if self.versioning_status is None:
            return {}
        return {"Status": self.versioning_status}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_requests.append(kwargs)
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        requested_version = kwargs.get("VersionId")
        version = str(requested_version) if requested_version is not None else None
        stored = self.remote.get((bucket, key, version))
        if stored is None and version is None:
            stored = self.current.get((bucket, key))
        if stored is None:
            raise MissingKey()
        body = TrackedBody(stored.body)
        self.bodies.append(body)
        return {
            "Body": body,
            "ETag": stored.etag,
            "Metadata": dict(stored.metadata or {}),
            "VersionId": stored.version_id,
        }

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.put_requests.append(kwargs)
        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        current = self.current.get((bucket, key))
        if self.fail_latest and key.endswith("/latest"):
            raise MissingKey("PreconditionFailed")
        if kwargs.get("IfNoneMatch") == "*" and current is not None:
            raise MissingKey("PreconditionFailed")
        if "IfMatch" in kwargs and (current is None or current.etag != kwargs["IfMatch"]):
            raise MissingKey("PreconditionFailed")
        version_id = f"published-v{len(self.put_requests)}"
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        metadata = kwargs.get("Metadata", {})
        assert isinstance(metadata, Mapping)
        self.current[(bucket, key)] = Stored(
            body,
            version_id,
            etag=f'"etag-{len(self.put_requests)}"',
            metadata=dict(metadata),
        )
        if (
            self.lose_revision_response
            and key.startswith(".quilt/named_packages/")
            and not key.endswith("/latest")
        ):
            self.lose_revision_response = False
            raise OSError("simulated response loss after durable revision")
        return {"VersionId": version_id}


def write_valid_genesis(
    directory: Path,
    *,
    handler_key: str = "protocol/handler.py",
) -> None:
    """Write the smallest local tree accepted by current Scene execution."""
    (directory / "input").mkdir(parents=True)
    handler_path = directory / handler_key
    handler_path.parent.mkdir(parents=True, exist_ok=True)
    (directory / "README.md").write_text("Run the sealed protocol.\n", encoding="utf-8")
    (directory / "outputs.json").write_bytes(OUTPUTS_JSON)
    handler_path.write_bytes(HANDLER_SOURCE)
    (directory / "input" / "local.txt").write_bytes(b"local input")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
