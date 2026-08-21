"""No-list Quilt v1 publication through one caller-supplied S3 client."""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal, Protocol

from quilt3 import Package, checksums
from quilt3.packages import PackageEntry
from quilt3.util import PhysicalKey

from .errors import (
    DomainInvariantError,
    InvariantFinding,
    PublicationConflictError,
    PublicationError,
    WorkflowContractError,
)
from .models import OutputPackage
from .workflows import resolve_workflow_stamp

_REVISION_ID_RE = re.compile(r"^[A-Za-z0-9_+=,.@-]{2,64}$")
_TOP_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONDITIONAL_ERROR_CODES = frozenset({"ConditionalRequestConflict", "PreconditionFailed"})
_MISSING_ERROR_CODES = frozenset({"404", "NoSuchKey", "NoSuchObject", "NotFound"})
_INTENT_DIGEST_META = "raja-intent-sha256"
_MANIFEST_VERSION_META = "raja-manifest-version-id"
_PREVIOUS_STATE_META = "raja-previous-state"
_PREVIOUS_TOP_HASH_META = "raja-previous-top-hash"
_PREVIOUS_ETAG_META = "raja-previous-etag"
_PREVIOUS_VERSION_ID_META = "raja-previous-version-id"
_PUBLISHED_AT_META = "raja-published-at"

PublicationStatus = Literal["published", "published-not-latest"]


@dataclass(frozen=True, slots=True)
class PublicationContext:
    """Detached, read-only material supplied to one domain-invariant checker.

    The context describes only the pending revision. Cross-revision checks need
    separately authorized prior-state resolution and are intentionally outside
    this interface.
    """

    name: str
    objects: Mapping[str, bytes]
    user_meta: Mapping[str, Any]
    message: str | None


class PublicationInvariant(Protocol):
    """Caller-supplied, side-effect-free check over one pending revision."""

    identifier: str

    def check(self, context: PublicationContext, /) -> Sequence[str]:
        """Return refusal findings, or an empty sequence when the revision passes."""


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """The durable revision and its best-effort named-package disposition."""

    top_hash: str
    revision_id: str
    revision_version_id: str
    manifest_version_id: str
    status: PublicationStatus
    is_latest: bool
    reused_revision: bool
    previous_top_hash: str | None
    latest_top_hash: str | None
    metadata_keys: tuple[str, ...]

    @property
    def revision_key(self) -> str:
        """Return the collision-safe named-package revision pointer key."""
        return self.revision_id


@dataclass(frozen=True, slots=True)
class _Pointer:
    top_hash: str
    etag: str
    version_id: str
    metadata: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _LatestBase:
    top_hash: str
    etag: str
    version_id: str


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    detail = response.get("Error")
    if not isinstance(detail, Mapping):
        return None
    code = detail.get("Code")
    return str(code) if code is not None else None


def _is_conditional_conflict(error: Exception) -> bool:
    if _error_code(error) in _CONDITIONAL_ERROR_CODES:
        return True
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    metadata = response.get("ResponseMetadata")
    return isinstance(metadata, Mapping) and metadata.get("HTTPStatusCode") in {409, 412}


def _put_versioned(s3_client: Any, **request: Any) -> str:
    response = s3_client.put_object(**request)
    version_id = response.get("VersionId")
    if not isinstance(version_id, str) or not version_id or version_id == "null":
        raise PublicationError(f"S3 did not version output object {request['Key']!r}")
    return version_id


def _read_pointer(s3_client: Any, *, bucket: str, key: str) -> _Pointer | None:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except Exception as error:
        if _error_code(error) in _MISSING_ERROR_CODES:
            return None
        raise

    version_id = response.get("VersionId")
    if not isinstance(version_id, str) or not version_id or version_id == "null":
        raise PublicationError(f"S3 pointer {key!r} has no immutable version ID")
    etag = response.get("ETag")
    if not isinstance(etag, str) or not etag:
        raise PublicationError(f"S3 pointer {key!r} has no ETag for compare-and-swap")
    body = response.get("Body")
    try:
        content = body.read()
    finally:
        if body is not None:
            body.close()
    if not isinstance(content, bytes):
        raise PublicationError(f"S3 pointer {key!r} did not return bytes")
    try:
        top_hash = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise PublicationError(f"S3 pointer {key!r} is not an ASCII Quilt top hash") from error
    if not _TOP_HASH_RE.fullmatch(top_hash):
        raise PublicationError(f"S3 pointer {key!r} is not a complete Quilt top hash")

    raw_metadata = response.get("Metadata", {})
    if not isinstance(raw_metadata, Mapping):
        raise PublicationError(f"S3 pointer {key!r} returned invalid metadata")
    metadata: dict[str, str] = {}
    for raw_key, raw_value in raw_metadata.items():
        if isinstance(raw_key, str) and isinstance(raw_value, str):
            metadata[raw_key.lower()] = raw_value
    return _Pointer(top_hash, etag, version_id, MappingProxyType(metadata))


def _validate_revision_id(revision_id: object) -> str:
    if (
        not isinstance(revision_id, str)
        or not _REVISION_ID_RE.fullmatch(revision_id)
        or revision_id == "latest"
    ):
        raise PublicationError("revision_id must be 2-64 safe Scene ID characters and not 'latest'")
    return revision_id


def _ensure_json_object_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise PublicationError("Package metadata JSON object keys must be strings")
            _ensure_json_object_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _ensure_json_object_keys(child)


def _metadata_json(meta: Mapping[str, Any]) -> str:
    _ensure_json_object_keys(meta)
    try:
        return json.dumps(meta, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PublicationError(f"Package metadata must be JSON-serializable: {error}") from error


def _workflow_identity(workflow: str | None | object) -> Mapping[str, str]:
    if workflow is ...:
        return {"selection": "default"}
    if workflow is None:
        return {"selection": "none"}
    if isinstance(workflow, str):
        return {"selection": "named", "id": workflow}
    raise WorkflowContractError("workflow must be a workflow id string, None, or ellipsis")


def _invariant_identifiers(
    invariants: Sequence[PublicationInvariant],
) -> tuple[str, ...]:
    findings: list[InvariantFinding] = []
    identifiers: list[str] = []
    seen: set[str] = set()
    for index, invariant in enumerate(invariants):
        identifier = getattr(invariant, "identifier", None)
        if not isinstance(identifier, str) or not identifier.strip():
            findings.append(
                InvariantFinding(f"invariant[{index}]", "identifier must be a non-empty string")
            )
            continue
        if identifier in seen:
            findings.append(InvariantFinding(identifier, "identifier must be unique"))
            continue
        seen.add(identifier)
        identifiers.append(identifier)
    if findings:
        raise DomainInvariantError(tuple(findings))
    return tuple(identifiers)


def _intent_digest(
    *,
    output: OutputPackage,
    revision_id: str,
    objects: Mapping[str, bytes],
    meta: Mapping[str, Any],
    message: str | None,
    workflow: str | None | object,
    invariant_ids: Sequence[str],
) -> str:
    intent = {
        "bucket": output.bucket,
        "name": output.name,
        "revision_id": revision_id,
        "objects": [
            {
                "logical_key": key,
                "size": len(objects[key]),
                "sha256": hashlib.sha256(objects[key]).hexdigest(),
            }
            for key in sorted(objects)
        ],
        "user_meta": meta,
        "message": message,
        "workflow": _workflow_identity(workflow),
        "invariants": list(invariant_ids),
    }
    encoded = json.dumps(intent, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _check_invariants(
    invariants: Sequence[PublicationInvariant],
    identifiers: Sequence[str],
    *,
    output: OutputPackage,
    objects: Mapping[str, bytes],
    meta_json: str,
    message: str | None,
) -> None:
    detached_meta = json.loads(meta_json)
    if not isinstance(detached_meta, dict):  # The source is always a metadata mapping.
        raise TypeError("metadata JSON did not decode to an object")
    frozen_meta = _freeze_json(detached_meta)
    if not isinstance(frozen_meta, Mapping):
        raise TypeError("frozen metadata is not a mapping")
    context = PublicationContext(
        name=output.name,
        objects=MappingProxyType(dict(objects)),
        user_meta=frozen_meta,
        message=message,
    )
    findings: list[InvariantFinding] = []
    for invariant, identifier in zip(invariants, identifiers, strict=True):
        try:
            result = invariant.check(context)
        except Exception as error:
            findings.append(
                InvariantFinding(
                    identifier,
                    f"checker raised {type(error).__name__}: {error}",
                )
            )
            continue
        if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
            findings.append(
                InvariantFinding(identifier, "checker must return a sequence of finding strings")
            )
            continue
        for finding in result:
            if not isinstance(finding, str) or not finding.strip():
                findings.append(InvariantFinding(identifier, "checker returned an invalid finding"))
            else:
                findings.append(InvariantFinding(identifier, finding))
    if findings:
        raise DomainInvariantError(tuple(findings))


def _build_package(
    *,
    s3_client: Any,
    output: OutputPackage,
    objects: Mapping[str, bytes],
    meta: Mapping[str, Any],
    message: str | None,
    stamp: Mapping[str, Any] | None,
) -> tuple[str, str]:
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
        package.set_meta(dict(meta))
    if message is not None:
        package._meta["message"] = message
    if stamp is not None:
        package._workflow = dict(stamp)

    top_hash = package.top_hash
    if not isinstance(top_hash, str):
        raise TypeError("Quilt returned a non-string top hash")
    manifest_buffer = io.BytesIO()
    # Quilt's public dump() emits telemetry. _dump is the same canonical,
    # in-memory serializer without process-global or network side effects.
    package._dump(manifest_buffer)
    manifest_version_id = _put_versioned(
        s3_client,
        Bucket=output.bucket,
        Key=f".quilt/packages/{top_hash}",
        Body=manifest_buffer.getvalue(),
        ContentType="application/jsonl",
    )
    return top_hash, manifest_version_id


def _base_from_revision(pointer: _Pointer) -> _LatestBase | None:
    state = pointer.metadata.get(_PREVIOUS_STATE_META)
    if state == "absent":
        return None
    if state != "present":
        raise PublicationConflictError("Existing revision has no valid latest-pointer base")
    top_hash = pointer.metadata.get(_PREVIOUS_TOP_HASH_META)
    etag = pointer.metadata.get(_PREVIOUS_ETAG_META)
    version_id = pointer.metadata.get(_PREVIOUS_VERSION_ID_META)
    if (
        top_hash is None
        or not _TOP_HASH_RE.fullmatch(top_hash)
        or not etag
        or not version_id
        or version_id == "null"
    ):
        raise PublicationConflictError("Existing revision has an invalid latest-pointer base")
    return _LatestBase(top_hash, etag, version_id)


def _snapshot_matches(pointer: _Pointer | None, base: _LatestBase | None) -> bool:
    if pointer is None or base is None:
        return pointer is None and base is None
    return (
        pointer.top_hash == base.top_hash
        and pointer.etag == base.etag
        and pointer.version_id == base.version_id
    )


def _advance_latest(
    *,
    s3_client: Any,
    output: OutputPackage,
    latest_key: str,
    top_hash: str,
    base: _LatestBase | None,
) -> tuple[bool, str | None]:
    request: dict[str, Any] = {
        "Bucket": output.bucket,
        "Key": latest_key,
        "Body": top_hash.encode("ascii"),
    }
    if base is None:
        request["IfNoneMatch"] = "*"
    else:
        request["IfMatch"] = base.etag
    try:
        _put_versioned(s3_client, **request)
    except Exception as error:
        if not _is_conditional_conflict(error):
            raise
        observed = _read_pointer(s3_client, bucket=output.bucket, key=latest_key)
        if observed is not None and observed.top_hash == top_hash:
            return True, observed.top_hash
        return False, observed.top_hash if observed is not None else None
    return True, top_hash


def _result_for_existing_revision(
    *,
    s3_client: Any,
    output: OutputPackage,
    revision_id: str,
    revision: _Pointer,
    latest_key: str,
    metadata_keys: tuple[str, ...],
) -> PublicationResult:
    manifest_version_id = revision.metadata.get(_MANIFEST_VERSION_META)
    if not manifest_version_id:
        raise PublicationConflictError("Existing revision has no manifest version identity")
    base = _base_from_revision(revision)
    current = _read_pointer(s3_client, bucket=output.bucket, key=latest_key)
    if current is not None and current.top_hash == revision.top_hash:
        is_latest = True
        latest_top_hash: str | None = revision.top_hash
    elif not _snapshot_matches(current, base):
        is_latest = False
        latest_top_hash = current.top_hash if current is not None else None
    else:
        is_latest, latest_top_hash = _advance_latest(
            s3_client=s3_client,
            output=output,
            latest_key=latest_key,
            top_hash=revision.top_hash,
            base=base,
        )
    return PublicationResult(
        top_hash=revision.top_hash,
        revision_id=revision_id,
        revision_version_id=revision.version_id,
        manifest_version_id=manifest_version_id,
        status="published" if is_latest else "published-not-latest",
        is_latest=is_latest,
        reused_revision=True,
        previous_top_hash=base.top_hash if base is not None else None,
        latest_top_hash=latest_top_hash,
        metadata_keys=metadata_keys,
    )


def publish_package(
    *,
    s3_client: Any,
    output: OutputPackage,
    objects: Mapping[str, bytes],
    timestamp: datetime,
    revision_id: str,
    user_meta: Mapping[str, Any] | None = None,
    message: str | None = None,
    workflow: str | None | object = ...,
    invariants: Sequence[PublicationInvariant] = (),
) -> PublicationResult:
    """Publish one idempotent Quilt revision and conditionally advance ``latest``.

    ``revision_id`` is the invocation identity. Reusing it with the same object
    bytes and publication declarations returns the already-recorded revision;
    reusing it for different intent is a typed conflict. Each revision pointer
    records the exact ``latest`` ETag observed before its writes, allowing a
    retry after interruption to finish that original compare-and-swap without
    rolling back a newer publication.

    Workflow validation and every caller-supplied invariant run before the first
    write. Invariants receive the final merged metadata and complete object set.
    Their findings are aggregated into :class:`DomainInvariantError`. Data and
    the immutable manifest are then written before a create-only revision
    pointer. ``latest`` is last and uses ``If-None-Match`` for genesis or
    ``If-Match`` for a subsequent revision. Losing that CAS is a successful
    ``published-not-latest`` outcome because the immutable revision is durable.
    """
    try:
        revision_id = _validate_revision_id(revision_id)
        if output.revision_id is not None and output.revision_id != revision_id:
            raise PublicationError(
                "revision_id does not match the exact revision read authority in output"
            )
        if timestamp.tzinfo is None:
            raise PublicationError("publication timestamp must be timezone-aware")
        published_at = timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")

        versioning = s3_client.get_bucket_versioning(Bucket=output.bucket)
        if versioning.get("Status") != "Enabled":
            raise PublicationError(f"Output bucket {output.bucket!r} must have versioning enabled")

        if not isinstance(objects, Mapping) or not objects:
            raise PublicationError("objects must be a non-empty mapping")
        normalized_objects: dict[str, bytes] = {}
        for logical_key, body in objects.items():
            if not isinstance(logical_key, str) or not logical_key:
                raise PublicationError("object logical keys must be non-empty strings")
            if not isinstance(body, bytes):
                raise PublicationError(f"output object {logical_key!r} must be bytes")
            normalized_objects[logical_key] = body

        if user_meta is not None and not isinstance(user_meta, Mapping):
            raise PublicationError("user_meta must be a mapping")
        meta = dict(user_meta) if user_meta else {}
        meta_json = _metadata_json(meta)
        if message is not None and not isinstance(message, str):
            raise PublicationError("message must be a string")
        if isinstance(invariants, (str, bytes)) or not isinstance(invariants, Sequence):
            raise PublicationError("invariants must be a sequence")
        invariant_ids = _invariant_identifiers(invariants)
        digest = _intent_digest(
            output=output,
            revision_id=revision_id,
            objects=normalized_objects,
            meta=meta,
            message=message,
            workflow=workflow,
            invariant_ids=invariant_ids,
        )

        pointer_prefix = f".quilt/named_packages/{output.name}"
        revision_key = f"{pointer_prefix}/{revision_id}"
        latest_key = f"{pointer_prefix}/latest"
        existing_revision = _read_pointer(s3_client, bucket=output.bucket, key=revision_key)
        metadata_keys = tuple(sorted(meta))
        if existing_revision is not None:
            if existing_revision.metadata.get(_INTENT_DIGEST_META) != digest:
                raise PublicationConflictError(
                    f"Revision {revision_id!r} already records different publication intent"
                )
            return _result_for_existing_revision(
                s3_client=s3_client,
                output=output,
                revision_id=revision_id,
                revision=existing_revision,
                latest_key=latest_key,
                metadata_keys=metadata_keys,
            )

        entries = [
            {"logical_key": key, "size": len(normalized_objects[key]), "meta": {}}
            for key in sorted(normalized_objects)
        ]
        stamp = resolve_workflow_stamp(
            s3_client=s3_client,
            bucket=output.bucket,
            package_name=output.name,
            workflow=workflow,
            user_meta=meta,
            entries=entries,
            message=message,
        )
        _check_invariants(
            invariants,
            invariant_ids,
            output=output,
            objects=normalized_objects,
            meta_json=meta_json,
            message=message,
        )

        previous = _read_pointer(s3_client, bucket=output.bucket, key=latest_key)
        base = (
            _LatestBase(previous.top_hash, previous.etag, previous.version_id)
            if previous is not None
            else None
        )
        top_hash, manifest_version_id = _build_package(
            s3_client=s3_client,
            output=output,
            objects=normalized_objects,
            meta=meta,
            message=message,
            stamp=stamp,
        )
        revision_metadata = {
            _INTENT_DIGEST_META: digest,
            _MANIFEST_VERSION_META: manifest_version_id,
            _PREVIOUS_STATE_META: "present" if base is not None else "absent",
            _PUBLISHED_AT_META: published_at,
        }
        if base is not None:
            revision_metadata[_PREVIOUS_TOP_HASH_META] = base.top_hash
            revision_metadata[_PREVIOUS_ETAG_META] = base.etag
            revision_metadata[_PREVIOUS_VERSION_ID_META] = base.version_id
        try:
            revision_version_id = _put_versioned(
                s3_client,
                Bucket=output.bucket,
                Key=revision_key,
                Body=top_hash.encode("ascii"),
                Metadata=revision_metadata,
                IfNoneMatch="*",
            )
        except Exception as error:
            if not _is_conditional_conflict(error):
                raise
            winner = _read_pointer(s3_client, bucket=output.bucket, key=revision_key)
            if winner is None or winner.metadata.get(_INTENT_DIGEST_META) != digest:
                raise PublicationConflictError(
                    f"Revision {revision_id!r} was concurrently claimed by different intent"
                ) from error
            return _result_for_existing_revision(
                s3_client=s3_client,
                output=output,
                revision_id=revision_id,
                revision=winner,
                latest_key=latest_key,
                metadata_keys=metadata_keys,
            )

        is_latest, latest_top_hash = _advance_latest(
            s3_client=s3_client,
            output=output,
            latest_key=latest_key,
            top_hash=top_hash,
            base=base,
        )
        return PublicationResult(
            top_hash=top_hash,
            revision_id=revision_id,
            revision_version_id=revision_version_id,
            manifest_version_id=manifest_version_id,
            status="published" if is_latest else "published-not-latest",
            is_latest=is_latest,
            reused_revision=False,
            previous_top_hash=previous.top_hash if previous is not None else None,
            latest_top_hash=latest_top_hash,
            metadata_keys=metadata_keys,
        )
    except (DomainInvariantError, PublicationError, WorkflowContractError):
        raise
    except Exception as error:
        raise PublicationError(f"Unable to publish bounded output package: {error}") from error
