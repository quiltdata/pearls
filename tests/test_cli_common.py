from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    NoRegionError,
    ProfileNotFound,
)
from cli_support import Streams, make_runtime

from raja_sts import __version__
from raja_sts.cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, CliRefusal, Runtime, main, run
from raja_sts.cli._common import (
    Emitter,
    confirm,
    envelope,
    iso8601,
    positive_int,
    read_json_argument,
    render_table,
)
from raja_sts.cli._template import (
    ROLE_ARN_OUTPUT,
    load_role_template,
    render_intrinsics,
)


def _runtime(streams: Streams, answers: tuple[str, ...] = ()) -> Runtime:
    return make_runtime(streams=streams, session=object(), answers=answers)


def test_root_help_documents_both_groups_and_the_shared_conventions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    streams = Streams()

    code = main(["--help"], runtime=_runtime(streams))

    assert code == EXIT_OK
    help_text = capsys.readouterr().out
    assert "scene   check | run" in help_text
    assert "infra   validate | plan | deploy | status | assume-policy" in help_text
    assert "--json emits a stable, versioned document" in help_text
    assert "never prompt" in help_text


def test_version_reports_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    streams = Streams()

    code = main(["--version"], runtime=_runtime(streams))

    assert code == EXIT_OK
    assert capsys.readouterr().out.strip() == f"pearls {__version__}"


def test_a_missing_group_is_a_usage_error() -> None:
    streams = Streams()

    code = main([], runtime=_runtime(streams))

    assert code == EXIT_USAGE


def test_an_unknown_group_is_a_usage_error() -> None:
    streams = Streams()

    code = main(["nonsense"], runtime=_runtime(streams))

    assert code == EXIT_USAGE


def test_main_builds_a_default_runtime_when_none_is_supplied() -> None:
    assert main(["--help"]) in {EXIT_OK, EXIT_USAGE}


def test_console_entry_point_exits_with_the_command_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("raja_sts.cli.main", lambda: EXIT_REFUSED)

    with pytest.raises(SystemExit) as exit_info:
        run()

    assert exit_info.value.code == EXIT_REFUSED


@pytest.mark.parametrize(
    "error",
    [
        OSError("the local disk went away"),
        ValueError("something unmodelled"),
        ProfileNotFound(profile="missing-profile"),
        NoRegionError(),
        NoCredentialsError(),
        ClientError({"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, "Operation"),
    ],
    ids=[
        "os-error",
        "value-error",
        "profile-not-found",
        "no-region",
        "no-credentials",
        "client-error",
    ],
)
def test_expected_operator_errors_are_reported_as_refusals_not_tracebacks(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Credential, region, and AWS refusals are operator conditions, not defects."""
    streams = Streams()

    def failing(*_: Any, **__: Any) -> int:
        raise error

    monkeypatch.setattr("raja_sts.cli.infra.status_command", failing)

    code = main(["infra", "status", "--stack-name", "x"], runtime=_runtime(streams))

    assert code == EXIT_REFUSED
    assert f"{type(error).__name__}: {error}" in streams.stderr
    assert streams.stdout == ""


def test_json_output_stays_on_stdout_while_diagnostics_go_to_stderr() -> None:
    streams = Streams()
    emitter = Emitter(_runtime(streams), json_output=True)

    emitter.line("suppressed under --json")
    emitter.note("a diagnostic")
    emitter.document({"ok": True})

    assert streams.stdout == '{\n  "ok": true\n}\n'
    assert streams.stderr == "a diagnostic\n"


def test_human_output_suppresses_the_json_document() -> None:
    streams = Streams()
    emitter = Emitter(_runtime(streams), json_output=False)

    emitter.line("a result")
    emitter.document({"ok": True})

    assert streams.stdout == "a result\n"


def test_render_table_aligns_labels_and_tolerates_no_rows() -> None:
    streams = Streams()
    emitter = Emitter(_runtime(streams), json_output=False)

    render_table(emitter, [])
    render_table(emitter, [("short", "a"), ("much longer label", "b")])

    assert streams.stdout == "short              a\nmuch longer label  b\n"


def test_envelope_carries_the_schema_status_and_version() -> None:
    assert envelope("https://example.test/v1", "ok") == {
        "$schema": "https://example.test/v1",
        "status": "ok",
        "pearls_version": __version__,
    }


def test_confirmation_is_skipped_only_by_an_explicit_yes_flag() -> None:
    streams = Streams()

    confirm(_runtime(streams), question="Proceed?", assume_yes=True)

    assert streams.stderr == ""


@pytest.mark.parametrize("answer", ["", "y", "no", "YES please"], ids=["blank", "y", "no", "extra"])
def test_only_the_exact_word_yes_confirms(answer: str) -> None:
    streams = Streams()

    with pytest.raises(CliRefusal, match="confirmation declined"):
        confirm(_runtime(streams, (answer,)), question="Proceed?", assume_yes=False)

    assert "type 'yes' to continue" in streams.stderr


def test_an_interrupted_prompt_fails_closed() -> None:
    streams = Streams()
    runtime = _runtime(streams)

    def interrupted(_: str) -> str:
        raise KeyboardInterrupt

    runtime.prompt = interrupted

    with pytest.raises(CliRefusal, match="re-run with --yes"):
        confirm(runtime, question="Proceed?", assume_yes=False)


def test_json_arguments_accept_inline_objects_and_files(tmp_path: Path) -> None:
    path = tmp_path / "meta.json"
    path.write_text('{"from": "file"}', encoding="utf-8")

    assert read_json_argument('{"inline": 1}', label="--metadata") == {"inline": 1}
    assert read_json_argument(f"@{path}", label="--metadata") == {"from": "file"}


def test_positive_int_rejects_non_numbers_and_non_positive_values() -> None:
    import argparse

    assert positive_int("3600") == 3600
    for value in ("abc", "0", "-1"):
        with pytest.raises(argparse.ArgumentTypeError):
            positive_int(value)


def test_iso8601_refuses_a_naive_timestamp() -> None:
    assert iso8601(datetime(2026, 8, 20, 12, tzinfo=UTC)) == "2026-08-20T12:00:00Z"

    with pytest.raises(CliRefusal, match="timezone-aware"):
        iso8601(datetime(2026, 8, 20, 12))


def test_packaged_template_declares_the_role_arn_output() -> None:
    template = load_role_template()

    assert ROLE_ARN_OUTPUT in template.document["Outputs"]
    assert set(template.parameters) == {
        "OrchestratorPrincipalArn",
        "ProjectBucketArnPattern",
        "BedrockInvokeModelArns",
        "RoleName",
        "MaxSessionDuration",
    }
    assert template.parameters["MaxSessionDuration"].min_value == 3600
    assert template.parameters["MaxSessionDuration"].max_value == 43200


def test_template_parameter_values_are_checked_as_numbers_when_declared_so() -> None:
    spec = load_role_template().parameters["MaxSessionDuration"]

    with pytest.raises(CliRefusal, match="must be a number"):
        spec.validate("not-a-number")


def test_template_role_properties_are_reachable_for_rendering() -> None:
    properties = load_role_template().role_policies

    assert "AssumeRolePolicyDocument" in properties
    assert "Policies" in properties


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "Parameters:\n  A:\n    Type: String\nOutputs:\n  SceneExecutionRoleArn: {}\n",
            "declares no Resources section",
        ),
        (
            "Parameters:\n  A:\n    Type: String\nResources:\n  Other: {}\n"
            "Outputs:\n  SceneExecutionRoleArn: {}\n",
            "declares no SceneExecutionRole resource",
        ),
        (
            "Parameters:\n  A:\n    Type: String\nResources:\n  SceneExecutionRole: {}\n"
            "Outputs:\n  SceneExecutionRoleArn: {}\n",
            "has no Properties",
        ),
        (
            "Parameters:\n  A: not-a-mapping\nOutputs:\n  SceneExecutionRoleArn: {}\n",
            "is not a mapping",
        ),
    ],
    ids=["no-resources", "no-role", "no-properties", "bad-parameter"],
)
def test_malformed_templates_are_refused_with_a_specific_reason(
    tmp_path: Path, body: str, expected: str
) -> None:
    path = tmp_path / "template.yaml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(CliRefusal, match=expected):
        assert load_role_template(str(path)).role_policies


def test_unreadable_template_files_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "template.yaml"
    path.write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(CliRefusal, match="not readable UTF-8"):
        load_role_template(str(path))


def test_short_form_intrinsics_survive_parsing_in_every_node_shape(tmp_path: Path) -> None:
    path = tmp_path / "template.yaml"
    path.write_text(
        "Parameters:\n"
        "  Name:\n"
        "    Type: String\n"
        "Resources:\n"
        "  SceneExecutionRole:\n"
        "    Type: AWS::IAM::Role\n"
        "    Properties:\n"
        "      Scalar: !Ref Name\n"
        "      Sequence: !GetAtt [SceneExecutionRole, Arn]\n"
        "      Mapping: !Select {a: 1}\n"
        "Outputs:\n"
        "  SceneExecutionRoleArn:\n"
        "    Value: !GetAtt SceneExecutionRole.Arn\n",
        encoding="utf-8",
    )

    properties = load_role_template(str(path)).role_policies

    assert properties["Scalar"] == {"Ref": "Name"}
    assert properties["Sequence"] == {"Fn::GetAtt": ["SceneExecutionRole", "Arn"]}
    assert properties["Mapping"] == {"Fn::Select": {"a": 1}}


def test_rendering_substitutes_known_parameters_and_marks_unknown_ones() -> None:
    parameters = {"RoleName": "scene-execution-haiku"}

    assert render_intrinsics({"Ref": "RoleName"}, parameters) == "scene-execution-haiku"
    assert render_intrinsics({"Ref": "Other"}, parameters) == "<unresolved Ref Other>"
    assert render_intrinsics({"Fn::Sub": "${RoleName}/*"}, parameters) == "scene-execution-haiku/*"
    assert render_intrinsics({"Fn::Sub": "${Nope}/*"}, parameters) == "<unresolved Sub Nope>/*"
    assert render_intrinsics({"Fn::GetAtt": ["Role", "Arn"]}, parameters) == (
        "<resolved at deploy time: Role.Arn>"
    )
    assert render_intrinsics({"Fn::GetAtt": "Role.Arn"}, parameters) == (
        "<resolved at deploy time: Role.Arn>"
    )
    assert render_intrinsics({"Fn::ImportValue": "x"}, parameters) == {"Fn::ImportValue": "x"}
    assert render_intrinsics([{"Ref": "RoleName"}, 3], parameters) == ["scene-execution-haiku", 3]
