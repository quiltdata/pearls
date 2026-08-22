from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cli_support import (
    BUCKET_PATTERN,
    MODEL_ARN,
    ORCHESTRATOR_ARN,
    PROFILE_ARN,
    PROFILE_MODEL_EAST_ARN,
    PROFILE_MODEL_WEST_ARN,
    SECOND_MODEL_ARN,
    FakeSession,
    Streams,
    make_runtime,
)

from pearls.cli import main
from pearls.cli._common import CliRefusal
from pearls.cli._template import load_role_template

ROLE_NAME = "scene-execution-haiku"
STACK_ARN = "arn:aws:iam::123456789012:role/scene-execution-haiku"
SECOND_ROLE_ARN = "arn:aws:iam::123456789012:role/scene-execution-nova"

PARAMETERS = [
    "--orchestrator-principal-arn",
    ORCHESTRATOR_ARN,
    "--project-bucket-arn-pattern",
    BUCKET_PATTERN,
    "--bedrock-model-arn",
    MODEL_ARN,
    "--role-name",
    ROLE_NAME,
]


class StackAbsent(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"Stack with id {name} does not exist")
        self.response = {"Error": {"Code": "ValidationError"}}


class FakeCloudFormation:
    """CloudFormation fake that records every mutating call it is asked to make."""

    def __init__(
        self,
        *,
        stacks: dict[str, dict[str, Any]] | None = None,
        change_set_status: str = "CREATE_COMPLETE",
        change_set_reason: str | None = None,
        statuses: list[str] | None = None,
        create_error: Exception | None = None,
        execute_error: Exception | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.stacks = stacks or {}
        self.change_set_status = change_set_status
        self.change_set_reason = change_set_reason
        self.statuses = statuses or []
        self.create_error = create_error
        self.execute_error = execute_error
        self.stack_events = events or []
        self.calls: list[str] = []
        self.created: list[dict[str, Any]] = []
        self.executed: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []

    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_stacks")
        name = str(kwargs["StackName"])
        if name not in self.stacks:
            raise StackAbsent(name)
        stack = dict(self.stacks[name])
        # Statuses advance only after the change set is executed, so the plan
        # phase cannot consume the sequence meant for the wait loop.
        if self.statuses and "execute_change_set" in self.calls:
            stack["StackStatus"] = self.statuses.pop(0)
            stack["LastUpdatedTime"] = f"after-execute-{len(self.executed)}"
        return {"Stacks": [stack]}

    def create_change_set(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append("create_change_set")
        if self.create_error is not None:
            raise self.create_error
        self.created.append(kwargs)
        return {"Id": "change-set-id"}

    def describe_change_set(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_change_set")
        described: dict[str, Any] = {
            "Status": self.change_set_status,
            "Changes": [
                {
                    "ResourceChange": {
                        "Action": "Add",
                        "LogicalResourceId": "SceneExecutionRole",
                        "ResourceType": "AWS::IAM::Role",
                        "Replacement": None,
                    }
                }
            ],
        }
        if self.change_set_reason is not None:
            described["StatusReason"] = self.change_set_reason
        return described

    def execute_change_set(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append("execute_change_set")
        if self.execute_error is not None:
            raise self.execute_error
        self.executed.append(kwargs)
        return {}

    def delete_change_set(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append("delete_change_set")
        self.deleted.append(kwargs)
        return {}

    def describe_stack_events(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_stack_events")
        return {"StackEvents": self.stack_events}


class FakeIAM:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.user_policies: list[dict[str, Any]] = []
        self.role_policies: list[dict[str, Any]] = []

    def put_user_policy(self, **kwargs: Any) -> dict[str, str]:
        if self.error is not None:
            raise self.error
        self.user_policies.append(kwargs)
        return {}

    def put_role_policy(self, **kwargs: Any) -> dict[str, str]:
        if self.error is not None:
            raise self.error
        self.role_policies.append(kwargs)
        return {}


def deployed_stack(
    *,
    name: str = ROLE_NAME,
    status: str = "CREATE_COMPLETE",
    role_arn: str | None = STACK_ARN,
    model_arns: tuple[str, ...] = (MODEL_ARN,),
) -> dict[str, dict[str, Any]]:
    outputs = (
        [{"OutputKey": "SceneExecutionRoleArn", "OutputValue": role_arn}]
        if role_arn is not None
        else []
    )
    return {
        name: {
            "StackName": name,
            "StackStatus": status,
            "Parameters": [
                {"ParameterKey": "OrchestratorPrincipalArn", "ParameterValue": ORCHESTRATOR_ARN},
                {"ParameterKey": "ProjectBucketArnPattern", "ParameterValue": BUCKET_PATTERN},
                {
                    "ParameterKey": "BedrockInvokeModelArns",
                    "ParameterValue": ",".join(model_arns),
                },
                {"ParameterKey": "RoleName", "ParameterValue": name},
                {"ParameterKey": "MaxSessionDuration", "ParameterValue": "3600"},
            ],
            "Outputs": outputs,
        }
    }


class InfraHarness:
    """One fully offline `pearls infra` invocation."""

    def __init__(
        self,
        *,
        cloudformation: FakeCloudFormation | None = None,
        iam: FakeIAM | None = None,
        region: str | None = "us-east-1",
    ) -> None:
        self.cloudformation = cloudformation or FakeCloudFormation()
        self.iam = iam or FakeIAM()
        self.session = FakeSession(
            {"cloudformation": self.cloudformation, "iam": self.iam}, region_name=region
        )
        self.streams = Streams()

    def invoke(self, *arguments: str, answers: tuple[str, ...] = ()) -> int:
        runtime = make_runtime(streams=self.streams, session=self.session, answers=answers)
        return main(list(arguments), runtime=runtime)


def test_validate_checks_the_packaged_template_without_calling_aws() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "validate", *PARAMETERS, "--json")

    assert code == 0
    payload = harness.streams.json()
    assert payload["$schema"] == "https://quiltdata.com/schemas/pearls/cli-infra-validate/v2"
    assert payload["status"] == "ok"
    assert payload["template"].endswith("scene-execution-role.yaml")
    assert payload["capabilities"] == ["CAPABILITY_NAMED_IAM"]
    assert payload["stack_name"] == ROLE_NAME
    assert payload["mutations_performed"] == []
    assert harness.session.requested == []
    assert harness.cloudformation.calls == []


def test_validate_reports_the_administrator_prerequisites_it_cannot_provision() -> None:
    harness = InfraHarness()

    assert harness.invoke("infra", "validate", *PARAMETERS) == 0

    stdout = harness.streams.stdout
    assert "Bedrock account-level access" in stdout
    assert "sts:ExternalId" in stdout
    assert "commercial 'aws' partition" in stdout
    assert "intersection" in stdout
    assert "No AWS API was called." in stdout


def test_validate_applies_template_defaults_when_a_parameter_is_omitted() -> None:
    harness = InfraHarness()

    code = harness.invoke(
        "infra",
        "validate",
        "--orchestrator-principal-arn",
        ORCHESTRATOR_ARN,
        "--project-bucket-arn-pattern",
        BUCKET_PATTERN,
        "--bedrock-model-arn",
        MODEL_ARN,
        "--json",
    )

    assert code == 0
    payload = harness.streams.json()
    assert "RoleName" not in payload["parameters"]
    assert payload["parameters_effective"]["RoleName"] == "scene-execution"
    assert payload["parameters_effective"]["MaxSessionDuration"] == "3600"
    assert payload["stack_name"] == "scene-execution"


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--orchestrator-principal-arn", "arn:aws:iam::123:role/short-account"),
        ("--project-bucket-arn-pattern", "arn:aws:s3:::Uppercase-Bucket"),
        ("--role-name", "has spaces"),
    ],
    ids=["principal", "bucket-pattern", "role-name"],
)
def test_validate_rejects_values_the_template_would_reject(flag: str, value: str) -> None:
    harness = InfraHarness()
    supplied = list(PARAMETERS)
    supplied[supplied.index(flag) + 1] = value

    code = harness.invoke("infra", "validate", *supplied)

    assert code == 1
    assert "AllowedPattern" in harness.streams.stderr
    assert harness.streams.stdout == ""


def test_validate_rejects_a_max_session_duration_below_the_template_minimum() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "validate", *PARAMETERS, "--max-session-duration", "900")

    assert code == 1
    assert "must be at least 3600" in harness.streams.stderr


def test_validate_rejects_a_max_session_duration_above_the_template_maximum() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "validate", *PARAMETERS, "--max-session-duration", "86400")

    assert code == 1
    assert "must be at most 43200" in harness.streams.stderr


def test_validate_rejects_a_model_in_a_different_region_than_the_deployment() -> None:
    harness = InfraHarness(region="us-west-2")

    code = harness.invoke("infra", "validate", *PARAMETERS)

    assert code == 1
    stderr = harness.streams.stderr
    assert "us-east-1" in stderr and "us-west-2" in stderr
    assert "fail closed" in stderr


def test_explicit_region_argument_wins_over_the_profile_region() -> None:
    harness = InfraHarness(region="us-west-2")

    code = harness.invoke("infra", "validate", *PARAMETERS, "--region", "us-east-1", "--json")

    assert code == 0
    assert harness.streams.json()["region"] == "us-east-1"


def test_validate_says_so_when_no_region_is_available_to_compare() -> None:
    harness = InfraHarness(region=None)

    code = harness.invoke("infra", "validate", *PARAMETERS, "--json")

    assert code == 0
    checks = {entry["name"]: entry["detail"] for entry in harness.streams.json()["checks"]}
    assert "could not be compared" in checks["model-region-consistency"]


def test_validate_without_a_model_arn_makes_no_region_comparison() -> None:
    harness = InfraHarness()

    code = harness.invoke(
        "infra",
        "validate",
        "--orchestrator-principal-arn",
        ORCHESTRATOR_ARN,
        "--project-bucket-arn-pattern",
        BUCKET_PATTERN,
        "--json",
    )

    assert code == 0
    checks = {entry["name"]: entry["detail"] for entry in harness.streams.json()["checks"]}
    assert "was not supplied" in checks["model-region-consistency"]


def test_validate_runs_the_pinned_cfn_lint_when_asked() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "validate", *PARAMETERS, "--lint", "--json")

    assert code == 0
    checks = {entry["name"]: entry["detail"] for entry in harness.streams.json()["checks"]}
    assert checks["cfn-lint"] == "clean"


def test_validate_surfaces_cfn_lint_findings_as_a_refusal(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "Parameters:\n"
        "  RoleName:\n"
        "    Type: String\n"
        "    Default: scene-execution\n"
        "Resources:\n"
        "  SceneExecutionRole:\n"
        "    Type: AWS::IAM::Role\n"
        "    Properties:\n"
        "      NotAValidProperty: true\n"
        "Outputs:\n"
        "  SceneExecutionRoleArn:\n"
        "    Value: !GetAtt SceneExecutionRole.Arn\n",
        encoding="utf-8",
    )
    harness = InfraHarness()

    code = harness.invoke("infra", "validate", "--template-file", str(broken), "--lint")

    assert code == 1
    assert "cfn-lint reported findings" in harness.streams.stderr


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (None, "does not exist"),
        ("just a string", "must contain a CloudFormation template mapping"),
        (
            "Parameters: {}\nOutputs:\n  SceneExecutionRoleArn:\n    Value: x\n",
            "declares no Parameters section",
        ),
        ("Parameters:\n  A: {Type: String}\n", "does not export the SceneExecutionRoleArn"),
        ("{oops: [\n", "not parseable YAML"),
    ],
    ids=["absent", "not-a-mapping", "no-parameters", "no-output", "bad-yaml"],
)
def test_template_override_is_validated(tmp_path: Path, body: str | None, expected: str) -> None:
    path = tmp_path / "template.yaml"
    if body is not None:
        path.write_text(body, encoding="utf-8")
    harness = InfraHarness()

    code = harness.invoke("infra", "validate", "--template-file", str(path))

    assert code == 1
    assert expected in harness.streams.stderr


def test_template_rejects_parameter_names_it_does_not_declare() -> None:
    template = load_role_template()

    with pytest.raises(CliRefusal, match="declares no such parameter"):
        template.validate_parameters({"NotAParameter": "x"})


def test_plan_renders_a_change_set_and_never_executes_it() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "plan", *PARAMETERS, "--json")

    assert code == 0
    assert harness.cloudformation.executed == []
    assert harness.cloudformation.created[0]["ChangeSetType"] == "CREATE"
    assert harness.cloudformation.created[0]["Capabilities"] == ["CAPABILITY_NAMED_IAM"]
    assert harness.cloudformation.deleted[0]["StackName"] == ROLE_NAME
    payload = harness.streams.json()
    assert payload["status"] == "planned"
    assert payload["operation"] == "CREATE"
    assert payload["change_set_retained"] is False
    assert payload["mutations_performed"] == []


def test_plan_renders_the_trust_policy_and_inline_role_policy_for_review() -> None:
    harness = InfraHarness()

    assert harness.invoke("infra", "plan", *PARAMETERS) == 0

    stdout = harness.streams.stdout
    assert "Rendered trust policy:" in stdout
    assert ORCHESTRATOR_ARN in stdout
    assert "Rendered inline role policy:" in stdout
    assert f"{BUCKET_PATTERN}/*" in stdout
    assert MODEL_ARN in stdout
    assert "No stack resource was created, updated, or deleted." in stdout


def test_plan_of_an_existing_stack_is_an_update_with_a_parameter_diff() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(model_arns=("arn:aws:bedrock:us-east-1::foundation-model/old",))
        )
    )

    assert harness.invoke("infra", "plan", *PARAMETERS, "--json") == 0

    payload = harness.streams.json()
    assert payload["operation"] == "UPDATE"
    changed = {entry["parameter"] for entry in payload["parameter_diff"] if entry["changed"]}
    assert changed == {"BedrockInvokeModelArns"}


def test_a_stack_awaiting_review_still_needs_a_create_change_set() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(status="REVIEW_IN_PROGRESS", role_arn=None)
        )
    )

    assert harness.invoke("infra", "plan", *PARAMETERS, "--json") == 0

    assert harness.cloudformation.created[0]["ChangeSetType"] == "CREATE"
    assert harness.streams.json()["operation"] == "CREATE"


def test_plan_can_keep_the_change_set_for_out_of_band_review() -> None:
    harness = InfraHarness()

    assert harness.invoke("infra", "plan", *PARAMETERS, "--keep-change-set", "--json") == 0

    assert harness.cloudformation.deleted == []
    assert harness.cloudformation.executed == []
    assert harness.streams.json()["change_set_retained"] is True
    assert "was not executed" in harness.streams.stderr


def test_plan_reports_an_unchanged_stack_as_a_no_op() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(),
            change_set_status="FAILED",
            change_set_reason="The submitted information didn't contain changes.",
        )
    )

    code = harness.invoke("infra", "plan", *PARAMETERS)

    assert code == 0
    assert "Resource changes: none" in harness.streams.stdout
    assert harness.cloudformation.executed == []


def test_plan_surfaces_a_failed_change_set_and_cleans_it_up() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            change_set_status="FAILED",
            change_set_reason="Parameter validation failed for BedrockInvokeModelArns",
        )
    )

    code = harness.invoke("infra", "plan", *PARAMETERS)

    assert code == 1
    assert "Parameter validation failed" in harness.streams.stderr
    assert harness.cloudformation.deleted != []
    assert harness.cloudformation.executed == []


def test_plan_surfaces_a_change_set_that_cannot_be_created() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(create_error=RuntimeError("AccessDenied"))
    )

    code = harness.invoke("infra", "plan", *PARAMETERS)

    assert code == 1
    assert "unable to create a change set" in harness.streams.stderr


def test_plan_refuses_a_change_set_that_never_settles() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(change_set_status="CREATE_IN_PROGRESS")
    )

    code = harness.invoke("infra", "plan", *PARAMETERS)

    assert code == 1
    assert "did not reach a terminal status" in harness.streams.stderr


def test_plan_warns_but_continues_when_the_change_set_cannot_be_deleted() -> None:
    cloudformation = FakeCloudFormation()

    def failing_delete(**kwargs: Any) -> dict[str, str]:
        raise RuntimeError("AccessDenied on DeleteChangeSet")

    cloudformation.delete_change_set = failing_delete  # type: ignore[method-assign]
    harness = InfraHarness(cloudformation=cloudformation)

    assert harness.invoke("infra", "plan", *PARAMETERS) == 0

    assert "could not delete change set" in harness.streams.stderr


def test_deploy_requires_confirmation_before_touching_named_iam() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "deploy", *PARAMETERS, answers=("no",))

    assert code == 1
    assert "confirmation declined" in harness.streams.stderr
    assert harness.cloudformation.executed == []


def test_deploy_fails_closed_when_stdin_cannot_answer() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "deploy", *PARAMETERS)

    assert code == 1
    assert "re-run with --yes" in harness.streams.stderr
    assert harness.cloudformation.executed == []


@pytest.mark.parametrize(
    ("answers", "expected"),
    [(("no",), "confirmation declined"), ((), "re-run with --yes")],
    ids=["declined", "no-stdin"],
)
def test_an_unapproved_change_set_does_not_outlive_the_command(
    answers: tuple[str, ...], expected: str
) -> None:
    """A change set the operator never approved must be cleaned up."""
    harness = InfraHarness()

    code = harness.invoke("infra", "deploy", *PARAMETERS, answers=answers)

    assert code == 1
    assert expected in harness.streams.stderr
    assert harness.cloudformation.executed == []
    assert harness.cloudformation.deleted[0]["StackName"] == ROLE_NAME
    assert harness.cloudformation.deleted[0]["ChangeSetName"].startswith("pearls-deploy-")


def test_abandoning_a_new_stack_reports_the_review_shell_it_leaves_behind() -> None:
    harness = InfraHarness()

    assert harness.invoke("infra", "deploy", *PARAMETERS, answers=("no",)) == 1

    # The change set is gone, but the empty stack shell needs an administrator.
    assert "REVIEW_IN_PROGRESS" in harness.streams.stderr


def test_abandoning_an_update_does_not_mention_a_review_shell() -> None:
    harness = InfraHarness(cloudformation=FakeCloudFormation(stacks=deployed_stack()))

    assert harness.invoke("infra", "deploy", *PARAMETERS, answers=("no",)) == 1

    assert harness.cloudformation.deleted != []
    assert "REVIEW_IN_PROGRESS" not in harness.streams.stderr


def test_an_executed_change_set_is_left_in_the_stack_history() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(stacks=deployed_stack(), statuses=["CREATE_COMPLETE"])
    )

    assert harness.invoke("infra", "deploy", *PARAMETERS, "--yes") == 0

    assert harness.cloudformation.executed != []
    assert harness.cloudformation.deleted == []


def test_deploy_executes_the_change_set_and_reports_the_role_arn() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(status="CREATE_COMPLETE"),
            statuses=["CREATE_IN_PROGRESS", "CREATE_COMPLETE"],
        )
    )

    code = harness.invoke("infra", "deploy", *PARAMETERS, "--yes", "--json")

    assert code == 0
    assert harness.cloudformation.executed[0]["StackName"] == ROLE_NAME
    payload = harness.streams.json()
    assert payload["status"] == "deployed"
    assert payload["role_arn"] == STACK_ARN
    assert payload["capabilities"] == ["CAPABILITY_NAMED_IAM"]
    assert payload["mutations_performed"] == ["execute_change_set"]


def test_deploy_prints_the_role_arn_in_a_form_usable_by_scene_run() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(stacks=deployed_stack(), statuses=["CREATE_COMPLETE"])
    )

    assert harness.invoke("infra", "deploy", *PARAMETERS, "--yes") == 0

    stdout = harness.streams.stdout
    assert f"pearls scene run --role-arn {STACK_ARN}" in stdout
    assert "Bedrock account-level access" in stdout


def test_deploy_defaults_the_stack_name_from_the_role_name() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(stacks=deployed_stack(), statuses=["CREATE_COMPLETE"])
    )

    assert harness.invoke("infra", "deploy", *PARAMETERS, "--yes", "--json") == 0

    payload = harness.streams.json()
    assert payload["stack_name"] == ROLE_NAME
    assert payload["parameters"]["RoleName"] == ROLE_NAME


def test_explicit_stack_name_overrides_the_role_name_default() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(name="pearls-worker"), statuses=["CREATE_COMPLETE"]
        )
    )

    code = harness.invoke(
        "infra", "deploy", *PARAMETERS, "--stack-name", "pearls-worker", "--yes", "--json"
    )

    assert code == 0
    assert harness.streams.json()["stack_name"] == "pearls-worker"


@pytest.mark.parametrize("verb", ["plan", "deploy"], ids=["plan", "plan-only"])
def test_a_role_name_that_is_not_a_valid_stack_name_is_refused_locally(verb: str) -> None:
    """The template admits role names CloudFormation rejects as stack names."""
    harness = InfraHarness()
    supplied = list(PARAMETERS)
    supplied[supplied.index("--role-name") + 1] = "scene_execution.haiku"
    extra = ["--plan-only"] if verb == "deploy" else []

    code = harness.invoke("infra", verb, *supplied, *extra)

    assert code == 1
    stderr = harness.streams.stderr
    assert "defaulted from --role-name" in stderr
    assert "Pass an explicit --stack-name" in stderr
    # Refused before any AWS call, not at the change set.
    assert harness.cloudformation.calls == []


def test_an_invalid_explicit_stack_name_is_refused_without_blaming_the_role_name() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "plan", *PARAMETERS, "--stack-name", "9-starts-with-digit")

    assert code == 1
    assert "is not a valid CloudFormation stack name" in harness.streams.stderr
    assert "defaulted from" not in harness.streams.stderr
    assert harness.cloudformation.calls == []


def test_a_role_name_needing_a_separate_stack_name_works_when_given_one() -> None:
    stacks = deployed_stack(name="pearls-trial001-haiku")
    harness = InfraHarness(cloudformation=FakeCloudFormation(stacks=stacks))
    supplied = list(PARAMETERS)
    supplied[supplied.index("--role-name") + 1] = "scene_execution.haiku"

    code = harness.invoke(
        "infra", "plan", *supplied, "--stack-name", "pearls-trial001-haiku", "--json"
    )

    assert code == 0
    payload = harness.streams.json()
    assert payload["stack_name"] == "pearls-trial001-haiku"
    assert payload["parameters"]["RoleName"] == "scene_execution.haiku"


def test_json_deploy_shows_the_review_material_on_stderr_before_confirming() -> None:
    """An operator must never be asked to approve IAM changes they cannot see."""
    harness = InfraHarness()

    code = harness.invoke("infra", "deploy", *PARAMETERS, "--json", answers=("no",))

    assert code == 1
    stderr = harness.streams.stderr
    assert "Rendered trust policy:" in stderr
    assert ORCHESTRATOR_ARN in stderr
    assert "Rendered inline role policy:" in stderr
    assert "type 'yes' to continue" in stderr
    # The prompt came after the review material, not before it.
    assert stderr.index("Rendered trust policy:") < stderr.index("type 'yes' to continue")
    assert harness.cloudformation.executed == []


def test_json_stdout_stays_a_single_parseable_document_when_review_goes_to_stderr() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(stacks=deployed_stack(), statuses=["CREATE_COMPLETE"])
    )

    assert harness.invoke("infra", "deploy", *PARAMETERS, "--json", "--yes") == 0

    assert "Rendered trust policy:" in harness.streams.stderr
    assert harness.streams.json()["status"] == "deployed"


def test_plan_only_reports_a_no_op_the_same_way_plan_does() -> None:
    def harness_for(verb: str, *extra: str) -> InfraHarness:
        harness = InfraHarness(
            cloudformation=FakeCloudFormation(
                stacks=deployed_stack(),
                change_set_status="FAILED",
                change_set_reason="The submitted information didn't contain changes.",
            )
        )
        assert harness.invoke("infra", verb, *PARAMETERS, *extra, "--json") == 0
        return harness

    plan = harness_for("plan")
    alias = harness_for("deploy", "--plan-only")

    assert plan.streams.json()["status"] == "no-change"
    assert alias.streams.json()["status"] == plan.streams.json()["status"]
    assert alias.cloudformation.executed == []


def test_plan_only_reports_the_review_shell_the_same_way_plan_does() -> None:
    plan = InfraHarness()
    alias = InfraHarness()

    assert plan.invoke("infra", "plan", *PARAMETERS, "--json") == 0
    assert alias.invoke("infra", "deploy", *PARAMETERS, "--plan-only", "--json") == 0

    assert "REVIEW_IN_PROGRESS" in alias.streams.stderr
    assert [note for note in alias.streams.json()["notes"] if "REVIEW_IN_PROGRESS" in note] == [
        note for note in plan.streams.json()["notes"] if "REVIEW_IN_PROGRESS" in note
    ]


def test_deploy_plan_only_is_an_alias_for_plan() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "deploy", *PARAMETERS, "--plan-only", "--json")

    assert code == 0
    assert harness.cloudformation.executed == []
    assert harness.cloudformation.deleted != []
    payload = harness.streams.json()
    assert payload["status"] == "planned"
    assert payload["mutations_performed"] == []

    human = InfraHarness()
    assert human.invoke("infra", "deploy", *PARAMETERS, "--plan-only") == 0
    assert "--plan-only: nothing was executed." in human.streams.stdout
    assert human.cloudformation.executed == []


def test_rerunning_deploy_with_unchanged_parameters_is_a_reported_no_op() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(),
            change_set_status="FAILED",
            change_set_reason="No updates are to be performed.",
        )
    )

    code = harness.invoke("infra", "deploy", *PARAMETERS, "--yes", "--json")

    assert code == 0
    assert harness.cloudformation.executed == []
    payload = harness.streams.json()
    assert payload["status"] == "no-change"
    assert payload["role_arn"] == STACK_ARN
    assert payload["mutations_performed"] == []

    human = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(),
            change_set_status="FAILED",
            change_set_reason="No updates are to be performed.",
        )
    )
    assert human.invoke("infra", "deploy", *PARAMETERS, "--yes") == 0
    assert "already matches these parameters" in human.streams.stdout
    assert STACK_ARN in human.streams.stdout


def test_a_rolled_back_deployment_exits_nonzero_with_the_cloudformation_reason() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(status="ROLLBACK_COMPLETE", role_arn=None),
            statuses=["CREATE_IN_PROGRESS", "ROLLBACK_COMPLETE"],
            events=[
                {
                    "LogicalResourceId": "SceneExecutionRole",
                    "ResourceStatus": "CREATE_FAILED",
                    "ResourceStatusReason": "already exists in stack other-stack",
                },
                {
                    "LogicalResourceId": "SceneExecutionRole",
                    "ResourceStatus": "CREATE_FAILED",
                    "ResourceStatusReason": "already exists in stack other-stack",
                },
            ],
        )
    )

    code = harness.invoke("infra", "deploy", *PARAMETERS, "--yes", "--json")

    assert code == 1
    stderr = harness.streams.stderr
    assert "ROLLBACK_COMPLETE" in stderr
    assert "already exists in stack other-stack" in stderr
    payload = harness.streams.json()
    assert payload["status"] == "failed"
    # The reason is de-duplicated rather than repeated per event.
    assert len(payload["failure_reasons"]) == 1


def test_deploy_reports_a_failure_even_when_stack_events_are_unavailable() -> None:
    cloudformation = FakeCloudFormation(
        stacks=deployed_stack(status="UPDATE_ROLLBACK_COMPLETE"),
        statuses=["UPDATE_ROLLBACK_COMPLETE"],
    )

    def failing_events(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("AccessDenied on DescribeStackEvents")

    cloudformation.describe_stack_events = failing_events  # type: ignore[method-assign]
    harness = InfraHarness(cloudformation=cloudformation)

    code = harness.invoke("infra", "deploy", *PARAMETERS, "--yes")

    assert code == 1
    assert "stack events unavailable" in harness.streams.stderr


def test_deploy_surfaces_a_change_set_that_cannot_be_executed_and_cleans_it_up() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(execute_error=RuntimeError("InsufficientCapabilities"))
    )

    code = harness.invoke("infra", "deploy", *PARAMETERS, "--yes")

    assert code == 1
    assert "unable to execute the change set" in harness.streams.stderr
    # The change set was never accepted for execution, so it must not survive.
    assert harness.cloudformation.deleted != []


def test_deploy_stops_waiting_and_says_the_stack_is_still_in_progress() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(),
            statuses=["CREATE_IN_PROGRESS", "CREATE_IN_PROGRESS", "CREATE_IN_PROGRESS"],
        )
    )

    code = harness.invoke("infra", "deploy", *PARAMETERS, "--yes", "--wait-seconds", "1")

    assert code == 1
    assert "still in progress in CloudFormation" in harness.streams.stderr


def test_deploy_will_not_report_success_from_a_stale_pre_execution_status() -> None:
    """An update must not read back the stack's previous terminal status.

    Without this, updating an already-CREATE_COMPLETE stack reports `deployed`
    with the outputs from before the change was executed.
    """
    cloudformation = FakeCloudFormation(stacks=deployed_stack())
    # Never advance past the pre-execution status, and never bump LastUpdatedTime.
    cloudformation.statuses = []
    harness = InfraHarness(cloudformation=cloudformation)

    code = harness.invoke("infra", "deploy", *PARAMETERS, "--yes", "--wait-seconds", "1")

    assert code == 1
    stderr = harness.streams.stderr
    assert "pre-execution status" in stderr
    assert "could not be confirmed" in stderr


def test_a_change_set_that_never_settles_is_still_cleaned_up() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(change_set_status="CREATE_IN_PROGRESS")
    )

    code = harness.invoke("infra", "plan", *PARAMETERS, "--wait-seconds", "1")

    assert code == 1
    assert "did not reach a terminal status within 1s" in harness.streams.stderr
    assert harness.cloudformation.deleted != []
    assert harness.cloudformation.executed == []


def test_an_undescribable_change_set_is_a_refusal_and_is_cleaned_up() -> None:
    cloudformation = FakeCloudFormation()

    def refusing(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("AccessDenied on DescribeChangeSet")

    cloudformation.describe_change_set = refusing  # type: ignore[method-assign]
    harness = InfraHarness(cloudformation=cloudformation)

    code = harness.invoke("infra", "plan", *PARAMETERS)

    assert code == 1
    assert "unable to describe change set" in harness.streams.stderr
    assert harness.cloudformation.deleted != []


def test_planning_a_new_stack_reports_the_review_shell_it_leaves_behind() -> None:
    harness = InfraHarness()

    assert harness.invoke("infra", "plan", *PARAMETERS, "--json") == 0

    assert "REVIEW_IN_PROGRESS" in harness.streams.stderr
    assert any("REVIEW_IN_PROGRESS" in note for note in harness.streams.json()["notes"])


def test_planning_an_existing_stack_does_not_warn_about_a_review_shell() -> None:
    harness = InfraHarness(cloudformation=FakeCloudFormation(stacks=deployed_stack()))

    assert harness.invoke("infra", "plan", *PARAMETERS) == 0

    assert "REVIEW_IN_PROGRESS" not in harness.streams.stderr


def test_deploy_warns_when_the_stack_exports_no_role_arn() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(role_arn=None), statuses=["CREATE_COMPLETE"]
        )
    )

    assert harness.invoke("infra", "deploy", *PARAMETERS, "--yes") == 0

    assert "exports no SceneExecutionRoleArn output" in harness.streams.stderr


def test_deploy_refuses_when_a_stack_vanishes_while_waiting() -> None:
    cloudformation = FakeCloudFormation(stacks=deployed_stack())
    calls: list[int] = []

    original = cloudformation.describe_stacks

    def vanishing(**kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        if len(calls) > 2:
            raise StackAbsent(str(kwargs["StackName"]))
        return original(**kwargs)

    cloudformation.describe_stacks = vanishing  # type: ignore[method-assign]
    # One in-progress poll after execution, then the stack is gone.
    cloudformation.statuses = ["UPDATE_IN_PROGRESS"]
    harness = InfraHarness(cloudformation=cloudformation)

    code = harness.invoke("infra", "deploy", *PARAMETERS, "--yes")

    assert code == 1
    assert "disappeared while waiting" in harness.streams.stderr


def test_status_reports_the_identity_a_deployed_role_grants() -> None:
    harness = InfraHarness(cloudformation=FakeCloudFormation(stacks=deployed_stack()))

    code = harness.invoke("infra", "status", "--stack-name", ROLE_NAME, "--json")

    assert code == 0
    payload = harness.streams.json()
    assert payload["$schema"] == "https://quiltdata.com/schemas/pearls/cli-infra-status/v2"
    assert payload["stack_status"] == "CREATE_COMPLETE"
    assert payload["identity"] == {
        "role_arn": STACK_ARN,
        "role_name": ROLE_NAME,
        "admitted_bedrock_resources": [MODEL_ARN],
        "project_bucket_arn_pattern": BUCKET_PATTERN,
        "orchestrator_principal_arn": ORCHESTRATOR_ARN,
        "max_session_duration": "3600",
    }
    assert payload["mutations_performed"] == []


def test_status_accepts_a_role_name_and_prints_a_readable_report() -> None:
    harness = InfraHarness(cloudformation=FakeCloudFormation(stacks=deployed_stack()))

    assert harness.invoke("infra", "status", "--role-name", ROLE_NAME) == 0

    stdout = harness.streams.stdout
    assert "admitted bedrock resources" in stdout
    assert MODEL_ARN in stdout
    assert "intersection" in stdout


def test_status_needs_a_stack_or_role_name() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "status")

    assert code == 1
    assert "supply --stack-name or --role-name" in harness.streams.stderr


def test_status_of_an_undeployed_stack_exits_nonzero() -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "status", "--stack-name", "not-deployed")

    assert code == 1
    assert "does not exist" in harness.streams.stderr


def test_status_surfaces_an_unexpected_describe_failure() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(),
    )

    def refusing(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("AccessDenied on DescribeStacks")

    harness.cloudformation.describe_stacks = refusing  # type: ignore[method-assign]

    code = harness.invoke("infra", "status", "--stack-name", ROLE_NAME)

    assert code == 1
    assert "unable to describe stack" in harness.streams.stderr


def test_assume_policy_prints_by_default_and_mutates_nothing() -> None:
    harness = InfraHarness()

    code = harness.invoke(
        "infra", "assume-policy", "--role-arn", STACK_ARN, "--role-arn", SECOND_ROLE_ARN, "--json"
    )

    assert code == 0
    payload = harness.streams.json()
    assert payload["status"] == "printed"
    assert payload["applied"] is False
    assert payload["mutations_performed"] == []
    assert payload["policy_document"]["Statement"][0]["Action"] == "sts:AssumeRole"
    assert payload["policy_document"]["Statement"][0]["Resource"] == [
        STACK_ARN,
        SECOND_ROLE_ARN,
    ]
    assert harness.iam.user_policies == []
    assert harness.iam.role_policies == []


def test_assume_policy_prints_the_document_for_direct_review() -> None:
    harness = InfraHarness()

    assert harness.invoke("infra", "assume-policy", "--role-arn", STACK_ARN) == 0

    stdout = harness.streams.stdout
    assert json.loads(stdout.split("Printed only")[0])["Version"] == "2012-10-17"
    assert "--apply-to-user/--apply-to-role" in stdout


def test_assume_policy_names_every_role_of_a_multi_model_project_from_stacks() -> None:
    stacks = deployed_stack()
    stacks.update(deployed_stack(name="scene-execution-nova", role_arn=SECOND_ROLE_ARN))
    harness = InfraHarness(cloudformation=FakeCloudFormation(stacks=stacks))

    code = harness.invoke(
        "infra",
        "assume-policy",
        "--from-stack",
        ROLE_NAME,
        "--from-stack",
        "scene-execution-nova",
        "--json",
    )

    assert code == 0
    payload = harness.streams.json()
    assert payload["role_arns"] == [STACK_ARN, SECOND_ROLE_ARN]
    assert payload["resolved_from_stacks"] == {
        ROLE_NAME: STACK_ARN,
        "scene-execution-nova": SECOND_ROLE_ARN,
    }


def test_assume_policy_deduplicates_repeated_role_arns() -> None:
    harness = InfraHarness()

    code = harness.invoke(
        "infra", "assume-policy", "--role-arn", STACK_ARN, "--role-arn", STACK_ARN, "--json"
    )

    assert code == 0
    assert harness.streams.json()["role_arns"] == [STACK_ARN]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], "supply at least one --role-arn or --from-stack"),
        (["--role-arn", "scene-execution"], "not a complete IAM role ARN"),
        (["--from-stack", "absent"], "does not exist"),
    ],
    ids=["nothing-named", "not-an-arn", "absent-stack"],
)
def test_assume_policy_refuses_unusable_targets(arguments: list[str], expected: str) -> None:
    harness = InfraHarness()

    code = harness.invoke("infra", "assume-policy", *arguments)

    assert code == 1
    assert expected in harness.streams.stderr


def test_assume_policy_refuses_a_stack_without_a_role_output() -> None:
    harness = InfraHarness(cloudformation=FakeCloudFormation(stacks=deployed_stack(role_arn=None)))

    code = harness.invoke("infra", "assume-policy", "--from-stack", ROLE_NAME)

    assert code == 1
    assert "exports no SceneExecutionRoleArn output" in harness.streams.stderr


def test_assume_policy_validates_arns_it_read_from_a_stack_output() -> None:
    """A stack output becomes a granted Resource, so it gets the same check."""
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(stacks=deployed_stack(role_arn="arn:aws:iam::*:role/*"))
    )

    code = harness.invoke("infra", "assume-policy", "--from-stack", ROLE_NAME)

    assert code == 1
    assert "not a complete IAM role ARN" in harness.streams.stderr
    assert harness.iam.user_policies == []


def test_applying_the_assume_policy_warns_that_it_replaces_the_inline_policy() -> None:
    harness = InfraHarness()

    code = harness.invoke(
        "infra",
        "assume-policy",
        "--role-arn",
        STACK_ARN,
        "--apply-to-user",
        "orchestrator",
        "--yes",
        "--json",
    )

    assert code == 0
    assert "replaces any existing inline policy of that exact name" in harness.streams.stderr
    applied = harness.iam.user_policies[0]
    assert applied["UserName"] == "orchestrator"
    assert applied["PolicyName"] == "PearlsSceneAssumeRole"
    assert json.loads(applied["PolicyDocument"])["Statement"][0]["Resource"] == [STACK_ARN]
    payload = harness.streams.json()
    assert payload["status"] == "applied"
    assert payload["applied"] is True
    assert payload["target"] == {"type": "user", "name": "orchestrator"}
    assert payload["mutations_performed"] == ["put_user_policy"]


def test_applying_the_assume_policy_to_a_role_uses_a_deterministic_policy_name() -> None:
    harness = InfraHarness()

    code = harness.invoke(
        "infra",
        "assume-policy",
        "--role-arn",
        STACK_ARN,
        "--apply-to-role",
        "orchestrator-role",
        "--policy-name",
        "TrialOneAssume",
        "--yes",
    )

    assert code == 0
    applied = harness.iam.role_policies[0]
    assert applied["RoleName"] == "orchestrator-role"
    assert applied["PolicyName"] == "TrialOneAssume"


def test_applying_the_assume_policy_requires_confirmation() -> None:
    harness = InfraHarness()

    code = harness.invoke(
        "infra",
        "assume-policy",
        "--role-arn",
        STACK_ARN,
        "--apply-to-user",
        "orchestrator",
        answers=("no",),
    )

    assert code == 1
    assert "confirmation declined" in harness.streams.stderr
    assert harness.iam.user_policies == []


def test_assume_policy_surfaces_an_iam_refusal() -> None:
    harness = InfraHarness(iam=FakeIAM(error=RuntimeError("AccessDenied on PutUserPolicy")))

    code = harness.invoke(
        "infra",
        "assume-policy",
        "--role-arn",
        STACK_ARN,
        "--apply-to-user",
        "orchestrator",
        "--yes",
    )

    assert code == 1
    assert "unable to apply inline policy" in harness.streams.stderr


def test_confirmation_accepts_a_case_insensitive_yes() -> None:
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(stacks=deployed_stack(), statuses=["CREATE_COMPLETE"])
    )

    code = harness.invoke("infra", "deploy", *PARAMETERS, answers=(" YES ",))

    assert code == 0
    assert harness.cloudformation.executed != []


def parameters_with_models(*model_arns: str) -> list[str]:
    parameters = list(PARAMETERS)
    index = parameters.index("--bedrock-model-arn")
    del parameters[index : index + 2]
    for model_arn in model_arns:
        parameters.extend(["--bedrock-model-arn", model_arn])
    return parameters


def test_validate_exposes_a_canonical_nonempty_model_inventory() -> None:
    harness = InfraHarness()

    code = harness.invoke(
        "infra",
        "validate",
        *parameters_with_models(SECOND_MODEL_ARN, MODEL_ARN),
        "--json",
    )

    assert code == 0
    expected = sorted([MODEL_ARN, SECOND_MODEL_ARN])
    payload = harness.streams.json()
    assert payload["parameters"]["BedrockInvokeModelArns"] == expected
    assert payload["parameters_effective"]["BedrockInvokeModelArns"] == expected
    assert harness.session.requested == []


def test_plan_serializes_and_renders_the_exact_inventory_as_a_list() -> None:
    harness = InfraHarness()

    code = harness.invoke(
        "infra",
        "plan",
        *parameters_with_models(SECOND_MODEL_ARN, MODEL_ARN),
        "--json",
    )

    assert code == 0
    expected = sorted([MODEL_ARN, SECOND_MODEL_ARN])
    sent = {
        item["ParameterKey"]: item["ParameterValue"]
        for item in harness.cloudformation.created[0]["Parameters"]
    }
    assert sent["BedrockInvokeModelArns"] == ",".join(expected)
    payload = harness.streams.json()
    assert payload["parameters"]["BedrockInvokeModelArns"] == expected
    statements = payload["rendered"]["inline_policies"][0]["PolicyDocument"]["Statement"]
    bedrock = next(item for item in statements if item["Action"] == "bedrock:InvokeModel")
    assert bedrock["Resource"] == expected


def test_validate_accepts_exact_cross_region_resources_only_for_a_profile() -> None:
    harness = InfraHarness(region="us-east-1")

    code = harness.invoke(
        "infra",
        "validate",
        *parameters_with_models(
            PROFILE_MODEL_WEST_ARN,
            PROFILE_ARN,
            PROFILE_MODEL_EAST_ARN,
        ),
        "--json",
    )

    assert code == 0
    expected = sorted([PROFILE_ARN, PROFILE_MODEL_EAST_ARN, PROFILE_MODEL_WEST_ARN])
    payload = harness.streams.json()
    assert payload["parameters"]["BedrockInvokeModelArns"] == expected
    checks = {entry["name"]: entry["detail"] for entry in payload["checks"]}
    assert "cross-region profile support" in checks["model-region-consistency"]


@pytest.mark.parametrize(
    "models",
    [
        (MODEL_ARN, MODEL_ARN),
        ("arn:aws:bedrock:us-east-1::foundation-model/*",),
        ("arn:aws:bedrock:us-east-1:123456789012:custom-model/example",),
        (PROFILE_ARN,),
        (PROFILE_ARN, MODEL_ARN),
        ("arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-micro-v1:0",),
    ],
    ids=[
        "duplicate",
        "wildcard",
        "custom-model",
        "profile-without-support",
        "profile-with-unrelated-support",
        "foreign-direct-model",
    ],
)
def test_validate_rejects_invalid_or_unusable_inventory_before_aws(
    models: tuple[str, ...],
) -> None:
    harness = InfraHarness(region="us-east-1")

    code = harness.invoke("infra", "validate", *parameters_with_models(*models))

    assert code == 1
    assert harness.session.requested == []
    assert harness.cloudformation.calls == []


def test_plan_diff_and_no_op_compare_logical_inventory_lists() -> None:
    expected = tuple(sorted([MODEL_ARN, SECOND_MODEL_ARN]))
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(
            stacks=deployed_stack(model_arns=expected),
            change_set_status="FAILED",
            change_set_reason="No updates are to be performed.",
        )
    )

    code = harness.invoke(
        "infra",
        "plan",
        *parameters_with_models(SECOND_MODEL_ARN, MODEL_ARN),
        "--json",
    )

    assert code == 0
    payload = harness.streams.json()
    assert payload["status"] == "no-change"
    inventory_diff = next(
        item for item in payload["parameter_diff"] if item["parameter"] == "BedrockInvokeModelArns"
    )
    assert inventory_diff == {
        "parameter": "BedrockInvokeModelArns",
        "current": list(expected),
        "requested": list(expected),
        "changed": False,
    }


def test_status_reports_the_complete_model_inventory() -> None:
    inventory = tuple(sorted([MODEL_ARN, SECOND_MODEL_ARN]))
    harness = InfraHarness(
        cloudformation=FakeCloudFormation(stacks=deployed_stack(model_arns=inventory))
    )

    assert harness.invoke("infra", "status", "--stack-name", ROLE_NAME, "--json") == 0

    payload = harness.streams.json()
    assert payload["parameters"]["BedrockInvokeModelArns"] == list(inventory)
    assert payload["identity"]["admitted_bedrock_resources"] == list(inventory)


def test_template_validates_each_comma_delimited_inventory_element() -> None:
    template = load_role_template()

    with pytest.raises(CliRefusal, match="AllowedPattern"):
        template.validate_parameters(
            {"BedrockInvokeModelArns": f"{MODEL_ARN},arn:aws:bedrock:us-east-1::custom-model/x"}
        )
