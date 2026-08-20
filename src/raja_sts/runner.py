"""One-shot execution of a version-pinned, least-authority Scene."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
from quilt3 import checksums

from .errors import (
    AuthorityModeError,
    HandlerContractError,
    InputIntegrityError,
    InvalidScopeError,
    ManifestLoadError,
)
from .manifest import ManifestLoader, load_quilt_manifest, parse_manifest_uri
from .models import CompiledPolicy, ManifestObject, OutputPackage, OutputSelector, PinnedManifestURI
from .policy import DEFAULT_MAX_POLICY_CHARACTERS, compile
from .publication import publish_package
from .sts import mint_compiled

SCENE_RECORD_SCHEMA = "https://quiltdata.com/schemas/raja-sts/scene-execution/v1"
_RESERVED_KEYS = frozenset({"run/scene.json", "run/invocation.json"})
_SECRET_KEY_FRAGMENTS = ("access_key", "secret", "session_token", "credential", "password")


@dataclass(frozen=True, slots=True)
class MaterializedInput:
    """One selected immutable input and its verified bytes."""

    descriptor: ManifestObject
    content: bytes


@dataclass(frozen=True, slots=True)
class SceneContext:
    """Non-secret authority context passed to a Scene handler."""

    scene_id: str
    compilation: CompiledPolicy
    control_prompt: str | None = None

    @property
    def bedrock_model_arn(self) -> str:
        """Return the exact model selected by the sealed scope."""
        model = self.compilation.bedrock_model_arn
        if model is None:  # The runner rejects this before constructing a context.
            raise AuthorityModeError("Scene scope does not select a Bedrock model")
        return model


@dataclass(frozen=True, slots=True)
class SceneHandlerResult:
    """Objects and safe invocation metadata returned by one handler call."""

    objects: Mapping[str, bytes | str]
    invocation: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SceneRunResult:
    """Non-secret identity and audit result for a completed Scene."""

    scene_id: str
    output_package: OutputPackage
    top_hash: str
    assumed_role_arn: str
    started_at: datetime
    completed_at: datetime

    @property
    def package_uri(self) -> str:
        """Return the immutable output package URI."""
        return (
            f"quilt+s3://{self.output_package.bucket}#package="
            f"{self.output_package.name}&top_hash={self.top_hash}"
        )


SceneHandler = Callable[
    [Mapping[str, MaterializedInput], Any, SceneContext],
    SceneHandlerResult,
]


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


def _read_manifest_object(s3_client: Any, item: ManifestObject) -> bytes:
    if item.version_id is None or item.hash_type is None or item.hash_value is None:
        raise AuthorityModeError(
            f"Selected input {item.logical_key!r} lacks a version ID or content hash"
        )
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
            content = body.read()
        finally:
            body.close()
        if not isinstance(content, bytes):
            raise TypeError("S3 body did not return bytes")
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


def _read_inputs(s3_client: Any, compilation: CompiledPolicy) -> dict[str, MaterializedInput]:
    return {
        item.logical_key: MaterializedInput(item, _read_manifest_object(s3_client, item))
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
            normalized = str(key).lower().replace("-", "_")
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
) -> tuple[dict[str, bytes], bytes]:
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
    return objects, invocation_bytes


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _iso8601(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Scene clock must return timezone-aware datetimes")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _model_region(model_arn: str) -> str:
    return model_arn.split(":", 5)[3]


def _validate_runner_authority(compilation: CompiledPolicy) -> None:
    if compilation.mode != "per-object-version":
        raise AuthorityModeError(
            f"Scene runner requires per-object-version authority, got {compilation.mode!r}"
        )
    if compilation.bedrock_model_arn is None:
        raise AuthorityModeError("Scene runner requires one exact bedrock_model resource")


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
    output_meta: Mapping[str, Any] | None = None,
    output_message: str | None = None,
    output_workflow: str | None | object = ...,
) -> SceneRunResult:
    _validate_runner_authority(compilation)
    credentials = mint_compiled(
        compilation,
        role_arn=role_arn,
        scene_id=scene_id,
        duration_seconds=duration_seconds,
        external_id=external_id,
        sts_client=sts_client,
    )
    model_arn = compilation.bedrock_model_arn
    if model_arn is None:  # Narrowed by _validate_runner_authority.
        raise AuthorityModeError("Scene runner requires one exact bedrock_model resource")
    session = session_factory(
        region_name=_model_region(model_arn),
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
    objects, invocation_bytes = _validate_handler_result(
        handler_result,
        secret_values,
        compilation.writable_selectors,
    )
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
        },
        "timestamps": {
            "started_at": _iso8601(started_at),
            "completed_at": _iso8601(completed_at),
        },
    }
    objects["run/scene.json"] = _json_bytes(scene_record)
    objects["run/invocation.json"] = invocation_bytes
    top_hash = publish_package(
        s3_client=s3_client,
        output=compilation.output_package,
        objects=objects,
        timestamp=completed_at,
        user_meta=output_meta,
        message=output_message,
        workflow=output_workflow,
    )
    return SceneRunResult(
        scene_id=scene_id,
        output_package=compilation.output_package,
        top_hash=top_hash,
        assumed_role_arn=credentials.assumed_role_arn,
        started_at=started_at,
        completed_at=completed_at,
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
    sts_client: Any | None = None,
    max_policy_characters: int = DEFAULT_MAX_POLICY_CHARACTERS,
    session_factory: Callable[..., Any] = boto3.Session,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    output_meta: Mapping[str, Any] | None = None,
    output_message: str | None = None,
    output_workflow: str | None | object = ...,
) -> SceneRunResult:
    """Compile, mint, execute, and publish exactly one bounded Scene.

    ``output_meta``, ``output_message``, and ``output_workflow`` are applied to
    the published output package and validated against the destination
    registry's workflow contract before anything is written (see
    :func:`raja_sts.publication.publish_package`).
    """
    started_at = clock()
    compilation = compile(
        manifest_uri,
        scope_spec,
        manifest_loader=manifest_loader,
        max_policy_characters=max_policy_characters,
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
    )


def _genesis_objects(
    genesis: PinnedManifestURI,
    loader: ManifestLoader,
) -> tuple[tuple[ManifestObject, ...], dict[str, ManifestObject]]:
    objects = tuple(loader(genesis))
    by_key: dict[str, ManifestObject] = {}
    for item in objects:
        if item.logical_key in by_key:
            raise ManifestLoadError(
                f"Genesis package contains duplicate logical key: {item.logical_key!r}"
            )
        by_key[item.logical_key] = item
    missing = {"README.md", "outputs.json"} - set(by_key)
    if missing:
        raise ManifestLoadError(
            "Genesis package is missing required control object(s): " + ", ".join(sorted(missing))
        )
    if not any(item.logical_key.startswith("input/") for item in objects):
        raise ManifestLoadError("Genesis package must contain at least one input/ object")
    return objects, by_key


def _parse_outputs_json(content: bytes) -> dict[str, object]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidScopeError(
            f"outputs.json must contain a UTF-8 JSON object: {error}"
        ) from error
    if not isinstance(value, dict):
        raise InvalidScopeError("outputs.json must contain a URI-to-description object")
    return value


def run_scene_from_genesis(
    genesis_uri: str,
    *,
    role_arn: str,
    scene_id: str,
    handler: SceneHandler,
    bedrock_model: str,
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
) -> SceneRunResult:
    """Boot one Scene from a pinned genesis package and its immutable controls.

    ``output_meta``, ``output_message``, and ``output_workflow`` are applied to
    the published output package and validated against the destination
    registry's workflow contract before anything is written.
    """
    started_at = clock()
    genesis = parse_manifest_uri(genesis_uri)
    loader = manifest_loader or load_quilt_manifest
    objects, by_key = _genesis_objects(genesis, loader)
    control_client = genesis_s3_client or boto3.client("s3")
    prompt_bytes = _read_manifest_object(control_client, by_key["README.md"])
    try:
        control_prompt = prompt_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManifestLoadError("Genesis README.md must be UTF-8 text") from error
    writable = _parse_outputs_json(_read_manifest_object(control_client, by_key["outputs.json"]))
    compilation = compile(
        genesis_uri,
        {
            "readable": ["input/"],
            "writable": writable,
            "bedrock_model": bedrock_model,
            "require_version_ids": True,
        },
        manifest_loader=lambda _: objects,
        max_policy_characters=max_policy_characters,
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
        control_prompt=control_prompt,
        output_meta=output_meta,
        output_message=output_message,
        output_workflow=output_workflow,
    )
