from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cli_support import (
    GENESIS_URI,
    MODEL_ARN,
    ROLE_ARN,
    TOP_HASH,
    FakeGenesisS3,
    FakeSession,
    FakeSTS,
    FakeWorkerS3,
    FakeWorkerSession,
    GenesisFixture,
    Recorder,
    Streams,
    make_runtime,
)

from raja_sts import run_scene_from_genesis
from raja_sts.cli import main
from raja_sts.cli.scene import UNPROVEN_CONDITIONS, generate_scene_id

BASE_ARGUMENTS = [
    "--genesis",
    GENESIS_URI,
    "--role-arn",
    ROLE_ARN,
    "--model-arn",
    MODEL_ARN,
]


class SceneHarness:
    """One fully offline `pearls scene` invocation."""

    def __init__(self, fixture: GenesisFixture | None = None) -> None:
        self.fixture = fixture or GenesisFixture()
        self.events: list[str] = []
        self.genesis_s3 = FakeGenesisS3(self.fixture)
        self.sts = FakeSTS(self.events)
        self.worker_s3 = FakeWorkerS3(self.events)
        self.worker_session = FakeWorkerSession(self.worker_s3, self.events)
        self.session = FakeSession(
            {"s3": self.genesis_s3, "sts": self.sts}, region_name="us-east-1"
        )
        self.streams = Streams()
        self.runner = Recorder(run_scene_from_genesis)

    def invoke(self, *arguments: str, answers: tuple[str, ...] = ()) -> int:
        runtime = make_runtime(
            streams=self.streams,
            session=self.session,
            manifest_loader=self.fixture.loader(),
            worker_session=self.worker_session,
            answers=answers,
            run_scene_from_genesis=self.runner,
        )
        return main(list(arguments), runtime=runtime)

    @property
    def mutating_calls(self) -> list[str]:
        """Return every call that could change AWS or the local machine."""
        return [
            *(f"sts:{len(self.sts.requests)}" for _ in self.sts.requests[:1]),
            *(f"s3:put:{request['Key']}" for request in self.worker_s3.put_requests),
            *self.genesis_s3.other_calls,
        ]

    def check(self, *extra: str) -> int:
        return self.invoke("scene", "check", *BASE_ARGUMENTS, *extra)

    def run(self, *extra: str) -> int:
        return self.invoke("scene", "run", *BASE_ARGUMENTS, *extra)


def test_check_passes_and_reports_non_secret_authority() -> None:
    harness = SceneHarness()

    code = harness.check("--json")

    assert code == 0
    payload = harness.streams.json()
    assert payload["$schema"] == "https://quiltdata.com/schemas/raja-sts/cli-scene-check/v1"
    assert payload["status"] == "ok"
    assert payload["genesis"]["top_hash"] == TOP_HASH
    assert payload["authority"]["mode"] == "per-object-version"
    assert payload["authority"]["bedrock_model_arn"] == MODEL_ARN
    assert payload["handler"]["logical_key"] == "protocol/handler.py"
    assert payload["output"]["package_name"] == "proj/trial001-worker-output"
    assert payload["mutations_performed"] == []
    assert payload["unproven"] == list(UNPROVEN_CONDITIONS)
    assert {entry["status"] for entry in payload["checks"]} == {"pass"}


def test_check_performs_no_assume_role_no_invocation_and_no_write() -> None:
    harness = SceneHarness()

    assert harness.check() == 0

    # Only an S3 reader was ever requested: no STS and no Bedrock client exists.
    assert harness.session.requested == ["s3"]
    assert harness.sts.requests == []
    assert harness.events == []
    assert harness.mutating_calls == []


def test_check_does_not_execute_the_sealed_handler(tmp_path: Path) -> None:
    """A preflight must not run genesis code, which would carry this identity.

    The module body here would write a file. If `check` imported the sealed
    handler, that file would exist and the command's own non-mutation claim
    would be false.
    """
    sentinel = tmp_path / "executed-by-check"
    source = f"open({str(sentinel)!r}, 'w').close()\n\n\ndef handler(*_):\n    pass\n".encode()
    harness = SceneHarness(GenesisFixture(handler_source=source))

    assert harness.check() == 0

    assert not sentinel.exists()
    assert "the bytes were not executed" in harness.streams.stdout


def test_check_reports_that_it_cannot_prove_the_handler_is_callable() -> None:
    harness = SceneHarness()

    assert harness.check("--json") == 0

    unproven = " ".join(harness.streams.json()["unproven"])
    assert "does not execute them" in unproven
    assert "callable" in unproven


def test_check_refuses_bytes_that_bind_no_handler_without_executing_them(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "executed-by-check"
    source = f"open({str(sentinel)!r}, 'w').close()\n".encode()
    harness = SceneHarness(GenesisFixture(handler_source=source))

    code = harness.check()

    assert code == 1
    assert "does not bind a module-level" in harness.streams.stderr
    assert not sentinel.exists()


def test_check_reads_only_the_sealed_control_objects_by_exact_version() -> None:
    harness = SceneHarness()

    assert harness.check() == 0

    assert harness.genesis_s3.read_keys == [
        "genesis/README.md",
        "genesis/outputs.json",
        "genesis/protocol/handler.py",
    ]
    assert all("VersionId" in request for request in harness.genesis_s3.get_requests)


def test_check_resolves_the_pinned_revision_once_for_preflight_and_handler() -> None:
    harness = SceneHarness()

    assert harness.check() == 0

    assert harness.fixture.loads == 1


def test_check_labels_which_checks_needed_the_trusted_bootstrap_identity() -> None:
    harness = SceneHarness()

    assert harness.check("--json") == 0

    by_name = {entry["name"]: entry["identity"] for entry in harness.streams.json()["checks"]}
    assert by_name["genesis-controls-verified"] == "trusted-bootstrap"
    assert by_name["sealed-handler-verified"] == "trusted-bootstrap"
    assert by_name["genesis-manifest-resolved"] == "trusted-bootstrap"
    assert by_name["argument-shapes"] == "local"
    assert by_name["model-arn-exact"] == "local"
    assert by_name["session-policy-compiled"] == "local"


def test_check_states_what_it_cannot_prove_including_bedrock_model_access() -> None:
    harness = SceneHarness()

    assert harness.check() == 0

    stdout = harness.streams.stdout
    assert "Bedrock account-level access" in stdout
    assert "quiltdata/quilt#5181" in stdout
    assert "code identity, not code safety" in stdout
    assert "No AssumeRole, model invocation, or write was performed" in stdout
    assert "no byte of the sealed handler was executed" in stdout


def test_check_human_output_summarizes_authority() -> None:
    harness = SceneHarness()

    assert harness.check() == 0

    stdout = harness.streams.stdout
    assert "pass  sealed-handler-verified (trusted-bootstrap)" in stdout
    assert "authority mode" in stdout
    assert "per-object-version" in stdout
    assert "proj/trial001-worker-output@output-bucket" in stdout


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            ["--genesis", f"quilt+s3://genesis-bucket#package=proj%2Fx&top_hash={'b' * 8}"],
            "InvalidManifestURI",
        ),
        (["--role-arn", "scene-execution"], "InvalidAssumeRoleRequest"),
        (["--model-arn", "arn:aws:bedrock:us-east-1::inference-profile/x"], "InvalidScopeError"),
        (["--duration-seconds", "60"], "InvalidAssumeRoleRequest"),
        (["--scene-id", "a"], "InvalidAssumeRoleRequest"),
        (["--external-id", "x"], "InvalidAssumeRoleRequest"),
        (["--message", "   "], "CliRefusal"),
        (["--handler-key", ""], "CliRefusal"),
    ],
    ids=[
        "unpinned-genesis",
        "role-arn",
        "model-arn",
        "duration",
        "scene-id",
        "external-id",
        "blank-message",
        "empty-handler-key",
    ],
)
def test_check_refuses_bad_argument_shapes_before_any_aws_call(
    arguments: list[str], expected: str
) -> None:
    harness = SceneHarness()
    overrides = dict(zip(arguments[::2], arguments[1::2], strict=True))
    supplied = list(BASE_ARGUMENTS)
    for flag, value in overrides.items():
        if flag in supplied:
            supplied[supplied.index(flag) + 1] = value
        else:
            supplied += [flag, value]

    code = harness.invoke("scene", "check", *supplied)

    assert code == 1
    assert expected in harness.streams.stderr
    assert harness.streams.stdout == ""
    assert harness.session.requested == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("@missing.json", "does not exist"),
        ("[1, 2]", "must be a JSON object, not list"),
        ("{oops}", "must be a JSON object"),
    ],
    ids=["absent-file", "not-an-object", "not-json"],
)
def test_check_refuses_malformed_metadata(value: str, expected: str) -> None:
    harness = SceneHarness()

    code = harness.check("--metadata", value)

    assert code == 1
    assert expected in harness.streams.stderr


def test_metadata_is_accepted_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "output-meta.json"
    path.write_text(json.dumps({"rubric_version": "v2"}), encoding="utf-8")
    harness = SceneHarness()

    assert harness.check("--metadata", f"@{path}", "--json") == 0

    assert harness.streams.json()["output"]["metadata_keys"] == ["rubric_version"]


def test_check_refuses_an_unreadable_metadata_file(tmp_path: Path) -> None:
    path = tmp_path / "output-meta.json"
    path.write_bytes(b"\xff\xfe not utf-8")
    harness = SceneHarness()

    code = harness.check("--metadata", f"@{path}")

    assert code == 1
    assert "not readable UTF-8" in harness.streams.stderr


def test_check_refuses_a_genesis_package_missing_its_controls() -> None:
    fixture = GenesisFixture()
    fixture.entries = [entry for entry in fixture.entries if entry.logical_key != "outputs.json"]
    harness = SceneHarness(fixture)

    code = harness.check()

    assert code == 1
    assert "ManifestLoadError" in harness.streams.stderr
    assert "outputs.json" in harness.streams.stderr


def test_check_refuses_a_corrupted_sealed_handler_and_writes_nothing() -> None:
    harness = SceneHarness(GenesisFixture(corrupt_handler=True))

    code = harness.check()

    assert code == 1
    assert "InputIntegrityError" in harness.streams.stderr
    assert harness.sts.requests == []
    assert harness.worker_s3.put_requests == []


def test_run_executes_one_scene_and_prints_the_immutable_package_uri() -> None:
    harness = SceneHarness()

    code = harness.run()

    assert code == 0
    assert len(harness.runner.calls) == 1
    stdout = harness.streams.stdout
    assert stdout.startswith(
        "quilt+s3://output-bucket#package=proj/trial001-worker-output&top_hash="
    )
    assert "assumed role" in stdout


def test_run_receipt_has_a_stable_versioned_shape_without_secrets() -> None:
    harness = SceneHarness()

    metadata = '{"pass_count": 3, "reviewer_note": "unique-metadata-value"}'

    assert harness.run("--json", "--metadata", metadata, "--message", "Trial 001") == 0

    payload = harness.streams.json()
    assert payload["$schema"] == "https://quiltdata.com/schemas/raja-sts/cli-scene-run/v1"
    assert payload["status"] == "published"
    assert payload["scene_id"].startswith("scene-2026")
    assert payload["genesis"]["uri"] == GENESIS_URI
    assert payload["handler"]["source_sha256"]
    assert payload["output"]["package_uri"].startswith("quilt+s3://output-bucket#package=")
    assert payload["output"]["metadata_keys"] == ["pass_count", "reviewer_note"]
    assert payload["output"]["message_provided"] is True
    # The first clock tick names the Scene; the run brackets itself with the next two.
    assert payload["timestamps"] == {
        "started_at": "2026-08-20T12:01:00Z",
        "completed_at": "2026-08-20T12:02:00Z",
    }
    serialized = json.dumps(payload)
    for secret in ("temporary-access", "temporary-secret", "temporary-token"):
        assert secret not in serialized
    # Metadata values are declarations, not results: only their keys are echoed.
    assert "reviewer_note" in serialized
    assert "unique-metadata-value" not in serialized


def test_run_sends_metadata_message_and_workflow_through_to_publication() -> None:
    harness = SceneHarness()

    assert harness.run("--metadata", '{"rubric": "v2"}', "--message", "Trial 001 worker") == 0

    kwargs = harness.runner.kwargs
    assert kwargs["output_meta"] == {"rubric": "v2"}
    assert kwargs["output_message"] == "Trial 001 worker"
    assert kwargs["output_workflow"] is ...


@pytest.mark.parametrize(
    ("arguments", "expected", "display"),
    [
        ([], ..., "<registry default>"),
        (["--no-workflow"], None, None),
    ],
    ids=["registry-default", "opted-out"],
)
def test_workflow_selection_reaches_publication(
    arguments: list[str], expected: Any, display: str | None
) -> None:
    harness = SceneHarness()

    assert harness.run("--json", *arguments) == 0

    assert harness.runner.kwargs["output_workflow"] is expected
    assert harness.streams.json()["output"]["workflow"] == display


def test_naming_a_workflow_the_output_registry_does_not_define_fails_closed() -> None:
    harness = SceneHarness()

    code = harness.run("--workflow", "trial-verdict-v2")

    # Proves the selector reached the publication contract rather than being dropped.
    assert code == 1
    assert "WorkflowContractError" in harness.streams.stderr
    assert "trial-verdict-v2" in harness.streams.stderr
    assert harness.runner.kwargs["output_workflow"] == "trial-verdict-v2"


def test_run_omits_empty_metadata_rather_than_declaring_an_empty_mapping() -> None:
    harness = SceneHarness()

    assert harness.run() == 0

    assert harness.runner.kwargs["output_meta"] is None


def test_run_generates_a_valid_role_session_name_when_no_scene_id_is_given() -> None:
    harness = SceneHarness()

    assert harness.run("--json") == 0

    scene_id = harness.streams.json()["scene_id"]
    assert re.fullmatch(r"^[A-Za-z0-9_+=,.@-]{2,64}$", scene_id)
    assert harness.sts.requests[0]["RoleSessionName"] == scene_id


def test_generated_scene_ids_are_unique_for_the_same_instant() -> None:
    now = datetime(2026, 8, 20, 12, tzinfo=UTC)

    identifiers = {generate_scene_id(now) for _ in range(64)}

    assert len(identifiers) == 64
    assert all(value.startswith("scene-20260820T120000Z-") for value in identifiers)


def test_explicit_scene_id_is_used_verbatim() -> None:
    harness = SceneHarness()

    assert harness.run("--scene-id", "trial001-worker-attempt2", "--json") == 0

    assert harness.streams.json()["scene_id"] == "trial001-worker-attempt2"
    assert harness.sts.requests[0]["RoleSessionName"] == "trial001-worker-attempt2"


def test_run_reuses_the_genesis_runner_and_adds_no_authority_of_its_own() -> None:
    harness = SceneHarness()

    assert harness.run("--duration-seconds", "1800", "--external-id", "trial-001") == 0

    kwargs = harness.runner.kwargs
    assert harness.runner.calls[0][0] == (GENESIS_URI,)
    assert kwargs["role_arn"] == ROLE_ARN
    assert kwargs["bedrock_model"] == MODEL_ARN
    assert kwargs["duration_seconds"] == 1800
    assert kwargs["external_id"] == "trial-001"
    assert kwargs["genesis_s3_client"] is harness.genesis_s3
    assert kwargs["sts_client"] is harness.sts
    request = harness.sts.requests[0]
    assert request["DurationSeconds"] == 1800
    assert request["ExternalId"] == "trial-001"
    assert json.loads(request["Policy"])["Statement"]


def test_run_publishes_the_sealed_handler_output_and_the_run_records() -> None:
    harness = SceneHarness()

    assert harness.run() == 0

    written = [request["Key"] for request in harness.worker_s3.put_requests]
    assert "proj/trial001-worker-output/result/answer.txt" in written
    assert "proj/trial001-worker-output/run/scene.json" in written
    assert "proj/trial001-worker-output/run/invocation.json" in written
    assert written[-1].endswith("/latest")


def test_run_sends_diagnostics_to_stderr_so_json_stdout_stays_parseable() -> None:
    harness = SceneHarness()

    assert harness.run("--json") == 0

    assert "verified sealed protocol/handler.py" in harness.streams.stderr
    assert "executing protocol/handler.py" in harness.streams.stderr
    harness.streams.json()


def test_run_never_builds_a_scene_session_from_the_operator_profile() -> None:
    harness = SceneHarness()

    assert harness.run("--profile", "admin", "--region", "us-west-2") == 0

    # The operator's profile and region reach only the operator's own session.
    assert harness.session.factory_kwargs == {
        "profile_name": "admin",
        "region_name": "us-west-2",
    }
    assert harness.session.requested == ["s3", "sts"]
    # The Scene session is built from the minted credential and the model region.
    assert "profile_name" not in harness.worker_session.kwargs
    assert harness.worker_session.kwargs["region_name"] == "us-east-1"
    assert harness.worker_session.kwargs["aws_access_key_id"] == "temporary-access"


def test_run_reports_a_refusal_from_the_runner_as_a_nonzero_exit() -> None:
    harness = SceneHarness()
    harness.sts.error = RuntimeError("AccessDenied: not authorized to assume the role")

    code = harness.run()

    assert code == 1
    assert "MintError" in harness.streams.stderr
    assert harness.streams.stdout == ""


def test_run_validates_authority_and_mints_before_executing_sealed_bytes(
    tmp_path: Path,
) -> None:
    """Sealed code must not run until the runner has finished admitting it.

    The module body records when it executed. An eager load would place that
    marker before the AssumeRole call, making the layout, authority, and
    credential checks unreachable by anything the genesis package does.
    """
    marker = tmp_path / "executed"
    source = (
        f"open({str(marker)!r}, 'w').close()\n"
        "from raja_sts import SceneHandlerResult\n\n\n"
        "def handler(inputs, session, context):\n"
        '    return SceneHandlerResult({"result/answer.txt": "done"}, {})\n'
    ).encode()
    harness = SceneHarness(GenesisFixture(handler_source=source))

    assert harness.run() == 0

    assert marker.exists()
    assert harness.events.index("assume_role") < harness.events.index("worker-get")
    assert "executing protocol/handler.py" in harness.streams.stderr


def test_run_refuses_a_genesis_layout_error_before_executing_sealed_bytes(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "executed"
    source = (f"open({str(marker)!r}, 'w').close()\n\n\ndef handler(*_):\n    pass\n").encode()
    fixture = GenesisFixture(handler_source=source)
    # Remove the required control object the runner checks for.
    fixture.entries = [entry for entry in fixture.entries if entry.logical_key != "outputs.json"]
    harness = SceneHarness(fixture)

    code = harness.run()

    assert code == 1
    assert "ManifestLoadError" in harness.streams.stderr
    assert not marker.exists()
    assert harness.sts.requests == []


def test_run_refuses_a_revision_with_no_sealed_handler_before_minting() -> None:
    harness = SceneHarness(GenesisFixture(handler_source=None))

    code = harness.run()

    assert code == 1
    assert "declares no sealed handler" in harness.streams.stderr
    assert harness.sts.requests == []
    assert harness.runner.calls == []
