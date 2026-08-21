from __future__ import annotations

import json
from pathlib import Path

import pytest
from authoring_support import (
    AUTHOR_DESTINATION,
    AUTHOR_MESSAGE,
    AUTHOR_METADATA,
    WORKFLOW_CONFIG,
    AuthoringS3,
    Stored,
    write_valid_genesis,
)
from cli_support import FakeSession, Streams, make_runtime

from raja_sts import parse_manifest_uri
from raja_sts.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, main


class GenesisHarness:
    """One fully offline ``pearls genesis author`` invocation."""

    def __init__(
        self,
        directory: Path,
        *,
        handler_key: str = "protocol/handler.py",
    ) -> None:
        self.directory = directory
        write_valid_genesis(directory, handler_key=handler_key)
        self.s3 = AuthoringS3()
        self.session = FakeSession({"s3": self.s3}, region_name="us-east-1")
        self.streams = Streams()

    def invoke(self, *extra: str, answers: tuple[str, ...] = ()) -> int:
        runtime = make_runtime(
            streams=self.streams,
            session=self.session,
            answers=answers,
        )
        return main(
            [
                "genesis",
                "author",
                str(self.directory),
                "--destination",
                AUTHOR_DESTINATION,
                "--metadata",
                json.dumps(AUTHOR_METADATA),
                "--message",
                AUTHOR_MESSAGE,
                *extra,
            ],
            runtime=runtime,
        )


def test_dry_run_validates_everything_without_prompt_or_write(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)

    code = harness.invoke("--dry-run", "--json")

    assert code == EXIT_OK
    payload = harness.streams.json()
    assert payload["$schema"] == "https://quiltdata.com/schemas/raja-sts/cli-genesis-author/v1"
    assert payload["status"] == "validated"
    assert payload["dry_run"] is True
    assert payload["mutations_performed"] == []
    assert payload["planned_revision_id"].startswith("genesis-20260820T120000Z-")
    assert payload["output"] == {
        "bucket": "output-bucket",
        "is_latest": None,
        "latest_top_hash": None,
        "manifest_version_id": None,
        "package_name": "proj/genesis",
        "pinned_uri": None,
        "previous_top_hash": None,
        "reused_revision": None,
        "revision_id": None,
        "revision_version_id": None,
        "top_hash": None,
    }
    assert payload["metadata_keys"] == ["owner"]
    assert payload["handler_key"] == "protocol/handler.py"
    assert payload["message_provided"] is True
    assert [entry["logical_key"] for entry in payload["entries"]] == sorted(
        entry["logical_key"] for entry in payload["entries"]
    )
    assert harness.s3.put_requests == []
    assert harness.session.requested == ["s3"]


def test_dry_run_human_output_reports_no_writes_and_no_fabricated_uri(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)

    assert harness.invoke("--dry-run") == EXIT_OK

    assert "dry-run performed no writes" in harness.streams.stdout
    assert "planned id  genesis-20260820T120000Z-" in harness.streams.stdout
    assert "quilt+s3://output-bucket#package=" not in harness.streams.stdout
    assert harness.streams.stderr == ""
    assert harness.s3.put_requests == []


def test_explicit_revision_id_is_preserved_as_a_nondurable_dry_run_identity(
    tmp_path: Path,
) -> None:
    harness = GenesisHarness(tmp_path)
    revision_id = "genesis-retry-001"

    code = harness.invoke("--revision-id", revision_id, "--dry-run", "--json")

    assert code == EXIT_OK
    payload = harness.streams.json()
    assert payload["planned_revision_id"] == revision_id
    assert payload["output"]["revision_id"] is None
    assert payload["output"]["revision_version_id"] is None
    assert payload["output"]["manifest_version_id"] is None
    assert payload["output"]["top_hash"] is None
    assert payload["output"]["pinned_uri"] is None
    assert harness.s3.put_requests == []


@pytest.mark.parametrize("revision_id", ["x", "latest", "bad/id", "a" * 65])
def test_invalid_revision_id_is_refused_before_creating_an_operator_session(
    tmp_path: Path,
    revision_id: str,
) -> None:
    harness = GenesisHarness(tmp_path)

    code = harness.invoke("--revision-id", revision_id, "--dry-run")

    assert code == EXIT_REFUSED
    assert "PublicationError" in harness.streams.stderr
    assert "revision_id" in harness.streams.stderr
    assert harness.session.requested == []
    assert harness.s3.put_requests == []


@pytest.mark.parametrize(
    ("answers", "expected"),
    [((), "re-run with --yes"), (("no",), "confirmation declined")],
    ids=["no-stdin", "declined"],
)
def test_real_publication_requires_confirmation_after_preparation(
    tmp_path: Path,
    answers: tuple[str, ...],
    expected: str,
) -> None:
    harness = GenesisHarness(tmp_path)

    code = harness.invoke(answers=answers)

    assert code == EXIT_REFUSED
    assert expected in harness.streams.stderr
    assert "Prepared genesis publication for approval" in harness.streams.stderr
    assert "revision id: genesis-20260820T120000Z-" in harness.streams.stderr
    assert harness.s3.put_requests == []


def test_real_json_publication_reports_stable_non_secret_receipt(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)
    seal_uri = harness.s3.add_source(content=b"sealed body must not print")

    code = harness.invoke("--seal-input", seal_uri, "--yes", "--json")

    assert code == EXIT_OK
    payload = harness.streams.json()
    assert payload["$schema"].endswith("/cli-genesis-author/v1")
    assert payload["status"] == "published"
    assert payload["dry_run"] is False
    assert payload["mutations_performed"] == ["publish_package"]
    assert payload["metadata_keys"] == ["owner"]
    assert payload["handler_key"] == "protocol/handler.py"
    assert payload["workflow"] == "genesis"
    assert payload["planned_revision_id"] == payload["output"]["revision_id"]
    assert payload["output"]["revision_id"].startswith("genesis-20260820T120000Z-")
    assert payload["output"]["revision_version_id"]
    assert payload["output"]["manifest_version_id"]
    assert payload["output"]["is_latest"] is True
    assert payload["output"]["reused_revision"] is False
    assert payload["output"]["latest_top_hash"] == payload["output"]["top_hash"]
    parsed = parse_manifest_uri(payload["output"]["pinned_uri"])
    assert parsed.package_name == "proj/genesis"
    assert parsed.top_hash == payload["output"]["top_hash"]
    assert payload["seal_provenance"][0]["destination_logical_key"] == ("input/evidence/source.txt")
    serialized = json.dumps(payload)
    assert "operator" not in serialized
    assert AUTHOR_MESSAGE not in serialized
    assert "sealed body must not print" not in serialized
    for secret in ("temporary-access", "temporary-secret", "temporary-token"):
        assert secret not in serialized
    assert harness.session.requested == ["s3"]


def test_human_publication_prints_canonical_pinned_uri_first(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)

    assert harness.invoke("--yes") == EXIT_OK

    first_line = harness.streams.stdout.splitlines()[0]
    parsed = parse_manifest_uri(first_line)
    assert parsed.package_name == "proj/genesis"
    assert "%2F" in first_line
    assert "Prepared genesis publication" in harness.streams.stderr
    assert AUTHOR_MESSAGE not in harness.streams.stderr


def test_latest_cas_loss_is_successful_and_preserved_in_receipt(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)
    harness.s3.fail_latest = True

    assert harness.invoke("--yes", "--json") == EXIT_OK

    payload = harness.streams.json()
    assert payload["status"] == "published-not-latest"
    assert payload["mutations_performed"] == ["publish_package"]
    assert payload["output"]["is_latest"] is False
    assert payload["output"]["revision_version_id"]
    assert payload["output"]["pinned_uri"]


def test_same_revision_id_recovers_lost_response_without_duplicate_writes_or_rollback(
    tmp_path: Path,
) -> None:
    harness = GenesisHarness(tmp_path)
    revision_id = "genesis-retry-001"
    revision_key = f".quilt/named_packages/proj/genesis/{revision_id}"
    latest_key = ".quilt/named_packages/proj/genesis/latest"
    harness.s3.lose_revision_response = True

    first_code = harness.invoke("--revision-id", revision_id, "--yes")

    assert first_code == EXIT_REFUSED
    assert revision_id in harness.streams.stderr
    assert "simulated response loss" in harness.streams.stderr
    durable_revision = harness.s3.current[("output-bucket", revision_key)]
    assert ("output-bucket", latest_key) not in harness.s3.current
    write_count = len(harness.s3.put_requests)
    written_keys = [str(request["Key"]) for request in harness.s3.put_requests]
    assert all(written_keys.count(key) == 1 for key in set(written_keys))

    newer_latest = Stored(
        b"f" * 64,
        "newer-latest-version",
        etag='"newer-latest-etag"',
    )
    harness.s3.current[("output-bucket", latest_key)] = newer_latest
    harness.streams = Streams()

    retry_code = harness.invoke("--revision-id", revision_id, "--yes", "--json")

    assert retry_code == EXIT_OK
    payload = harness.streams.json()
    assert payload["status"] == "published-not-latest"
    assert payload["mutations_performed"] == []
    assert payload["planned_revision_id"] == revision_id
    assert payload["output"]["revision_id"] == revision_id
    assert payload["output"]["revision_version_id"] == durable_revision.version_id
    assert payload["output"]["reused_revision"] is True
    assert payload["output"]["latest_top_hash"] == "f" * 64
    assert len(harness.s3.put_requests) == write_count
    assert harness.s3.current[("output-bucket", revision_key)] == durable_revision
    assert harness.s3.current[("output-bucket", latest_key)] == newer_latest


def test_completed_retry_skips_changed_workflow_when_no_write_is_needed(
    tmp_path: Path,
) -> None:
    harness = GenesisHarness(tmp_path)
    revision_id = "genesis-completed-retry"

    assert harness.invoke("--revision-id", revision_id, "--yes") == EXIT_OK

    harness.s3.remote[("output-bucket", ".quilt/workflows/genesis-metadata.json", None)] = Stored(
        b'{"type":"object","required":["replacement"]}',
        "metadata-schema-v2",
    )
    harness.s3.get_requests.clear()
    write_count = len(harness.s3.put_requests)
    harness.streams = Streams()

    assert harness.invoke("--revision-id", revision_id, "--yes", "--json") == EXIT_OK

    payload = harness.streams.json()
    assert payload["status"] == "published"
    assert payload["mutations_performed"] == []
    assert payload["output"]["reused_revision"] is True
    assert len(harness.s3.put_requests) == write_count
    assert all(
        not str(request["Key"]).startswith(".quilt/workflows/")
        for request in harness.s3.get_requests
    )


def test_superseded_retry_skips_deleted_workflow_without_rollback(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)
    revision_id = "genesis-superseded-retry"
    latest_key = ".quilt/named_packages/proj/genesis/latest"
    harness.s3.fail_latest = True

    assert harness.invoke("--revision-id", revision_id, "--yes") == EXIT_OK

    harness.s3.fail_latest = False
    newer_latest = Stored(
        b"f" * 64,
        "newer-latest-version",
        etag='"newer-latest-etag"',
    )
    harness.s3.current[("output-bucket", latest_key)] = newer_latest
    harness.s3.remote.clear()
    harness.s3.get_requests.clear()
    write_count = len(harness.s3.put_requests)
    harness.streams = Streams()

    assert harness.invoke("--revision-id", revision_id, "--yes", "--json") == EXIT_OK

    payload = harness.streams.json()
    assert payload["status"] == "published-not-latest"
    assert payload["mutations_performed"] == []
    assert payload["output"]["reused_revision"] is True
    assert payload["output"]["latest_top_hash"] == "f" * 64
    assert len(harness.s3.put_requests) == write_count
    assert harness.s3.current[("output-bucket", latest_key)] == newer_latest
    assert all(
        not str(request["Key"]).startswith(".quilt/workflows/")
        for request in harness.s3.get_requests
    )


def test_exact_base_interrupted_retry_reports_successful_latest_mutation(
    tmp_path: Path,
) -> None:
    harness = GenesisHarness(tmp_path)
    revision_id = "genesis-interrupted-recovery"
    harness.s3.fail_latest = True

    assert harness.invoke("--revision-id", revision_id, "--yes") == EXIT_OK

    harness.s3.fail_latest = False
    write_count = len(harness.s3.put_requests)
    harness.streams = Streams()

    assert harness.invoke("--revision-id", revision_id, "--yes", "--json") == EXIT_OK

    payload = harness.streams.json()
    assert payload["status"] == "published"
    assert payload["mutations_performed"] == ["publish_package"]
    assert payload["output"]["reused_revision"] is True
    assert payload["output"]["is_latest"] is True
    assert len(harness.s3.put_requests) == write_count + 1
    assert harness.s3.put_requests[-1]["Key"].endswith("/latest")
    assert harness.s3.put_requests[-1]["IfNoneMatch"] == "*"


def test_exact_base_interrupted_retry_revalidates_before_latest_write(
    tmp_path: Path,
) -> None:
    harness = GenesisHarness(tmp_path)
    revision_id = "genesis-interrupted-retry"
    latest_key = ".quilt/named_packages/proj/genesis/latest"
    harness.s3.fail_latest = True

    assert harness.invoke("--revision-id", revision_id, "--yes") == EXIT_OK

    harness.s3.fail_latest = False
    harness.s3.remote.clear()
    harness.s3.get_requests.clear()
    write_count = len(harness.s3.put_requests)
    harness.streams = Streams()

    assert harness.invoke("--revision-id", revision_id, "--yes") == EXIT_REFUSED

    assert "WorkflowContractError" in harness.streams.stderr
    assert len(harness.s3.put_requests) == write_count
    assert ("output-bucket", latest_key) not in harness.s3.current
    assert any(
        str(request["Key"]).startswith(".quilt/workflows/") for request in harness.s3.get_requests
    )


def test_profile_and_region_apply_only_to_the_operator_s3_session(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)

    assert (
        harness.invoke(
            "--dry-run",
            "--profile",
            "author-profile",
            "--region",
            "us-west-2",
        )
        == EXIT_OK
    )

    assert harness.session.factory_kwargs == {
        "profile_name": "author-profile",
        "region_name": "us-west-2",
    }
    assert harness.session.requested == ["s3"]


def test_metadata_file_and_explicit_named_workflow_reach_preparation(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path / "tree")
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps(AUTHOR_METADATA), encoding="utf-8")

    code = main(
        [
            "genesis",
            "author",
            str(harness.directory),
            "--destination",
            AUTHOR_DESTINATION,
            "--metadata",
            f"@{metadata}",
            "--message",
            AUTHOR_MESSAGE,
            "--workflow",
            "genesis",
            "--dry-run",
            "--json",
        ],
        runtime=make_runtime(streams=harness.streams, session=harness.session),
    )

    assert code == EXIT_OK
    assert harness.streams.json()["workflow"] == "genesis"


def test_changed_handler_declaration_cannot_relabel_reused_revision(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)
    supervisor = tmp_path / "protocol" / "supervisor.py"
    supervisor.write_bytes((tmp_path / "protocol" / "handler.py").read_bytes())
    revision_id = "genesis-handler-binding"

    assert harness.invoke("--revision-id", revision_id, "--yes", "--json") == EXIT_OK

    write_count = len(harness.s3.put_requests)
    harness.streams = Streams()
    code = harness.invoke(
        "--revision-id",
        revision_id,
        "--handler-key",
        "protocol/supervisor.py",
        "--yes",
        "--json",
    )

    assert code == EXIT_REFUSED
    assert "PublicationConflictError" in harness.streams.stderr
    assert "different publication intent" in harness.streams.stderr
    assert harness.streams.stdout == ""
    assert len(harness.s3.put_requests) == write_count


def test_custom_handler_key_is_validated_and_reported_by_cli(tmp_path: Path) -> None:
    handler_key = "protocol/supervisor.py"
    harness = GenesisHarness(tmp_path, handler_key=handler_key)

    code = harness.invoke("--handler-key", handler_key, "--dry-run", "--json")

    assert code == EXIT_OK
    payload = harness.streams.json()
    assert payload["handler_key"] == handler_key
    assert any(entry["logical_key"] == handler_key for entry in payload["entries"])
    assert all(entry["logical_key"] != "protocol/handler.py" for entry in payload["entries"])
    assert harness.s3.put_requests == []


def test_workflow_refusal_propagates_verbatim_before_writes(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)

    code = harness.invoke("--workflow", "missing", "--yes")

    assert code == EXIT_REFUSED
    assert "WorkflowContractError" in harness.streams.stderr
    assert "missing" in harness.streams.stderr
    assert harness.s3.put_requests == []


@pytest.mark.parametrize(
    "pattern",
    [
        "[",
        "a{999999999999999999999999999999}",
        "(" * 1000,
    ],
    ids=["syntax", "overflow", "recursion"],
)
@pytest.mark.parametrize("arguments", [("--dry-run",), ("--yes",)])
def test_malformed_workflow_pattern_is_sanitized_and_never_writes(
    tmp_path: Path,
    arguments: tuple[str, ...],
    pattern: str,
) -> None:
    harness = GenesisHarness(tmp_path)
    malformed = WORKFLOW_CONFIG.replace(
        b"handle_pattern: '^proj/'",
        f"handle_pattern: {json.dumps(pattern)}".encode(),
    )
    harness.s3.remote[("output-bucket", ".quilt/workflows/config.yml", None)] = Stored(
        malformed,
        "workflow-config-malformed",
    )

    code = harness.invoke(*arguments)

    assert code == EXIT_REFUSED
    assert "WorkflowContractError" in harness.streams.stderr
    assert "invalid handle_pattern" in harness.streams.stderr
    assert pattern not in harness.streams.stderr
    assert "operator" not in harness.streams.stderr
    assert harness.s3.put_requests == []


def test_blank_message_is_refused_before_creating_an_operator_session(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)

    code = main(
        [
            "genesis",
            "author",
            str(tmp_path),
            "--destination",
            AUTHOR_DESTINATION,
            "--metadata",
            "{}",
            "--message",
            "   ",
            "--dry-run",
        ],
        runtime=make_runtime(streams=harness.streams, session=harness.session),
    )

    assert code == EXIT_REFUSED
    assert "--message must not be blank" in harness.streams.stderr
    assert harness.session.requested == []


def test_author_parser_requires_directory_destination_metadata_and_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = FakeSession({"s3": AuthoringS3()})
    streams = Streams()

    code = main(
        ["genesis", "author"],
        runtime=make_runtime(streams=streams, session=session),
    )

    assert code == EXIT_USAGE
    usage = capsys.readouterr().err
    assert "DIRECTORY" in usage
    assert "--destination" in usage
    assert "--metadata" in usage
    assert "--message" in usage


def test_authoring_has_no_no_workflow_escape(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)

    code = harness.invoke("--no-workflow", "--dry-run")

    assert code == EXIT_USAGE
    assert harness.session.requested == []
    assert harness.s3.put_requests == []


def test_blank_workflow_is_refused_before_creating_an_operator_session(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)

    code = harness.invoke("--workflow", "", "--dry-run")

    assert code == EXIT_REFUSED
    assert "--workflow must not be blank" in harness.streams.stderr
    assert harness.session.requested == []


def test_workflow_refusal_never_prints_rejected_metadata_values(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)
    sentinel = "unique-secret-metadata-value"

    code = main(
        [
            "genesis",
            "author",
            str(tmp_path),
            "--destination",
            AUTHOR_DESTINATION,
            "--metadata",
            json.dumps({"owner": [sentinel]}),
            "--message",
            AUTHOR_MESSAGE,
            "--dry-run",
        ],
        runtime=make_runtime(streams=harness.streams, session=harness.session),
    )

    assert code == EXIT_REFUSED
    assert "WorkflowContractError" in harness.streams.stderr
    assert "failed validation" in harness.streams.stderr
    assert sentinel not in harness.streams.stderr
    assert harness.s3.put_requests == []


def test_malformed_foreign_manifest_errors_never_echo_entry_metadata(tmp_path: Path) -> None:
    harness = GenesisHarness(tmp_path)
    seal_uri = harness.s3.add_source()
    sentinel = "SECRET-MANIFEST-METADATA"
    manifest_key = next(
        key
        for key in harness.s3.remote
        if key[0] == "foreign-registry" and key[1].startswith(".quilt/packages/")
    )
    stored = harness.s3.remote[manifest_key]
    records = [json.loads(line) for line in stored.body.decode().splitlines()]
    entry = dict(records[1])
    entry["meta"] = {"note": sentinel}
    malformed = (
        "\n".join(json.dumps(record) for record in (records[0], entry, entry)) + "\n"
    ).encode()
    harness.s3.remote[manifest_key] = Stored(malformed, stored.version_id)

    code = harness.invoke("--seal-input", seal_uri, "--dry-run")

    assert code == EXIT_REFUSED
    assert "ManifestLoadError" in harness.streams.stderr
    assert "Unable to load pinned Quilt manifest" in harness.streams.stderr
    assert sentinel not in harness.streams.stderr
    assert harness.s3.put_requests == []
