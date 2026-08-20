"""Client-side Quilt workflow-contract validation for Scene publication.

Mirrors quilt3's registry workflow semantics — default-workflow resolution,
``is_workflow_required`` (default true when a config exists), metadata and
entries JSON Schemas, ``is_message_required``, ``handle_pattern``, and the
manifest-header stamp shape — while performing every read through the one
caller-supplied S3 client, so a scoped Scene session validates against exactly
the registry its credentials can see. No ambient AWS clients, no listing.

``yaml`` and ``jsonschema`` are hard dependencies of quilt3 (its own workflow
validator imports both), so no new direct dependency is introduced.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jsonschema
import yaml

from .errors import WorkflowContractError

CONFIG_KEY = ".quilt/workflows/config.yml"
_ABSENT_CODES = frozenset({"NoSuchKey", "NoSuchVersion", "404", "NotFound"})
_SUPPORTED_META_SCHEMAS: Mapping[str, Any] = {
    "http://json-schema.org/draft-07/schema#": jsonschema.Draft7Validator,
}


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        inner = response.get("Error")
        if isinstance(inner, Mapping):
            code = inner.get("Code")
            if isinstance(code, str):
                return code
    return None


def _read_versioned(
    s3_client: Any, bucket: str, key: str, version_id: str | None
) -> tuple[bytes, str | None] | None:
    """Read one workflow object; return ``None`` only when it does not exist."""
    request: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id is not None:
        request["VersionId"] = version_id
    try:
        response = s3_client.get_object(**request)
    except Exception as error:
        if _error_code(error) in _ABSENT_CODES:
            return None
        raise WorkflowContractError(
            f"Unable to read workflow object s3://{bucket}/{key}: {error}"
        ) from error
    body = response["Body"]
    try:
        content = body.read()
    finally:
        body.close()
    if not isinstance(content, bytes):
        raise WorkflowContractError(f"Workflow object s3://{bucket}/{key} did not return bytes")
    returned = response.get("VersionId")
    if not isinstance(returned, str) or not returned or returned == "null":
        returned = None
    return content, returned


def _uri(bucket: str, key: str, version_id: str | None) -> str:
    return f"s3://{bucket}/{key}" + (f"?versionId={version_id}" if version_id else "")


def _reject_refs(value: object, schema_id: str) -> None:
    if isinstance(value, Mapping):
        if "$ref" in value:
            raise WorkflowContractError(
                f"Workflow schema {schema_id!r} uses $ref, "
                "which quilt3 workflow validation does not support"
            )
        for child in value.values():
            _reject_refs(child, schema_id)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_refs(child, schema_id)


def _parse_schema_location(schema_id: str, entry: object) -> tuple[str, str, str | None]:
    url = entry.get("url") if isinstance(entry, Mapping) else None
    if not isinstance(url, str) or not url:
        raise WorkflowContractError(f"Workflow schema {schema_id!r} has no url in config")
    parsed = urlsplit(url)
    key = parsed.path.lstrip("/")
    if parsed.scheme != "s3" or not parsed.hostname or not key:
        raise WorkflowContractError(f"Workflow schema {schema_id!r} url must be s3://bucket/key")
    version_id: str | None = None
    if parsed.query:
        values = parse_qs(parsed.query)
        ids = values.get("versionId", [])
        if len(ids) == 1 and ids[0]:
            version_id = ids[0]
    return parsed.hostname, key, version_id


class _SchemaValidator:
    """One loaded, ``$ref``-free JSON Schema and its versioned source URI."""

    def __init__(self, schema_id: str, schema: object, source_uri: str) -> None:
        if isinstance(schema, Mapping) and "$schema" in schema:
            meta_schema = schema["$schema"]
            validator_cls = (
                _SUPPORTED_META_SCHEMAS.get(meta_schema) if isinstance(meta_schema, str) else None
            )
            if validator_cls is None:
                raise WorkflowContractError(
                    f"Workflow schema {schema_id!r} declares unsupported meta-schema "
                    f"{meta_schema!r}"
                )
        else:
            validator_cls = jsonschema.Draft7Validator
        _reject_refs(schema, schema_id)
        try:
            validator_cls.check_schema(schema)
        except jsonschema.SchemaError as error:
            raise WorkflowContractError(
                f"Workflow schema {schema_id!r} is not a valid JSON Schema: {error.message}"
            ) from error
        self.schema_id = schema_id
        self.source_uri = source_uri
        self._validator = validator_cls(schema)

    def validate(self, instance: object, *, subject: str) -> None:
        try:
            self._validator.validate(instance)
        except jsonschema.ValidationError as error:
            raise WorkflowContractError(f"{subject} failed validation: {error.message}") from error


def _load_schema(
    s3_client: Any,
    schemas_config: Mapping[str, Any],
    schema_id: object,
    loaded: dict[str, _SchemaValidator],
) -> _SchemaValidator:
    if not isinstance(schema_id, str) or not schema_id:
        raise WorkflowContractError(f"Workflow names an invalid schema id: {schema_id!r}")
    if schema_id in loaded:
        return loaded[schema_id]
    if schema_id not in schemas_config:
        raise WorkflowContractError(f"There is no {schema_id!r} schema in workflows config")
    bucket, key, version_id = _parse_schema_location(schema_id, schemas_config[schema_id])
    read = _read_versioned(s3_client, bucket, key, version_id)
    if read is None:
        raise WorkflowContractError(
            f"Workflow schema {schema_id!r} not found at s3://{bucket}/{key}"
        )
    content, returned_version = read
    try:
        schema = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise WorkflowContractError(
            f"Workflow schema {schema_id!r} is not valid JSON/YAML: {error}"
        ) from error
    validator = _SchemaValidator(
        schema_id, schema, _uri(bucket, key, version_id or returned_version)
    )
    loaded[schema_id] = validator
    return validator


def resolve_workflow_stamp(
    *,
    s3_client: Any,
    bucket: str,
    package_name: str,
    workflow: object = ...,
    user_meta: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    message: str | None,
) -> dict[str, Any] | None:
    """Validate a pending package against the registry's workflow contract.

    Returns the manifest-header workflow stamp (quilt3-compatible shape), or
    ``None`` when the registry has no workflows config and no workflow was
    named. Raises :class:`WorkflowContractError` — before anything is written —
    when the registry requires a workflow, the named workflow does not exist,
    or the metadata, entries, message, or package name fail the contract.
    """
    if not (workflow is ... or workflow is None or isinstance(workflow, str)):
        raise WorkflowContractError("workflow must be a workflow id string, None, or ellipsis")

    read = _read_versioned(s3_client, bucket, CONFIG_KEY, None)
    if read is None:
        if isinstance(workflow, str):
            raise WorkflowContractError(
                f"Workflow {workflow!r} was requested, but s3://{bucket} has no {CONFIG_KEY}"
            )
        return None
    config_bytes, config_version = read

    try:
        config = yaml.safe_load(config_bytes)
    except yaml.YAMLError as error:
        raise WorkflowContractError(
            f"Invalid workflows config in s3://{bucket}: {error}"
        ) from error
    if not isinstance(config, Mapping) or not isinstance(config.get("workflows"), Mapping):
        raise WorkflowContractError(
            f"Invalid workflows config in s3://{bucket}: expected a mapping with 'workflows'"
        )

    resolved: object = workflow
    if resolved is ...:
        resolved = config.get("default_workflow")
    if resolved is None:
        if config.get("is_workflow_required", True):
            raise WorkflowContractError("Workflow required, but none specified.")
        workflow_data: Mapping[str, Any] = {}
    elif not isinstance(resolved, str) or resolved not in config["workflows"]:
        raise WorkflowContractError(f"There is no {resolved!r} workflow in config.")
    else:
        candidate = config["workflows"][resolved]
        if not isinstance(candidate, Mapping):
            raise WorkflowContractError(f"Workflow {resolved!r} is not a mapping in config")
        workflow_data = candidate

    pattern = workflow_data.get("handle_pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise WorkflowContractError(f"Workflow {resolved!r} handle_pattern must be a string")
        if not re.search(pattern, package_name):
            raise WorkflowContractError("Package name doesn't match required pattern.")

    if workflow_data.get("is_message_required", False) and not message:
        raise WorkflowContractError(
            "Commit message is required by workflow, but none was provided."
        )

    schemas_config = config.get("schemas", {})
    if not isinstance(schemas_config, Mapping):
        raise WorkflowContractError(f"Invalid schemas section in s3://{bucket} workflows config")
    loaded: dict[str, _SchemaValidator] = {}

    metadata_schema_id = workflow_data.get("metadata_schema")
    if metadata_schema_id is not None:
        validator = _load_schema(s3_client, schemas_config, metadata_schema_id, loaded)
        validator.validate(dict(user_meta), subject="Package metadata")

    entries_schema_id = workflow_data.get("entries_schema")
    if entries_schema_id is not None:
        validator = _load_schema(s3_client, schemas_config, entries_schema_id, loaded)
        validator.validate([dict(entry) for entry in entries], subject="Package entries")

    stamp: dict[str, Any] = {
        "id": resolved if isinstance(resolved, str) else None,
        "config": _uri(bucket, CONFIG_KEY, config_version),
    }
    if loaded:
        stamp["schemas"] = {
            schema_id: validator.source_uri for schema_id, validator in sorted(loaded.items())
        }
    return stamp
