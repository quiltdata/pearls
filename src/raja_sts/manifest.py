"""Parse pinned package URIs and resolve Quilt entries without downloading data."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import InvalidManifestURI, InvalidScopeError, ManifestLoadError
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
    if left.kind == "subtree":
        return right.path.startswith(left.path)
    return left.path.startswith(right.path)


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


def load_quilt_manifest(manifest: PinnedManifestURI) -> tuple[ManifestObject, ...]:
    """Load and verify a pinned Quilt manifest using the caller's AWS identity."""
    try:
        from quilt3 import Package

        package = Package.browse(
            manifest.package_name,
            registry=manifest.registry,
            top_hash=manifest.top_hash,
        )
        actual_hash = package.top_hash
        if actual_hash.lower() != manifest.top_hash:
            raise ManifestLoadError(
                f"Loaded manifest hash {actual_hash!r} does not match {manifest.top_hash!r}"
            )
        objects = tuple(_entry_to_object(key, entry) for key, entry in package.walk())
    except ManifestLoadError:
        raise
    except Exception as error:
        raise ManifestLoadError(f"Unable to load pinned Quilt manifest: {error}") from error

    if not objects:
        raise ManifestLoadError("Pinned Quilt manifest contains no objects")
    return objects
