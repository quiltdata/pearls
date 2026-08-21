"""Load a Scene handler from the sealed bytes of a pinned genesis revision.

By convention a genesis package carries its executable protocol at
``protocol/handler.py``. The trusted bootstrap identity reads that entry by
exact S3 version, verifies it against the Quilt content hash recorded in the
pinned manifest, and only then compiles and executes those exact bytes.

Hash verification proves *code identity*, not *code safety*. The loaded module
executes in this process with this process's ambient authority, and the handler
it exposes later receives the scoped Scene session. This module is therefore a
provenance boundary, not a sandbox: a genesis package you would not run by hand
is equally dangerous here.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

import boto3

from .errors import HandlerContractError, ManifestLoadError
from .genesis import SEALED_HANDLER_KEY as SEALED_HANDLER_KEY
from .manifest import (
    ManifestLoader,
    load_quilt_manifest,
    parse_manifest_uri,
    read_manifest_object,
)
from .models import ManifestObject, PinnedManifestURI

if TYPE_CHECKING:
    from .runner import SceneHandler

HANDLER_ATTRIBUTE = "handler"
_MODULE_PREFIX = "raja_sts._sealed_handler_"


def validate_sealed_handler_source(source: bytes, handler_key: str) -> None:
    """Check statically that the sealed bytes bind a module-level ``handler``.

    Parsing is not execution: this proves the bytes are valid Python that binds
    the expected name at module level. It cannot prove the binding is callable,
    which is why :meth:`SealedSource.load` re-checks after execution.
    """
    try:
        tree = ast.parse(source, filename=handler_key)
    except (SyntaxError, ValueError) as error:
        raise HandlerContractError(f"Sealed handler is not valid Python: {error}") from error

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == HANDLER_ATTRIBUTE:
                return
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == HANDLER_ATTRIBUTE:
                    return
        elif isinstance(node, ast.AnnAssign):
            # A bare `handler: object` annotates without assigning, so importing
            # the module would leave the attribute absent.
            if (
                node.value is not None
                and isinstance(node.target, ast.Name)
                and node.target.id == HANDLER_ATTRIBUTE
            ):
                return
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == HANDLER_ATTRIBUTE:
                    return
    raise HandlerContractError(
        f"Sealed {handler_key!r} does not bind a module-level {HANDLER_ATTRIBUTE!r}"
    )


@dataclass(frozen=True, slots=True)
class SealedSource:
    """Verified sealed bytes and their provenance, not yet executed.

    Holding this object means the bytes matched the Quilt content hash recorded
    in the pinned manifest and statically bind ``handler``. Nothing from the
    genesis package has run yet, which is what makes a preflight safe.
    """

    source: bytes
    genesis: PinnedManifestURI
    descriptor: ManifestObject
    source_sha256: str

    @property
    def source_size(self) -> int:
        """Return the verified byte count."""
        return len(self.source)

    def report(self) -> dict[str, Any]:
        """Return serializable, non-secret provenance for the verified bytes."""
        return {
            "logical_key": self.descriptor.logical_key,
            "bucket": self.descriptor.bucket,
            "key": self.descriptor.key,
            "version_id": self.descriptor.version_id,
            "quilt_hash": {
                "type": self.descriptor.hash_type,
                "value": self.descriptor.hash_value,
            },
            "source_sha256": self.source_sha256,
            "source_bytes": self.source_size,
            "attribute": HANDLER_ATTRIBUTE,
        }

    def load(self) -> SealedHandler:
        """Execute the verified bytes and return the callable they expose.

        This is the point at which sealed code runs in this process. Callers that
        must not execute genesis code, such as a preflight, stop at
        :class:`SealedSource`.
        """
        module = _compile_sealed_module(
            self.source,
            f"{_MODULE_PREFIX}{self.source_sha256[:16]}",
            f"<sealed {self.genesis.original}#{self.descriptor.logical_key}>",
        )
        candidate = getattr(module, HANDLER_ATTRIBUTE, None)
        if not callable(candidate):
            sys.modules.pop(module.__name__, None)
            raise HandlerContractError(
                f"Sealed {self.descriptor.logical_key!r} must define a callable "
                f"{HANDLER_ATTRIBUTE!r} attribute"
            )
        # The sealed bytes are dynamic, so the runner's contract checks on the
        # returned SceneHandlerResult remain the enforcement point.
        return SealedHandler(
            handler=cast("SceneHandler", candidate),
            genesis=self.genesis,
            descriptor=self.descriptor,
            source_sha256=self.source_sha256,
            source_size=self.source_size,
        )


@dataclass(frozen=True, slots=True)
class SealedHandler:
    """One callable loaded from verified sealed bytes, plus its provenance."""

    handler: SceneHandler
    genesis: PinnedManifestURI
    descriptor: ManifestObject
    source_sha256: str
    source_size: int

    def report(self) -> dict[str, Any]:
        """Return serializable, non-secret provenance for the executed bytes."""
        return {
            "logical_key": self.descriptor.logical_key,
            "bucket": self.descriptor.bucket,
            "key": self.descriptor.key,
            "version_id": self.descriptor.version_id,
            "quilt_hash": {
                "type": self.descriptor.hash_type,
                "value": self.descriptor.hash_value,
            },
            "source_sha256": self.source_sha256,
            "source_bytes": self.source_size,
            "attribute": HANDLER_ATTRIBUTE,
        }


def _compile_sealed_module(source: bytes, module_name: str, origin: str) -> ModuleType:
    module = ModuleType(module_name)
    # Deliberately no __file__: these bytes came from a pinned Quilt revision, not
    # from the filesystem. The origin is recorded for tracebacks and provenance
    # instead of being disguised as a path.
    module.__dict__["__pearls_origin__"] = origin
    try:
        code = compile(source, origin, "exec")
    except (SyntaxError, ValueError) as error:
        raise HandlerContractError(f"Sealed handler is not valid Python: {error}") from error
    # Registered before execution so that dataclasses, typing, and pickle can
    # resolve objects the module defines about itself. The entry is left in
    # place because the returned callable keeps referencing this module.
    sys.modules[module_name] = module
    try:
        exec(code, module.__dict__)  # noqa: S102 - verified sealed bytes, documented boundary
    except Exception as error:
        # pop, not del: the sealed bytes may have unregistered themselves, and a
        # KeyError here would escape as an untyped failure.
        sys.modules.pop(module_name, None)
        raise HandlerContractError(
            f"Sealed handler failed to import: {type(error).__name__}: {error}"
        ) from error
    return module


def verify_sealed_handler(
    genesis_uri: str,
    *,
    manifest_loader: ManifestLoader | None = None,
    genesis_s3_client: Any | None = None,
    handler_key: str = SEALED_HANDLER_KEY,
) -> SealedSource:
    """Verify the sealed handler of a pinned genesis revision without running it.

    Reads ``handler_key`` from the requested revision by exact S3 version, checks
    the bytes against the manifest's Quilt content hash, and checks statically
    that they parse and bind a module-level ``handler``. Raises
    :class:`ManifestLoadError` when the revision declares no such entry,
    :class:`~raja_sts.InputIntegrityError` when the bytes do not match the
    manifest, and :class:`HandlerContractError` when they cannot expose a handler.

    Executes no genesis code, calls no STS, invokes no model, and writes nothing,
    so this is the operation a preflight should use.
    """
    genesis = parse_manifest_uri(genesis_uri)
    loader = manifest_loader or load_quilt_manifest
    descriptor = next(
        (item for item in loader(genesis) if item.logical_key == handler_key),
        None,
    )
    if descriptor is None:
        raise ManifestLoadError(
            f"Genesis package declares no sealed handler at {handler_key!r}; "
            f"pinned revision {genesis.top_hash} cannot be executed by convention"
        )
    client = genesis_s3_client or boto3.client("s3")
    # Hash verification happens inside read_manifest_object, strictly before the
    # bytes are parsed and long before they could be executed.
    source = read_manifest_object(client, descriptor)
    validate_sealed_handler_source(source, handler_key)
    return SealedSource(
        source=source,
        genesis=genesis,
        descriptor=descriptor,
        source_sha256=hashlib.sha256(source).hexdigest(),
    )


def load_sealed_handler(
    genesis_uri: str,
    *,
    manifest_loader: ManifestLoader | None = None,
    genesis_s3_client: Any | None = None,
    handler_key: str = SEALED_HANDLER_KEY,
) -> SealedHandler:
    """Verify and then execute the sealed handler of a pinned genesis revision.

    Equivalent to :func:`verify_sealed_handler` followed by
    :meth:`SealedSource.load`. Sealed code runs in this process, so callers that
    must not execute genesis code should stop at :func:`verify_sealed_handler`.
    """
    return verify_sealed_handler(
        genesis_uri,
        manifest_loader=manifest_loader,
        genesis_s3_client=genesis_s3_client,
        handler_key=handler_key,
    ).load()
