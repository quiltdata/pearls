"""Parse pinned package URIs and resolve Quilt entries without downloading data."""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from quilt3 import checksums

from .errors import (
    AuthorityModeError,
    InputIntegrityError,
    InvalidManifestURI,
    InvalidScopeError,
    ManifestLoadError,
)
from .models import (
    ManifestObject,
    OutputPackage,
    OutputSelector,
    PinnedManifestURI,
    WriteSelectorKind,
)

_BUCKET_RE = re.compile(
    r"^(?!\d+\.\d+\.\d+\.\d+$)(?!.*\.\.)(?!.*\.-)(?!.*-\.)[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
)
_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PACKAGE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FORBIDDEN_RESOURCE_CHARS = frozenset("*?")
_MALFORMED_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_RESERVED_OUTPUT_KEYS = frozenset({"run/scene.json", "run/invocation.json"})

ManifestLoader = Callable[[PinnedManifestURI], Sequence[ManifestObject]]


def _validate_bucket(bucket: str, *, error_type: type[Exception]) -> None:
    if not _BUCKET_RE.fullmatch(bucket):
        raise error_type(f"Invalid S3 bucket name: {bucket!r}")


def _validate_package_name(name: str, *, error_type: type[ValueError]) -> None:
    parts = name.split("/")
    if not name or any(part in {"", ".", ".."} for part in parts):
        raise error_type(f"Invalid Quilt package name: {name!r}")
    if any(not _PACKAGE_SEGMENT_RE.fullmatch(part) for part in parts):
        raise error_type(f"Invalid Quilt package name: {name!r}")


def format_manifest_uri(*, bucket: str, package_name: str, top_hash: str) -> str:
    """Return the canonical URI for one immutable Quilt package revision.

    Package names are encoded as one fragment value, including their ``/``
    separators, so the result round-trips through :func:`parse_manifest_uri`.
    """
    _validate_bucket(bucket, error_type=InvalidManifestURI)
    _validate_package_name(package_name, error_type=InvalidManifestURI)
    if not _HASH_RE.fullmatch(top_hash):
        raise InvalidManifestURI("top_hash must be the complete 64-character hexadecimal hash")
    return f"quilt+s3://{bucket}#package={quote(package_name, safe='')}&top_hash={top_hash.lower()}"


def parse_manifest_uri(manifest_uri: str) -> PinnedManifestURI:
    """Parse a canonical, top-hash-pinned Quilt S3 URI.

    The accepted grammar is::

        quilt+s3://BUCKET#package=NAME&top_hash=64_HEX_CHARACTERS

    Fragment values may be URL encoded. No floating aliases or hash prefixes are
    accepted because authority must be compiled from an immutable revision.
    """
    if not isinstance(manifest_uri, str) or not manifest_uri:
        raise InvalidManifestURI("manifest_uri must be a non-empty string")

    parsed = urlsplit(manifest_uri)
    if parsed.scheme != "quilt+s3" or not parsed.hostname:
        raise InvalidManifestURI("manifest_uri must use quilt+s3:// with a bucket")
    try:
        has_authority_extras = bool(parsed.username or parsed.password or parsed.port)
    except ValueError as error:
        raise InvalidManifestURI(f"invalid manifest URI authority: {error}") from error
    if has_authority_extras:
        raise InvalidManifestURI("manifest_uri must not contain user information or a port")
    if parsed.path not in {"", "/"} or parsed.query:
        raise InvalidManifestURI("package and top_hash must be supplied in the URI fragment")

    _validate_bucket(parsed.hostname, error_type=InvalidManifestURI)
    try:
        values = parse_qs(parsed.fragment, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise InvalidManifestURI(f"invalid manifest URI fragment: {error}") from error
    if set(values) != {"package", "top_hash"} or any(len(items) != 1 for items in values.values()):
        raise InvalidManifestURI("fragment must contain exactly one package and one top_hash")

    package_name = values["package"][0]
    top_hash = values["top_hash"][0]
    _validate_package_name(package_name, error_type=InvalidManifestURI)
    if not _HASH_RE.fullmatch(top_hash):
        raise InvalidManifestURI("top_hash must be the complete 64-character hexadecimal hash")

    return PinnedManifestURI(
        bucket=parsed.hostname,
        package_name=package_name,
        top_hash=top_hash.lower(),
        original=manifest_uri,
    )


def parse_output_package(value: object) -> OutputPackage:
    """Parse and validate ``name@bucket`` from a scope specification."""
    if not isinstance(value, str) or value.count("@") != 1:
        raise InvalidScopeError("writable_package must use the form name@bucket")
    name, bucket = value.rsplit("@", 1)
    _validate_package_name(name, error_type=InvalidScopeError)
    _validate_bucket(bucket, error_type=InvalidScopeError)
    return OutputPackage(name=name, bucket=bucket)


def _parse_output_selector(uri: str, description: str) -> OutputSelector:
    parsed = urlsplit(uri)
    if parsed.scheme != "quilt+s3" or not parsed.hostname:
        raise InvalidScopeError("writable URI must use quilt+s3:// with a bucket")
    try:
        has_authority_extras = bool(parsed.username or parsed.password or parsed.port)
    except ValueError as error:
        raise InvalidScopeError(f"invalid writable URI authority: {error}") from error
    if has_authority_extras or parsed.query:
        raise InvalidScopeError("writable URI must not contain credentials, a port, or a query")

    _validate_bucket(parsed.hostname, error_type=InvalidScopeError)
    try:
        values = parse_qs(parsed.fragment, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise InvalidScopeError(f"invalid writable URI fragment: {error}") from error
    if set(values) != {"package"} or len(values["package"]) != 1:
        raise InvalidScopeError("writable URI fragment must contain exactly one unpinned package")
    package_name = values["package"][0]
    _validate_package_name(package_name, error_type=InvalidScopeError)

    if _MALFORMED_PERCENT_RE.search(parsed.path):
        raise InvalidScopeError(f"writable URI contains malformed percent encoding: {uri!r}")
    try:
        decoded_path = unquote(parsed.path, errors="strict")
    except UnicodeDecodeError as error:
        raise InvalidScopeError(f"writable URI path is not valid UTF-8: {uri!r}") from error

    kind: WriteSelectorKind
    if decoded_path in {"", "/"}:
        path = ""
        kind = "package"
    else:
        path = decoded_path.removeprefix("/")
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or "${" in path
            or any(character in path for character in _FORBIDDEN_RESOURCE_CHARS)
            or any(ord(character) < 0x20 or ord(character) > 0xFF for character in path)
            or any(part in {"", ".", ".."} for part in path.rstrip("/").split("/"))
        ):
            raise InvalidScopeError(f"unsafe writable URI path: {decoded_path!r}")
        kind = "subtree" if path.endswith("/") else "object"
        if kind == "object" and path in _RESERVED_OUTPUT_KEYS:
            raise InvalidScopeError(f"writable URI selects reserved output key: {path!r}")
        if kind == "subtree" and any(key.startswith(path) for key in _RESERVED_OUTPUT_KEYS):
            raise InvalidScopeError(
                f"writable URI subtree contains a reserved output key: {path!r}"
            )

    return OutputSelector(
        bucket=parsed.hostname,
        package_name=package_name,
        path=path,
        kind=kind,
        original=uri,
        description=description,
    )


def _selectors_overlap(left: OutputSelector, right: OutputSelector) -> bool:
    if (left.bucket, left.package_name) != (right.bucket, right.package_name):
        return False
    if left.kind == "package" or right.kind == "package":
        return True
    if left.kind == "object" and right.kind == "object":
        return left.path == right.path
    return (left.kind == "subtree" and right.path.startswith(left.path)) or (
        right.kind == "subtree" and left.path.startswith(right.path)
    )


def parse_output_selectors(value: object) -> tuple[OutputSelector, ...]:
    """Parse ``{unpinned quilt+ URI: description}`` into non-overlapping grants."""
    if not isinstance(value, Mapping) or not value:
        raise InvalidScopeError("writable must be a non-empty URI-to-description mapping")

    selectors: list[OutputSelector] = []
    for uri, description in value.items():
        if not isinstance(uri, str) or not uri:
            raise InvalidScopeError("each writable URI must be a non-empty string")
        if not isinstance(description, str) or not description.strip():
            raise InvalidScopeError("each writable description must be a non-empty string")
        selector = _parse_output_selector(uri, description)
        if selectors and (selector.bucket, selector.package_name) != (
            selectors[0].bucket,
            selectors[0].package_name,
        ):
            raise InvalidScopeError("all writable selectors must target one output package")
        overlap = next(
            (existing for existing in selectors if _selectors_overlap(existing, selector)),
            None,
        )
        if overlap is not None:
            raise InvalidScopeError(
                f"overlapping writable selectors are ambiguous: {overlap.original!r} and {uri!r}"
            )
        selectors.append(selector)
    return tuple(sorted(selectors, key=lambda item: (item.path, item.kind, item.original)))


def _validate_physical_key(key: str) -> None:
    if not key or key.startswith("/"):
        raise ManifestLoadError(f"Manifest contains an invalid S3 key: {key!r}")
    if any(character in key for character in _FORBIDDEN_RESOURCE_CHARS) or "${" in key:
        raise ManifestLoadError(f"Manifest key would widen an IAM resource ARN: {key!r}")
    if any(ord(character) < 0x20 or ord(character) > 0xFF for character in key):
        raise ManifestLoadError(
            f"Manifest key cannot be represented safely in an IAM policy: {key!r}"
        )


def _entry_to_object(logical_key: str, entry: Any) -> ManifestObject:
    physical_key = entry.physical_key
    bucket = getattr(physical_key, "bucket", None)
    key = getattr(physical_key, "path", None)
    version_id = getattr(physical_key, "version_id", None)
    if not isinstance(bucket, str) or not isinstance(key, str):
        raise ManifestLoadError(f"Manifest entry {logical_key!r} is not backed by S3")
    _validate_bucket(bucket, error_type=ManifestLoadError)
    _validate_physical_key(key)
    if version_id == "null":
        version_id = None
    if version_id is not None and (
        not isinstance(version_id, str)
        or not version_id
        or "${" in version_id
        or any(ord(character) < 0x20 or ord(character) > 0xFF for character in version_id)
    ):
        raise ManifestLoadError(f"Manifest entry {logical_key!r} has an invalid version ID")

    hash_object = getattr(entry, "hash", None) or {}
    return ManifestObject(
        logical_key=logical_key,
        bucket=bucket,
        key=key,
        version_id=version_id,
        hash_type=hash_object.get("type"),
        hash_value=hash_object.get("value"),
    )


def _verified_manifest_objects(
    package: Any,
    manifest: PinnedManifestURI,
) -> tuple[ManifestObject, ...]:
    actual_hash = package.top_hash
    if not isinstance(actual_hash, str) or actual_hash.lower() != manifest.top_hash:
        raise ManifestLoadError(
            f"Loaded manifest hash {actual_hash!r} does not match {manifest.top_hash!r}"
        )
    objects = tuple(_entry_to_object(key, entry) for key, entry in package.walk())
    if not objects:
        raise ManifestLoadError("Pinned Quilt manifest contains no objects")
    return objects


def load_quilt_manifest(manifest: PinnedManifestURI) -> tuple[ManifestObject, ...]:
    """Load and verify a pinned Quilt manifest through Quilt's registry client."""
    try:
        from quilt3 import Package

        package = Package.browse(
            manifest.package_name,
            registry=manifest.registry,
            top_hash=manifest.top_hash,
        )
        return _verified_manifest_objects(package, manifest)
    except ManifestLoadError:
        raise
    except Exception as error:
        raise ManifestLoadError(f"Unable to load pinned Quilt manifest: {error}") from error


def load_quilt_manifest_from_s3(
    manifest: PinnedManifestURI,
    *,
    s3_client: Any,
) -> tuple[ManifestObject, ...]:
    """Load a pinned Quilt manifest through exactly one caller-supplied S3 client.

    The content-addressed manifest bytes are parsed in memory with Quilt's local
    serializer. Their semantic top hash is recomputed before any physical entry is
    trusted; no registry lookup, telemetry call, or ambient AWS client is used.
    """
    try:
        from quilt3 import Package

        response = s3_client.get_object(
            Bucket=manifest.bucket,
            Key=f".quilt/packages/{manifest.top_hash}",
        )
        body = response["Body"]
        try:
            content = body.read()
        finally:
            body.close()
        if not isinstance(content, bytes):
            raise TypeError("manifest body did not return bytes")
        package = Package._load(io.StringIO(content.decode("utf-8")))
        return _verified_manifest_objects(package, manifest)
    except ManifestLoadError:
        raise
    except Exception as error:
        raise ManifestLoadError(
            "Unable to load pinned Quilt manifest through the supplied S3 client"
        ) from error


def validate_manifest_object_integrity(item: ManifestObject) -> None:
    """Admit only descriptors the runner can pin and verify before minting."""
    if (
        item.version_id is None
        or item.hash_type is None
        or not isinstance(item.hash_value, str)
        or not item.hash_value
    ):
        raise AuthorityModeError(
            f"Selected input {item.logical_key!r} lacks a version ID or content hash"
        )
    supported = {
        checksums.SHA256_HASH_NAME,
        checksums.SHA256_CHUNKED_HASH_NAME,
        checksums.CRC64NVME_HASH_NAME,
    }
    if item.hash_type not in supported:
        raise AuthorityModeError(
            f"Selected input {item.logical_key!r} uses unsupported Quilt content hash type "
            f"{item.hash_type!r}"
        )


def _calculate_hash(hash_type: str, content: bytes) -> str:
    if hash_type == checksums.SHA256_HASH_NAME:
        result = checksums.legacy_calculate_checksum_bytes(content)
    elif hash_type in (checksums.SHA256_CHUNKED_HASH_NAME, checksums.CRC64NVME_HASH_NAME):
        result = checksums.calculate_multipart_checksum_bytes(content, checksum_type=hash_type)
    else:
        raise InputIntegrityError(f"Unsupported Quilt content hash type: {hash_type!r}")
    if not isinstance(result, str):
        raise InputIntegrityError(f"Invalid Quilt content hash result for {hash_type!r}")
    return result


def read_manifest_object(
    s3_client: Any,
    item: ManifestObject,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Read one exact S3 version, optionally bound its bytes, and verify its hash."""
    validate_manifest_object_integrity(item)
    assert item.version_id is not None
    assert item.hash_type is not None
    assert item.hash_value is not None
    if max_bytes is not None and (not isinstance(max_bytes, int) or max_bytes < 0):
        raise ValueError("max_bytes must be a nonnegative integer or None")
    try:
        response = s3_client.get_object(
            Bucket=item.bucket,
            Key=item.key,
            VersionId=item.version_id,
        )
        if response.get("VersionId") != item.version_id:
            raise InputIntegrityError(
                f"S3 returned the wrong version for selected input {item.logical_key!r}"
            )
        body = response["Body"]
        try:
            content = body.read() if max_bytes is None else body.read(max_bytes + 1)
        finally:
            body.close()
        if not isinstance(content, bytes):
            raise TypeError("S3 body did not return bytes")
        if max_bytes is not None and len(content) > max_bytes:
            raise InputIntegrityError(
                f"Selected input {item.logical_key!r} exceeds {max_bytes} bytes"
            )
    except InputIntegrityError:
        raise
    except Exception as error:
        raise InputIntegrityError(
            f"Unable to read selected input {item.logical_key!r}: {error}"
        ) from error

    actual = _calculate_hash(item.hash_type, content)
    if actual != item.hash_value:
        raise InputIntegrityError(f"Content hash mismatch for selected input {item.logical_key!r}")
    return content
