from __future__ import annotations

import json
from typing import Any

import pytest

from raja_sts import WorkflowContractError
from raja_sts.workflows import CONFIG_KEY, resolve_workflow_stamp

BUCKET = "output-bucket"

CONFIG = """
version: "1"
is_workflow_required: true
default_workflow: trial-run
workflows:
  trial-run:
    name: Trial run
    metadata_schema: trial-run-v1
    entries_schema: run-entries-v1
    is_message_required: true
  loose:
    name: No schemas, message optional
  patterned:
    name: Name-gated
    handle_pattern: "^trials/"
schemas:
  trial-run-v1:
    url: s3://output-bucket/.quilt/workflows/trial-run-v1.json
  run-entries-v1:
    url: s3://output-bucket/.quilt/workflows/run-entries-v1.json?versionId=pinned-sv
"""

METADATA_SCHEMA = json.dumps(
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["trial", "limitations"],
        "properties": {"trial": {"type": "integer"}, "limitations": {"type": "string"}},
    }
).encode()

ENTRIES_SCHEMA = json.dumps(
    {
        "type": "array",
        "contains": {
            "type": "object",
            "properties": {"logical_key": {"const": "content/observations.csv"}},
            "required": ["logical_key"],
        },
    }
).encode()


class NoSuchKey(Exception):
    def __init__(self) -> None:
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class Body:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self) -> bytes:
        return self._content

    def close(self) -> None:
        return None


class WorkflowS3:
    """Serves workflow config/schema objects; records requested versions."""

    def __init__(self, objects: dict[str, tuple[bytes, str]]) -> None:
        self.objects = objects
        self.requests: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        entry = self.objects.get(kwargs["Key"])
        if entry is None:
            raise NoSuchKey()
        content, version = entry
        return {"Body": Body(content), "VersionId": version}


def _client(config: bytes = CONFIG.encode()) -> WorkflowS3:
    return WorkflowS3(
        {
            CONFIG_KEY: (config, "config-v1"),
            ".quilt/workflows/trial-run-v1.json": (METADATA_SCHEMA, "meta-sv"),
            ".quilt/workflows/run-entries-v1.json": (ENTRIES_SCHEMA, "pinned-sv"),
        }
    )


GOOD_META = {"trial": 0, "limitations": "smoke test only"}
GOOD_ENTRIES = [
    {"logical_key": "content/observations.csv", "size": 3, "meta": {}},
    {"logical_key": "run/scene.json", "size": 9, "meta": {}},
]


def _resolve(client: WorkflowS3, **overrides: Any) -> dict[str, Any] | None:
    request: dict[str, Any] = {
        "s3_client": client,
        "bucket": BUCKET,
        "package_name": "trials/trial001",
        "user_meta": GOOD_META,
        "entries": GOOD_ENTRIES,
        "message": "run one",
    }
    request.update(overrides)
    return resolve_workflow_stamp(**request)


def test_absent_config_with_default_workflow_returns_no_stamp() -> None:
    assert _resolve(WorkflowS3({})) is None
    assert _resolve(WorkflowS3({}), workflow=None) is None


def test_absent_config_with_named_workflow_is_refused() -> None:
    with pytest.raises(WorkflowContractError, match="has no .quilt/workflows"):
        _resolve(WorkflowS3({}), workflow="trial-run")


def test_explicit_none_is_refused_when_required() -> None:
    with pytest.raises(WorkflowContractError, match="Workflow required, but none specified."):
        _resolve(_client(), workflow=None)


def test_requirement_defaults_to_true_when_config_exists() -> None:
    config = CONFIG.replace("is_workflow_required: true\n", "").replace(
        "default_workflow: trial-run\n", ""
    )
    with pytest.raises(WorkflowContractError, match="Workflow required"):
        _resolve(_client(config.encode()))


def test_explicit_none_allowed_when_not_required_stamps_null_id() -> None:
    config = CONFIG.replace("is_workflow_required: true", "is_workflow_required: false")
    stamp = _resolve(_client(config.encode()), workflow=None)
    assert stamp == {"id": None, "config": f"s3://{BUCKET}/{CONFIG_KEY}?versionId=config-v1"}


def test_default_workflow_resolution_and_stamp_shape() -> None:
    stamp = _resolve(_client())
    assert stamp is not None
    assert stamp["id"] == "trial-run"
    assert stamp["config"] == f"s3://{BUCKET}/{CONFIG_KEY}?versionId=config-v1"
    assert stamp["schemas"] == {
        "run-entries-v1": f"s3://{BUCKET}/.quilt/workflows/run-entries-v1.json?versionId=pinned-sv",
        "trial-run-v1": f"s3://{BUCKET}/.quilt/workflows/trial-run-v1.json?versionId=meta-sv",
    }


def test_pinned_schema_url_requests_that_version() -> None:
    client = _client()
    _resolve(client)
    pinned = [
        request for request in client.requests if request["Key"].endswith("run-entries-v1.json")
    ]
    assert pinned == [
        {
            "Bucket": BUCKET,
            "Key": ".quilt/workflows/run-entries-v1.json",
            "VersionId": "pinned-sv",
        }
    ]


def test_unknown_workflow_is_refused() -> None:
    with pytest.raises(WorkflowContractError, match="There is no 'missing' workflow in config."):
        _resolve(_client(), workflow="missing")


def test_metadata_schema_refusal_names_the_missing_field() -> None:
    with pytest.raises(
        WorkflowContractError, match="Package metadata failed validation.*limitations"
    ):
        _resolve(_client(), user_meta={"trial": 0})


def test_entries_schema_refusal() -> None:
    with pytest.raises(WorkflowContractError, match="Package entries failed validation"):
        _resolve(_client(), entries=[{"logical_key": "run/scene.json", "size": 9, "meta": {}}])


def test_message_required_refusal() -> None:
    with pytest.raises(WorkflowContractError, match="Commit message is required by workflow"):
        _resolve(_client(), message=None)


def test_workflow_without_schemas_or_message_requirement_passes() -> None:
    stamp = _resolve(_client(), workflow="loose", message=None, user_meta={})
    assert stamp is not None
    assert stamp["id"] == "loose"
    assert "schemas" not in stamp


def test_handle_pattern_mismatch_is_refused() -> None:
    with pytest.raises(WorkflowContractError, match="doesn't match required pattern"):
        _resolve(_client(), workflow="patterned", package_name="scratch/other")
    stamp = _resolve(_client(), workflow="patterned")
    assert stamp is not None and stamp["id"] == "patterned"


def test_ref_in_schema_is_refused() -> None:
    schema = json.dumps(
        {"type": "object", "properties": {"trial": {"$ref": "#/definitions/x"}}}
    ).encode()
    client = _client()
    client.objects[".quilt/workflows/trial-run-v1.json"] = (schema, "meta-sv")
    with pytest.raises(WorkflowContractError, match=r"uses \$ref"):
        _resolve(client)


def test_unsupported_meta_schema_is_refused() -> None:
    schema = json.dumps(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
    ).encode()
    client = _client()
    client.objects[".quilt/workflows/trial-run-v1.json"] = (schema, "meta-sv")
    with pytest.raises(WorkflowContractError, match="unsupported meta-schema"):
        _resolve(client)


def test_invalid_schema_document_is_refused() -> None:
    client = _client()
    client.objects[".quilt/workflows/trial-run-v1.json"] = (
        json.dumps({"type": "no-such-type"}).encode(),
        "meta-sv",
    )
    with pytest.raises(WorkflowContractError, match="not a valid JSON Schema"):
        _resolve(client)


def test_missing_schema_object_is_refused() -> None:
    client = WorkflowS3({CONFIG_KEY: (CONFIG.encode(), "config-v1")})
    with pytest.raises(WorkflowContractError, match="not found"):
        _resolve(client)


def test_unlisted_schema_id_is_refused() -> None:
    config = CONFIG.replace("metadata_schema: trial-run-v1", "metadata_schema: absent-id")
    with pytest.raises(WorkflowContractError, match="There is no 'absent-id' schema"):
        _resolve(_client(config.encode()))


@pytest.mark.parametrize("config", [b"- just\n- a list\n", b"workflows: 3\n"])
def test_malformed_config_is_refused(config: bytes) -> None:
    with pytest.raises(WorkflowContractError, match="Invalid workflows config"):
        _resolve(_client(config))


def test_unreadable_config_is_wrapped() -> None:
    class Exploding:
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("socket closed")

    with pytest.raises(WorkflowContractError, match="Unable to read workflow object"):
        _resolve(Exploding())  # type: ignore[arg-type]


def test_invalid_workflow_argument_type_is_refused() -> None:
    with pytest.raises(WorkflowContractError, match="workflow must be"):
        _resolve(_client(), workflow=7)


def test_non_absence_error_shapes_are_wrapped_not_treated_as_missing() -> None:
    class WeirdError(Exception):
        response = {"Error": {"Code": 123}}

    class WeirdS3:
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            raise WeirdError()

    with pytest.raises(WorkflowContractError, match="Unable to read workflow object"):
        _resolve(WeirdS3())  # type: ignore[arg-type]


def test_non_bytes_workflow_object_is_refused() -> None:
    class TextBody:
        def read(self) -> str:
            return "not-bytes"

        def close(self) -> None:
            return None

    class TextS3:
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            return {"Body": TextBody(), "VersionId": "v1"}

    with pytest.raises(WorkflowContractError, match="did not return bytes"):
        _resolve(TextS3())  # type: ignore[arg-type]


def test_null_config_version_omits_version_from_stamp() -> None:
    config = CONFIG.replace("is_workflow_required: true", "is_workflow_required: false")
    client = WorkflowS3({CONFIG_KEY: (config.encode(), "null")})
    stamp = _resolve(client, workflow=None)
    assert stamp == {"id": None, "config": f"s3://{BUCKET}/{CONFIG_KEY}"}


def test_ref_nested_in_a_list_is_refused() -> None:
    schema = json.dumps({"allOf": [{"$ref": "#/definitions/x"}]}).encode()
    client = _client()
    client.objects[".quilt/workflows/trial-run-v1.json"] = (schema, "meta-sv")
    with pytest.raises(WorkflowContractError, match=r"uses \$ref"):
        _resolve(client)


def test_schema_entry_without_string_url_is_refused() -> None:
    config = CONFIG.replace(
        "url: s3://output-bucket/.quilt/workflows/trial-run-v1.json\n", "url: 3\n"
    )
    with pytest.raises(WorkflowContractError, match="has no url|must be s3"):
        _resolve(_client(config.encode()))


def test_non_string_schema_id_is_refused() -> None:
    config = CONFIG.replace("metadata_schema: trial-run-v1", "metadata_schema: 3")
    with pytest.raises(WorkflowContractError, match="invalid schema id"):
        _resolve(_client(config.encode()))


def test_shared_schema_id_is_loaded_once() -> None:
    config = CONFIG.replace("entries_schema: run-entries-v1", "entries_schema: trial-run-v1")
    client = _client(config.encode())
    with pytest.raises(WorkflowContractError, match="Package entries failed validation"):
        # entries list does not satisfy the object-shaped metadata schema
        _resolve(client)
    schema_reads = [
        request for request in client.requests if request["Key"].endswith("trial-run-v1.json")
    ]
    assert len(schema_reads) == 1


def test_unparseable_schema_document_is_refused() -> None:
    client = _client()
    client.objects[".quilt/workflows/trial-run-v1.json"] = (b"[unclosed", "meta-sv")
    with pytest.raises(WorkflowContractError, match="not valid JSON/YAML"):
        _resolve(client)
