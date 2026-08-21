"""Operator-side preparation and publication of immutable genesis packages.

Authoring is deliberately separate from Scene execution and infrastructure
administration. It uses one caller-supplied S3 client, freezes local bytes once,
materializes sealed foreign inputs by exact version and content hash, validates the
destination contract without writing, and delegates durable publication to the
existing revision-safe publisher.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from .errors import (
    GenesisAuthoringError,
    InvalidManifestURI,
    ManifestLoadError,
    PublicationError,
)
from .genesis import (
    OUTPUTS_KEY,
    README_KEY,
    RUNTIME_REQUIREMENTS_KEY,
    SEALED_HANDLER_KEY,
    decode_genesis_readme,
    parse_genesis_outputs,
    parse_runtime_requirements,
    validate_genesis_layout_keys,
)
from .manifest import (
    format_manifest_uri,
    load_quilt_manifest_from_s3,
    parse_manifest_uri,
    parse_output_package,
    read_manifest_object,
)
from .models import ManifestObject, OutputPackage, PinnedManifestURI
from .publication import (
    PublicationResult,
    _publication_would_write,
    publish_package,
    validate_revision_id,
)
from .sealed import validate_sealed_handler_source
from .workflows import resolve_workflow_stamp

DEFAULT_GENESIS_WORKFLOW = "genesis"
_GENESIS_PUBLICATION_INTENT_SCHEMA = (
    "https://quiltdata.com/schemas/raja-sts/genesis-publication-intent/v1"
)
_MALFORMED_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_FORBIDDEN_LOGICAL_CHARACTERS = frozenset("*?")
_READ_BITS = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_SUPPORTS_DESCRIPTOR_TRAVERSAL = (
    os.scandir in os.supports_fd and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
)


@dataclass(frozen=True, slots=True)
class SealInputURI:
    """One strictly parsed, immutable foreign-package input selection."""

    manifest: PinnedManifestURI
    logical_key: str
    original: str

    @property
    def destination_logical_key(self) -> str:
        """Return the deterministic local key used in the authored genesis."""
        return f"input/{self.logical_key}"


@dataclass(frozen=True, slots=True)
class GenesisEntrySummary:
    """Non-secret digest and size evidence for one frozen authored entry."""

    logical_key: str
    size: int
    sha256: str
    origin: Literal["local", "sealed"]

    def report(self) -> dict[str, Any]:
        """Return the stable receipt shape for this entry."""
        return {
            "logical_key": self.logical_key,
            "origin": self.origin,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class SealProvenance:
    """Non-secret evidence for one exact-version input materialization."""

    source_uri: str
    source_logical_key: str
    source_bucket: str
    source_key: str
    source_version_id: str
    hash_type: str
    hash_value: str
    destination_logical_key: str
    size: int
    sha256: str

    def report(self) -> dict[str, Any]:
        """Return provenance without object bodies or credential material."""
        return {
            "destination_logical_key": self.destination_logical_key,
            "sha256": self.sha256,
            "size": self.size,
            "source": {
                "bucket": self.source_bucket,
                "hash": {"type": self.hash_type, "value": self.hash_value},
                "key": self.source_key,
                "logical_key": self.source_logical_key,
                "pinned_uri": self.source_uri,
                "version_id": self.source_version_id,
            },
        }


@dataclass(frozen=True, slots=True)
class GenesisAuthoringPlan:
    """Immutable, fully validated material prepared before operator approval."""

    directory: Path
    output: OutputPackage
    objects: Mapping[str, bytes] = field(repr=False)
    _metadata_json: str = field(repr=False)
    _declared_intent_json: str = field(repr=False)
    message: str
    workflow: str
    revision_id: str
    entries: tuple[GenesisEntrySummary, ...]
    seals: tuple[SealProvenance, ...]
    handler_key: str = SEALED_HANDLER_KEY

    @property
    def user_meta(self) -> Mapping[str, Any]:
        """Return a detached metadata object; mutating it cannot change the plan."""
        value = json.loads(self._metadata_json)
        if not isinstance(value, dict):  # Constructed only by _normalize_metadata.
            raise TypeError("prepared metadata did not decode to an object")
        return MappingProxyType(value)

    @property
    def declared_intent(self) -> Mapping[str, Any]:
        """Return the canonical non-secret declarations bound to this revision."""
        value = json.loads(self._declared_intent_json)
        if not isinstance(value, dict):  # Constructed only by preparation.
            raise TypeError("prepared declared intent did not decode to an object")
        return MappingProxyType(value)

    @property
    def metadata_keys(self) -> tuple[str, ...]:
        """Return the only metadata detail safe for implicit receipts."""
        return tuple(sorted(self.user_meta))


@dataclass(frozen=True, slots=True)
class GenesisAuthoringResult:
    """One durable authored revision and the exact frozen plan that produced it."""

    plan: GenesisAuthoringPlan
    publication: PublicationResult

    @property
    def package_uri(self) -> str:
        """Return the canonical immutable package URI for the durable revision."""
        return format_manifest_uri(
            bucket=self.plan.output.bucket,
            package_name=self.plan.output.name,
            top_hash=self.publication.top_hash,
        )


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _validate_logical_key(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or value.endswith("/"):
        raise GenesisAuthoringError(f"{label} must be a non-empty relative file path")
    if (
        "\\" in value
        or "${" in value
        or any(character in value for character in _FORBIDDEN_LOGICAL_CHARACTERS)
        or _has_control_characters(value)
        or any(ord(character) > 0xFF for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise GenesisAuthoringError(f"unsafe {label}: {value!r}")
    return value


def parse_seal_input_uri(value: str) -> SealInputURI:
    """Parse a strict pinned package URI selecting one logical source object.

    Accepted grammar::

        quilt+s3://BUCKET#package=NAME&top_hash=64_HEX&path=LOGICAL_KEY

    This parser is intentionally separate from :func:`parse_manifest_uri`; the
    ordinary manifest grammar remains unchanged and never accepts a path field.
    """
    if not isinstance(value, str) or not value:
        raise InvalidManifestURI("seal-input URI must be a non-empty string")
    if value != value.strip():
        raise InvalidManifestURI("seal-input URI must not contain surrounding whitespace")
    if _has_control_characters(value):
        raise InvalidManifestURI("seal-input URI must not contain control characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing port forces malformed numeric/bracketed authorities to fail.
        _ = parsed.port
    except ValueError as error:
        raise InvalidManifestURI(f"invalid seal-input URI authority: {error}") from error
    if parsed.scheme != "quilt+s3" or not hostname:
        raise InvalidManifestURI("seal-input URI must use quilt+s3:// with a bucket")
    if parsed.netloc != hostname:
        raise InvalidManifestURI("seal-input URI authority must contain only the bucket name")
    if parsed.path not in {"", "/"} or parsed.query:
        raise InvalidManifestURI(
            "seal-input package, top_hash, and path must be supplied in the URI fragment"
        )
    if _MALFORMED_PERCENT_RE.search(parsed.fragment):
        raise InvalidManifestURI("seal-input URI contains malformed percent encoding")
    try:
        pairs = parse_qsl(
            parsed.fragment,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise InvalidManifestURI(f"invalid seal-input URI fragment: {error}") from error
    fields: dict[str, str] = {}
    for name, field_value in pairs:
        if name in fields:
            raise InvalidManifestURI(f"seal-input URI contains duplicate field {name!r}")
        fields[name] = field_value
    if set(fields) != {"package", "top_hash", "path"}:
        raise InvalidManifestURI(
            "seal-input fragment must contain exactly one package, top_hash, and path"
        )

    pinned_uri = format_manifest_uri(
        bucket=hostname,
        package_name=fields["package"],
        top_hash=fields["top_hash"],
    )
    manifest = parse_manifest_uri(pinned_uri)
    try:
        logical_key = _validate_logical_key(fields["path"], label="seal-input path")
    except GenesisAuthoringError as error:
        raise InvalidManifestURI(str(error)) from error
    return SealInputURI(manifest=manifest, logical_key=logical_key, original=value)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_regular_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    """Return whether two observations describe one unchanged regular file."""
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_directory(
    path: str | os.PathLike[str],
    expected: os.stat_result,
    *,
    label: str,
    parent_fd: int | None = None,
) -> int:
    directory_fd: int | None = None
    try:
        if parent_fd is None:
            directory_fd = os.open(path, _directory_flags())
        else:
            directory_fd = os.open(path, _directory_flags(), dir_fd=parent_fd)
        opened = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise GenesisAuthoringError(f"local genesis path is not a directory: {label}")
        if not _same_file(opened, expected):
            raise GenesisAuthoringError(f"local genesis directory changed during freeze: {label}")
        return directory_fd
    except GenesisAuthoringError:
        if directory_fd is not None:
            os.close(directory_fd)
        raise
    except OSError as error:
        if directory_fd is not None:
            os.close(directory_fd)
        raise GenesisAuthoringError(
            f"unable to open local genesis directory {label}: {error}"
        ) from error


def _open_root_directory(path: Path) -> int:
    try:
        descriptor = path.lstat()
    except OSError as error:
        raise GenesisAuthoringError(
            f"unable to inspect genesis directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(descriptor.st_mode):
        raise GenesisAuthoringError(f"genesis directory must not be a symlink: {path}")
    if not stat.S_ISDIR(descriptor.st_mode):
        raise GenesisAuthoringError(f"genesis authoring source must be a directory: {path}")
    if descriptor.st_mode & _READ_BITS == 0:
        raise GenesisAuthoringError(f"genesis directory is not readable: {path}")
    return _open_directory(path, descriptor, label=str(path))


def _read_regular_file(
    directory_fd: int,
    name: str,
    descriptor: os.stat_result,
    key: str,
) -> bytes:
    if descriptor.st_mode & _READ_BITS == 0:
        raise GenesisAuthoringError(f"local genesis file is not readable: {key!r}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(name, flags, dir_fd=directory_fd)
        opened = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise GenesisAuthoringError(f"local genesis path is not a regular file: {key!r}")
        if not _same_regular_file_state(opened, descriptor):
            raise GenesisAuthoringError(f"local genesis file changed during freeze: {key!r}")
        with os.fdopen(file_descriptor, "rb") as handle:
            file_descriptor = None
            content = handle.read()
            completed = os.fstat(handle.fileno())
            if not _same_regular_file_state(completed, descriptor):
                raise GenesisAuthoringError(f"local genesis file changed during freeze: {key!r}")
    except GenesisAuthoringError:
        raise
    except OSError as error:
        raise GenesisAuthoringError(
            f"unable to read local genesis file {key!r}: {error}"
        ) from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
    return content


def _freeze_directory(
    directory_fd: int,
    parts: tuple[str, ...],
    objects: dict[str, bytes],
) -> None:
    try:
        with os.scandir(directory_fd) as scanner:
            entries = sorted(scanner, key=lambda item: item.name)
    except OSError as error:
        logical = "/".join(parts) or "."
        raise GenesisAuthoringError(
            f"unable to read local genesis directory {logical!r}: {error}"
        ) from error

    for entry in entries:
        key = _validate_logical_key("/".join((*parts, entry.name)), label="local logical key")
        try:
            descriptor = os.stat(
                entry.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise GenesisAuthoringError(
                f"unable to inspect local genesis path {key!r}: {error}"
            ) from error
        if stat.S_ISLNK(descriptor.st_mode):
            raise GenesisAuthoringError(f"local genesis path must not be a symlink: {key!r}")
        if stat.S_ISDIR(descriptor.st_mode):
            if descriptor.st_mode & _READ_BITS == 0:
                raise GenesisAuthoringError(f"local genesis directory is not readable: {key!r}")
            child_fd = _open_directory(
                entry.name,
                descriptor,
                label=repr(key),
                parent_fd=directory_fd,
            )
            try:
                _freeze_directory(child_fd, (*parts, entry.name), objects)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(descriptor.st_mode):
            raise GenesisAuthoringError(f"local genesis path is not a regular file: {key!r}")
        if key in objects:
            raise GenesisAuthoringError(f"local genesis logical-key collision: {key!r}")
        objects[key] = _read_regular_file(directory_fd, entry.name, descriptor, key)


def freeze_genesis_directory(directory: str | os.PathLike[str]) -> Mapping[str, bytes]:
    """Recursively read each safe regular file exactly once into an immutable map."""
    if not _SUPPORTS_DESCRIPTOR_TRAVERSAL:
        raise GenesisAuthoringError(
            "this platform cannot safely traverse genesis files by directory descriptor"
        )
    root = Path(directory)
    root_fd = _open_root_directory(root)
    objects: dict[str, bytes] = {}
    try:
        _freeze_directory(root_fd, (), objects)
    finally:
        os.close(root_fd)
    return MappingProxyType({key: objects[key] for key in sorted(objects)})


def _ensure_json_object_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise GenesisAuthoringError("metadata JSON object keys must be strings")
            if _has_control_characters(key):
                raise GenesisAuthoringError("metadata JSON object keys must not contain controls")
            _ensure_json_object_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _ensure_json_object_keys(child)


def _normalize_metadata(value: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise GenesisAuthoringError("metadata must be a JSON object")
    candidate = dict(value)
    _ensure_json_object_keys(candidate)
    try:
        encoded = json.dumps(candidate, sort_keys=True, separators=(",", ":"), allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise GenesisAuthoringError(f"metadata must be standard JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise TypeError("metadata JSON did not decode to an object")
    return encoded, decoded


def generate_genesis_revision_id(timestamp: datetime) -> str:
    """Return a collision-resistant publication identity valid for Quilt pointers."""
    if timestamp.tzinfo is None:
        raise GenesisAuthoringError("genesis revision timestamp must be timezone-aware")
    stamp = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"genesis-{stamp}-{secrets.token_hex(8)}"


def _manifest_index(objects: Sequence[ManifestObject]) -> dict[str, ManifestObject]:
    indexed: dict[str, ManifestObject] = {}
    for item in objects:
        if item.logical_key in indexed:
            raise ManifestLoadError(
                f"Pinned Quilt manifest contains duplicate logical key: {item.logical_key!r}"
            )
        indexed[item.logical_key] = item
    return indexed


def _seal_provenance(
    selection: SealInputURI,
    descriptor: ManifestObject,
    content: bytes,
) -> SealProvenance:
    if (
        descriptor.version_id is None
        or descriptor.hash_type is None
        or descriptor.hash_value is None
    ):
        raise ManifestLoadError(
            f"Sealed source {selection.logical_key!r} lacks immutable hash provenance"
        )
    return SealProvenance(
        source_uri=format_manifest_uri(
            bucket=selection.manifest.bucket,
            package_name=selection.manifest.package_name,
            top_hash=selection.manifest.top_hash,
        ),
        source_logical_key=selection.logical_key,
        source_bucket=descriptor.bucket,
        source_key=descriptor.key,
        source_version_id=descriptor.version_id,
        hash_type=descriptor.hash_type,
        hash_value=descriptor.hash_value,
        destination_logical_key=selection.destination_logical_key,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _validate_frozen_layout(objects: Mapping[str, bytes], *, handler_key: str) -> None:
    validate_genesis_layout_keys(objects, require_handler=False)
    decode_genesis_readme(objects[README_KEY])
    parse_genesis_outputs(objects[OUTPUTS_KEY])
    parse_runtime_requirements(objects[RUNTIME_REQUIREMENTS_KEY])
    handler_source = objects.get(handler_key)
    if handler_source is None:
        raise ManifestLoadError(
            f"Genesis package is missing selected sealed handler: {handler_key!r}"
        )
    validate_sealed_handler_source(handler_source, handler_key)


def _entry_summaries(
    objects: Mapping[str, bytes],
    sealed_keys: set[str],
) -> tuple[GenesisEntrySummary, ...]:
    return tuple(
        GenesisEntrySummary(
            logical_key=key,
            size=len(objects[key]),
            sha256=hashlib.sha256(objects[key]).hexdigest(),
            origin="sealed" if key in sealed_keys else "local",
        )
        for key in sorted(objects)
    )


def prepare_genesis_authoring(
    directory: str | os.PathLike[str],
    *,
    destination: str,
    user_meta: Mapping[str, Any],
    message: str,
    s3_client: Any,
    revision_id: str,
    seal_inputs: Sequence[str] = (),
    workflow: str = DEFAULT_GENESIS_WORKFLOW,
    handler_key: str = SEALED_HANDLER_KEY,
) -> GenesisAuthoringPlan:
    """Freeze and fully validate one genesis publication without writing anything.

    Source manifests, exact object versions, destination bucket versioning, and the
    named destination workflow are all read through ``s3_client``. The returned
    plan owns immutable byte objects; publication never re-reads the local tree.
    """
    if not isinstance(message, str) or not message.strip():
        raise GenesisAuthoringError("message must be a nonblank string")
    if not isinstance(workflow, str) or not workflow.strip():
        raise GenesisAuthoringError("workflow must be a nonblank named workflow")
    handler_key = _validate_logical_key(handler_key, label="handler key")
    validate_revision_id(revision_id)
    metadata_json, metadata = _normalize_metadata(user_meta)
    parsed_output = parse_output_package(destination)
    output = OutputPackage(
        name=parsed_output.name,
        bucket=parsed_output.bucket,
        revision_id=revision_id,
    )

    frozen_local = freeze_genesis_directory(directory)
    combined = dict(frozen_local)
    if isinstance(seal_inputs, (str, bytes)) or not isinstance(seal_inputs, Sequence):
        raise GenesisAuthoringError("seal_inputs must be a sequence of seal-input URIs")
    selections = tuple(parse_seal_input_uri(value) for value in seal_inputs)
    seen_sources: set[tuple[str, str, str, str]] = set()
    manifest_cache: dict[tuple[str, str, str], dict[str, ManifestObject]] = {}
    provenance: list[SealProvenance] = []
    sealed_keys: set[str] = set()

    for selection in selections:
        source_identity = (
            selection.manifest.bucket,
            selection.manifest.package_name,
            selection.manifest.top_hash,
            selection.logical_key,
        )
        if source_identity in seen_sources:
            raise GenesisAuthoringError(
                f"duplicate sealed source logical key: {selection.logical_key!r}"
            )
        seen_sources.add(source_identity)
        destination_key = selection.destination_logical_key
        if destination_key in combined:
            raise GenesisAuthoringError(
                f"local/sealed destination collision at {destination_key!r}"
            )

        manifest_identity = source_identity[:3]
        indexed = manifest_cache.get(manifest_identity)
        if indexed is None:
            loaded = load_quilt_manifest_from_s3(selection.manifest, s3_client=s3_client)
            indexed = _manifest_index(loaded)
            manifest_cache[manifest_identity] = indexed
        descriptor = indexed.get(selection.logical_key)
        if descriptor is None:
            raise ManifestLoadError(
                f"Pinned source package contains no logical key {selection.logical_key!r}"
            )
        content = read_manifest_object(s3_client, descriptor)
        combined[destination_key] = content
        sealed_keys.add(destination_key)
        provenance.append(_seal_provenance(selection, descriptor, content))

    frozen_objects: Mapping[str, bytes] = MappingProxyType(
        {key: combined[key] for key in sorted(combined)}
    )
    _validate_frozen_layout(frozen_objects, handler_key=handler_key)
    entries = _entry_summaries(frozen_objects, sealed_keys)
    ordered_provenance = tuple(
        sorted(
            provenance,
            key=lambda seal: (
                seal.destination_logical_key,
                seal.source_uri,
                seal.source_logical_key,
                seal.source_bucket,
                seal.source_key,
                seal.source_version_id,
            ),
        )
    )
    declared_intent_json = json.dumps(
        {
            "$schema": _GENESIS_PUBLICATION_INTENT_SCHEMA,
            "handler_key": handler_key,
            "seal_provenance": [seal.report() for seal in ordered_provenance],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    declared_intent = json.loads(declared_intent_json)
    if not isinstance(declared_intent, dict):  # Constructed above as an object.
        raise TypeError("declared intent did not decode to an object")

    versioning = s3_client.get_bucket_versioning(Bucket=output.bucket)
    if not isinstance(versioning, Mapping) or versioning.get("Status") != "Enabled":
        raise PublicationError(f"Output bucket {output.bucket!r} must have versioning enabled")
    if _publication_would_write(
        s3_client=s3_client,
        output=output,
        objects=frozen_objects,
        revision_id=revision_id,
        user_meta=metadata,
        message=message,
        workflow=workflow,
        declared_intent=declared_intent,
    ):
        resolve_workflow_stamp(
            s3_client=s3_client,
            bucket=output.bucket,
            package_name=output.name,
            workflow=workflow,
            user_meta=metadata,
            entries=[
                {"logical_key": entry.logical_key, "size": entry.size, "meta": {}}
                for entry in entries
            ],
            message=message,
        )

    return GenesisAuthoringPlan(
        directory=Path(directory),
        output=output,
        objects=frozen_objects,
        _metadata_json=metadata_json,
        _declared_intent_json=declared_intent_json,
        message=message,
        workflow=workflow,
        revision_id=revision_id,
        entries=entries,
        seals=ordered_provenance,
        handler_key=handler_key,
    )


def publish_genesis_authoring(
    plan: GenesisAuthoringPlan,
    *,
    s3_client: Any,
    timestamp: datetime,
) -> GenesisAuthoringResult:
    """Publish exactly one prepared plan through the canonical revision-safe writer.

    ``publish_package`` deliberately reruns bucket-versioning and workflow
    validation immediately before its first write, closing the preparation-to-
    approval window without weakening or retrying a refused contract.
    """
    publication = publish_package(
        s3_client=s3_client,
        output=plan.output,
        objects=plan.objects,
        timestamp=timestamp,
        revision_id=plan.revision_id,
        user_meta=plan.user_meta,
        message=plan.message,
        workflow=plan.workflow,
        declared_intent=plan.declared_intent,
    )
    return GenesisAuthoringResult(plan=plan, publication=publication)
