"""One-shot execution of a version-pinned, least-authority Scene."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import boto3

from .bedrock import BedrockInvocation, BedrockProfileLoader, bedrock_model_region
from .errors import (
    AuthorityModeError,
    HandlerContractError,
    InvalidScopeError,
    PublicationError,
)
from .genesis import (
    RUNTIME_REQUIREMENTS_KEY,
    RUNTIME_REQUIREMENTS_MAX_BYTES,
    RuntimeRequirementsReport,
    decode_genesis_readme,
    parse_genesis_outputs,
    parse_runtime_requirements,
    validate_genesis_layout_keys,
    verify_runtime_requirements,
)
from .manifest import (
    ManifestLoader,
    format_manifest_uri,
    load_quilt_manifest,
    parse_manifest_uri,
    read_manifest_object,
    validate_manifest_object_integrity,
)
from .models import CompiledPolicy, ManifestObject, OutputPackage, OutputSelector, PinnedManifestURI
from .policy import DEFAULT_MAX_POLICY_CHARACTERS, compile
from .publication import PublicationInvariant, PublicationResult, publish_package
from .sts import mint_compiled

SCENE_RECORD_SCHEMA = "https://quiltdata.com/schemas/pearls/scene-execution/v1"
_RESERVED_KEYS = frozenset({"run/scene.json", "run/invocation.json"})
_SECRET_KEY_FRAGMENTS = ("access_key", "secret", "session_token", "credential", "password")


@dataclass(frozen=True, slots=True)
class MaterializedInput:
    """One selected immutable input and its verified bytes."""

    descriptor: ManifestObject
    content: bytes


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeRequirements:
    """Sealed control provenance and local compatibility checked before STS."""

    descriptor: ManifestObject
    source_sha256: str
    source_size: int
    compatibility: RuntimeRequirementsReport

    def report(self) -> dict[str, Any]:
        """Return exact control evidence plus bounded local compatibility results."""
        return {
            "control": {
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
            },
            "compatibility": self.compatibility.report(),
        }


@dataclass(frozen=True, slots=True)
class SceneContext:
    """Non-secret authority context passed to a Scene handler."""

    scene_id: str
    compilation: CompiledPolicy
    control_prompt: str | None = None

    @property
    def bedrock_invocation(self) -> BedrockInvocation:
        """Return the selected Bedrock identifier and exact IAM resource closure."""
        invocation = self.compilation.bedrock_invocation
        if invocation is None:  # The runner rejects this before constructing a context.
            raise AuthorityModeError("Scene scope does not select a Bedrock model")
        return invocation

    @property
    def bedrock_model_id(self) -> str:
        """Return the exact modelId selected by the sealed Scene scope."""
        return self.bedrock_invocation.selected_model_id

    @property
    def bedrock_model_arn(self) -> str:
        """Return the selected modelId (compatibility alias for ``bedrock_model_id``)."""
        return self.bedrock_model_id

    @property
    def bedrock_invoke_resources(self) -> tuple[str, ...]:
        """Return every exact IAM resource needed for the selected invocation."""
        return self.bedrock_invocation.invoke_resources


@dataclass(frozen=True, slots=True)
class SceneHandlerResult:
    """Objects, invocation evidence, and run-derived package metadata."""

    objects: Mapping[str, bytes | str]
    invocation: Mapping[str, Any]
    output_meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SceneRunResult:
    """Non-secret identity and audit result for a completed Scene."""

    scene_id: str
    output_package: OutputPackage
    publication: PublicationResult
    assumed_role_arn: str
    started_at: datetime
    completed_at: datetime
    runtime_requirements: VerifiedRuntimeRequirements | None = None

    @property
    def top_hash(self) -> str:
        """Return the immutable published top hash (compatibility accessor)."""
        return self.publication.top_hash

    @property
    def package_uri(self) -> str:
        """Return the canonical immutable output package URI."""
        return format_manifest_uri(
            bucket=self.output_package.bucket,
            package_name=self.output_package.name,
            top_hash=self.top_hash,
        )


SceneHandler = Callable[
    [Mapping[str, MaterializedInput], Any, SceneContext],
    SceneHandlerResult,
]


def _read_inputs(s3_client: Any, compilation: CompiledPolicy) -> dict[str, MaterializedInput]:
    return {
        item.logical_key: MaterializedInput(item, read_manifest_object(s3_client, item))
        for item in compilation.selected_objects
    }


def _validate_logical_key(key: object) -> str:
    if not isinstance(key, str) or not key or key.startswith("/") or key.endswith("/"):
        raise HandlerContractError("handler output keys must be relative file paths")
    if "\\" in key or "*" in key or "?" in key or "${" in key:
        raise HandlerContractError(f"unsafe handler output key: {key!r}")
    if any(part in {"", ".", ".."} for part in key.split("/")):
        raise HandlerContractError(f"unsafe handler output key: {key!r}")
    if key in _RESERVED_KEYS:
        raise HandlerContractError(f"handler cannot replace reserved output {key!r}")
    return key


def _contains_secret(value: object, secret_values: frozenset[str]) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if any(secret and secret in key_text for secret in secret_values):
                return True
            normalized = key_text.lower().replace("-", "_")
            if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
                return True
            if _contains_secret(child, secret_values):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(child, secret_values) for child in value)
    return isinstance(value, str) and any(secret and secret in value for secret in secret_values)


def _validate_handler_result(
    result: object,
    secret_values: frozenset[str],
    writable_selectors: Sequence[OutputSelector] = (),
) -> tuple[dict[str, bytes], bytes, dict[str, Any]]:
    if not isinstance(result, SceneHandlerResult):
        raise HandlerContractError("handler must return SceneHandlerResult")
    if not isinstance(result.objects, Mapping) or not result.objects:
        raise HandlerContractError("handler must return at least one output object")

    objects: dict[str, bytes] = {}
    for raw_key, value in result.objects.items():
        key = _validate_logical_key(raw_key)
        if writable_selectors and not any(selector.admits(key) for selector in writable_selectors):
            raise HandlerContractError(
                f"handler output key is outside the genesis write scope: {key!r}"
            )
        if not isinstance(value, (bytes, str)):
            raise HandlerContractError(f"handler output {key!r} must be bytes or str")
        objects[key] = value.encode("utf-8") if isinstance(value, str) else value

    if not isinstance(result.invocation, Mapping):
        raise HandlerContractError("handler invocation metadata must be a mapping")
    if _contains_secret(result.invocation, secret_values):
        raise HandlerContractError("handler invocation metadata contains credential material")
    try:
        invocation_bytes = _json_bytes(dict(result.invocation))
    except (TypeError, ValueError) as error:
        raise HandlerContractError(
            f"handler invocation metadata must be JSON-serializable: {error}"
        ) from error

    if not isinstance(result.output_meta, Mapping):
        raise HandlerContractError("handler output metadata must be a mapping")
    if _contains_secret(result.output_meta, secret_values):
        raise HandlerContractError("handler output metadata contains credential material")
    handler_meta = dict(result.output_meta)
    try:
        _json_bytes(handler_meta)
    except (TypeError, ValueError) as error:
        raise HandlerContractError(
            f"handler output metadata must be JSON-serializable: {error}"
        ) from error
    return objects, invocation_bytes, handler_meta


def _merge_output_meta(
    caller_meta: Mapping[str, Any] | None,
    handler_meta: Mapping[str, Any],
    secret_values: frozenset[str],
) -> dict[str, Any]:
    if caller_meta is not None and not isinstance(caller_meta, Mapping):
        raise PublicationError("output_meta must be a mapping")
    declared = dict(caller_meta) if caller_meta else {}
    if _contains_secret(declared, secret_values):
        raise PublicationError("output_meta contains credential material")
    try:
        _json_bytes(declared)
    except (TypeError, ValueError) as error:
        raise PublicationError(f"output_meta must be JSON-serializable: {error}") from error
    collisions = set(declared).intersection(handler_meta)
    if collisions:
        raise HandlerContractError(
            "handler output metadata collides with caller metadata: "
            + ", ".join(repr(key) for key in sorted(collisions, key=str))
        )
    return {**declared, **handler_meta}


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _iso8601(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Scene clock must return timezone-aware datetimes")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_runner_authority(compilation: CompiledPolicy) -> BedrockInvocation:
    if compilation.mode != "prefix-scoped":
        raise AuthorityModeError(
            f"Scene runner requires prefix-scoped authority, got {compilation.mode!r}"
        )
    for item in compilation.selected_objects:
        validate_manifest_object_integrity(item)
    try:
        invocation = compilation.bedrock_invocation
    except InvalidScopeError as error:
        raise AuthorityModeError(
            f"Scene runner received invalid Bedrock authority: {error}"
        ) from error
    if invocation is None:
        raise AuthorityModeError("Scene runner requires one exact bedrock_model selection")
    return invocation


def _execute_compiled_scene(
    *,
    compilation: CompiledPolicy,
    role_arn: str,
    scene_id: str,
    handler: SceneHandler,
    started_at: datetime,
    duration_seconds: int,
    external_id: str | None,
    sts_client: Any | None,
    session_factory: Callable[..., Any],
    clock: Callable[[], datetime],
    control_prompt: str | None = None,
    runtime_requirements: VerifiedRuntimeRequirements | None = None,
    output_meta: Mapping[str, Any] | None = None,
    output_message: str | None = None,
    output_workflow: str | None | object = ...,
    output_invariants: Sequence[PublicationInvariant] = (),
) -> SceneRunResult:
    invocation = _validate_runner_authority(compilation)
    credentials = mint_compiled(
        compilation,
        role_arn=role_arn,
        scene_id=scene_id,
        duration_seconds=duration_seconds,
        external_id=external_id,
        sts_client=sts_client,
    )
    session = session_factory(
        region_name=bedrock_model_region(invocation.selected_model_id),
        **credentials.boto3_kwargs(),
    )
    s3_client = session.client("s3")
    inputs = _read_inputs(s3_client, compilation)
    context = SceneContext(
        scene_id=scene_id,
        compilation=compilation,
        control_prompt=control_prompt,
    )
    handler_result = handler(inputs, session, context)
    secret_values = frozenset(credentials.boto3_kwargs().values())
    objects, invocation_bytes, handler_output_meta = _validate_handler_result(
        handler_result,
        secret_values,
        compilation.writable_selectors,
    )
    merged_output_meta = _merge_output_meta(output_meta, handler_output_meta, secret_values)
    completed_at = clock()

    scene_record: dict[str, Any] = {
        "$schema": SCENE_RECORD_SCHEMA,
        "scene_id": scene_id,
        "input": {
            "manifest_uri": compilation.manifest.original,
            "top_hash": compilation.manifest.top_hash,
            "selected_objects": [
                {
                    "logical_key": item.logical_key,
                    "bucket": item.bucket,
                    "key": item.key,
                    "version_id": item.version_id,
                    "hash": {"type": item.hash_type, "value": item.hash_value},
                }
                for item in compilation.selected_objects
            ],
        },
        "authority": compilation.report(),
        "assumed_role": {
            "arn": credentials.assumed_role_arn,
            "id": credentials.assumed_role_id,
            "packed_policy_size": credentials.packed_policy_size,
        },
        "output": {
            "name": compilation.output_package.name,
            "bucket": compilation.output_package.bucket,
            "revision_id": scene_id,
        },
        "timestamps": {
            "started_at": _iso8601(started_at),
            "completed_at": _iso8601(completed_at),
        },
    }
    objects["run/scene.json"] = _json_bytes(scene_record)
    objects["run/invocation.json"] = invocation_bytes
    publication = publish_package(
        s3_client=s3_client,
        output=compilation.output_package,
        objects=objects,
        timestamp=completed_at,
        revision_id=scene_id,
        user_meta=merged_output_meta,
        message=output_message,
        workflow=output_workflow,
        invariants=output_invariants,
    )
    return SceneRunResult(
        scene_id=scene_id,
        output_package=compilation.output_package,
        publication=publication,
        assumed_role_arn=credentials.assumed_role_arn,
        started_at=started_at,
        completed_at=completed_at,
        runtime_requirements=runtime_requirements,
    )


def run_scene(
    *,
    manifest_uri: str,
    scope_spec: Mapping[str, object],
    role_arn: str,
    scene_id: str,
    handler: SceneHandler,
    duration_seconds: int = 3600,
    external_id: str | None = None,
    manifest_loader: ManifestLoader | None = None,
    bedrock_profile_loader: BedrockProfileLoader | None = None,
    sts_client: Any | None = None,
    max_policy_characters: int = DEFAULT_MAX_POLICY_CHARACTERS,
    session_factory: Callable[..., Any] = boto3.Session,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    output_meta: Mapping[str, Any] | None = None,
    output_message: str | None = None,
    output_workflow: str | None | object = ...,
    output_invariants: Sequence[PublicationInvariant] = (),
) -> SceneRunResult:
    """Compile, mint, execute, and publish exactly one bounded Scene.

    Caller and handler ``output_meta`` are merged without collisions before the
    workflow contract and ``output_invariants`` validate the final revision.
    """
    started_at = clock()
    compilation = compile(
        manifest_uri,
        scope_spec,
        manifest_loader=manifest_loader,
        max_policy_characters=max_policy_characters,
        include_manifest_read=False,
        bedrock_profile_loader=bedrock_profile_loader,
        output_revision_id=scene_id,
    )
    return _execute_compiled_scene(
        compilation=compilation,
        role_arn=role_arn,
        scene_id=scene_id,
        handler=handler,
        started_at=started_at,
        duration_seconds=duration_seconds,
        external_id=external_id,
        sts_client=sts_client,
        session_factory=session_factory,
        clock=clock,
        output_meta=output_meta,
        output_message=output_message,
        output_workflow=output_workflow,
        output_invariants=output_invariants,
    )


def _genesis_objects(
    genesis: PinnedManifestURI,
    loader: ManifestLoader,
) -> tuple[tuple[ManifestObject, ...], dict[str, ManifestObject]]:
    objects = tuple(loader(genesis))
    validate_genesis_layout_keys(
        (item.logical_key for item in objects),
        require_handler=False,
    )
    return objects, {item.logical_key: item for item in objects}


@dataclass(frozen=True, slots=True)
class GenesisPreflight:
    """Everything derived from a pinned genesis package before any STS call."""

    genesis: PinnedManifestURI
    objects: tuple[ManifestObject, ...]
    by_logical_key: Mapping[str, ManifestObject]
    control_prompt: str
    writable: Mapping[str, object]
    runtime_requirements: VerifiedRuntimeRequirements
    compilation: CompiledPolicy


def preflight_genesis(
    genesis_uri: str,
    *,
    bedrock_model: str,
    bedrock_invoke_resources: Sequence[str] | None = None,
    bedrock_profile_loader: BedrockProfileLoader | None = None,
    manifest_loader: ManifestLoader | None = None,
    genesis_s3_client: Any | None = None,
    max_policy_characters: int = DEFAULT_MAX_POLICY_CHARACTERS,
    output_revision_id: str | None = None,
) -> GenesisPreflight:
    """Resolve, verify, and compile one pinned genesis package without minting.

    Performs only the reads the trusted bootstrap identity must perform: the
    pinned manifest, then ``README.md``, ``outputs.json``, and
    ``protocol/requirements.json`` by exact S3 version with their Quilt content
    hashes verified. Runtime compatibility is checked without importing module
    bodies. Calls no STS, invokes no model, and writes nothing, so it is safe as
    a preflight. :func:`run_scene_from_genesis` uses this same function, so a
    passing preflight and a run agree by construction on controls, compatibility,
    authority, and compiled policy.
    """
    genesis = parse_manifest_uri(genesis_uri)
    loader = manifest_loader or load_quilt_manifest
    objects, by_key = _genesis_objects(genesis, loader)
    control_client = genesis_s3_client or boto3.client("s3")
    prompt_bytes = read_manifest_object(control_client, by_key["README.md"])
    control_prompt = decode_genesis_readme(prompt_bytes)
    writable, _selectors = parse_genesis_outputs(
        read_manifest_object(control_client, by_key["outputs.json"])
    )
    requirements_descriptor = by_key[RUNTIME_REQUIREMENTS_KEY]
    requirements_source = read_manifest_object(
        control_client,
        requirements_descriptor,
        max_bytes=RUNTIME_REQUIREMENTS_MAX_BYTES,
    )
    runtime_requirements = VerifiedRuntimeRequirements(
        descriptor=requirements_descriptor,
        source_sha256=hashlib.sha256(requirements_source).hexdigest(),
        source_size=len(requirements_source),
        compatibility=verify_runtime_requirements(parse_runtime_requirements(requirements_source)),
    )
    scope: dict[str, object] = {
        "readable": ["input/"],
        "writable": writable,
        "bedrock_model": bedrock_model,
        "require_version_ids": True,
    }
    if bedrock_invoke_resources is not None:
        scope["bedrock_invoke_resources"] = tuple(bedrock_invoke_resources)
    compilation = compile(
        genesis_uri,
        scope,
        manifest_loader=lambda _: objects,
        max_policy_characters=max_policy_characters,
        include_manifest_read=False,
        bedrock_profile_loader=bedrock_profile_loader,
        output_revision_id=output_revision_id,
    )
    for item in compilation.selected_objects:
        validate_manifest_object_integrity(item)
    return GenesisPreflight(
        genesis=genesis,
        objects=objects,
        by_logical_key=by_key,
        control_prompt=control_prompt,
        writable=writable,
        runtime_requirements=runtime_requirements,
        compilation=compilation,
    )


def run_scene_from_genesis(
    genesis_uri: str,
    *,
    role_arn: str,
    scene_id: str,
    handler: SceneHandler,
    bedrock_model: str,
    bedrock_invoke_resources: Sequence[str] | None = None,
    bedrock_profile_loader: BedrockProfileLoader | None = None,
    duration_seconds: int = 3600,
    external_id: str | None = None,
    manifest_loader: ManifestLoader | None = None,
    genesis_s3_client: Any | None = None,
    sts_client: Any | None = None,
    max_policy_characters: int = DEFAULT_MAX_POLICY_CHARACTERS,
    session_factory: Callable[..., Any] = boto3.Session,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    output_meta: Mapping[str, Any] | None = None,
    output_message: str | None = None,
    output_workflow: str | None | object = ...,
    output_invariants: Sequence[PublicationInvariant] = (),
) -> SceneRunResult:
    """Boot one Scene from a pinned genesis package and its immutable controls.

    Caller and handler ``output_meta`` are merged without collisions before the
    workflow contract and ``output_invariants`` validate the final revision.
    """
    started_at = clock()
    preflight = preflight_genesis(
        genesis_uri,
        bedrock_model=bedrock_model,
        bedrock_invoke_resources=bedrock_invoke_resources,
        bedrock_profile_loader=bedrock_profile_loader,
        manifest_loader=manifest_loader,
        genesis_s3_client=genesis_s3_client,
        max_policy_characters=max_policy_characters,
        output_revision_id=scene_id,
    )
    return _execute_compiled_scene(
        compilation=preflight.compilation,
        role_arn=role_arn,
        scene_id=scene_id,
        handler=handler,
        started_at=started_at,
        duration_seconds=duration_seconds,
        external_id=external_id,
        sts_client=sts_client,
        session_factory=session_factory,
        clock=clock,
        control_prompt=preflight.control_prompt,
        runtime_requirements=preflight.runtime_requirements,
        output_meta=output_meta,
        output_message=output_message,
        output_workflow=output_workflow,
        output_invariants=output_invariants,
    )
