"""``pearls infra`` — reviewable CloudFormation deployment of Scene execution roles.

These verbs mutate persistent IAM with administrator authority, which is why they
are a separate group from ``pearls scene``. Nothing here hides the privileged
action: ``validate`` and ``plan`` never change AWS, ``deploy`` prints the change
it is about to execute and requires confirmation, and ``assume-policy`` prints by
default and applies only behind an explicit flag.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

from ..bedrock import (
    bedrock_inventory_region_mismatches,
    parse_bedrock_inventory,
    parse_bedrock_resource,
)
from ._common import (
    EXIT_OK,
    INFRA_ASSUME_POLICY_SCHEMA,
    INFRA_DEPLOY_SCHEMA,
    INFRA_PLAN_SCHEMA,
    INFRA_STATUS_SCHEMA,
    INFRA_VALIDATE_SCHEMA,
    CliRefusal,
    Emitter,
    Runtime,
    add_aws_arguments,
    add_confirmation_argument,
    build_session,
    confirm,
    envelope,
    positive_int,
    render_table,
)
from ._template import ROLE_ARN_OUTPUT, RoleTemplate, load_role_template, render_intrinsics

NAMED_IAM_CAPABILITY = "CAPABILITY_NAMED_IAM"
DEFAULT_ASSUME_POLICY_NAME = "PearlsSceneAssumeRole"
_POLL_INTERVAL = 5.0
# CloudFormation stack names are narrower than the template's RoleName pattern.
_STACK_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
_NO_CHANGE_FRAGMENTS = ("didn't contain changes", "no updates are to be performed")
_ABSENT_FRAGMENTS = ("does not exist", "not found")
_CHANGE_SET_DONE = frozenset({"CREATE_COMPLETE", "FAILED", "DELETE_COMPLETE", "DELETE_FAILED"})
_STACK_SUCCESS = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"})
_STACK_FAILED = frozenset(
    {
        "CREATE_FAILED",
        "DELETE_COMPLETE",
        "DELETE_FAILED",
        "IMPORT_ROLLBACK_COMPLETE",
        "IMPORT_ROLLBACK_FAILED",
        "ROLLBACK_COMPLETE",
        "ROLLBACK_FAILED",
        "UPDATE_FAILED",
        "UPDATE_ROLLBACK_COMPLETE",
        "UPDATE_ROLLBACK_FAILED",
    }
)
_PARAMETER_ARGUMENTS: tuple[tuple[str, str], ...] = (
    ("OrchestratorPrincipalArn", "orchestrator_principal_arn"),
    ("ProjectBucketArnPattern", "project_bucket_arn_pattern"),
    ("RoleName", "role_name"),
    ("MaxSessionDuration", "max_session_duration"),
)
_BEDROCK_INVENTORY_PARAMETER = "BedrockInvokeModelArns"

BEDROCK_ACCESS_NOTE = (
    "Bedrock account-level access to admitted models and profiles cannot be provisioned "
    "by this template; grant it in the Bedrock console. This role alone is not sufficient."
)
EXTERNAL_ID_NOTE = (
    "The trust policy admits the orchestrator principal with no sts:ExternalId "
    "condition, while mint(external_id=...) exists for separately managed roles."
)
PARTITION_NOTE = "The template targets the commercial 'aws' partition only."
REVIEW_SHELL_NOTE = (
    "Planning a stack that does not exist yet leaves it in REVIEW_IN_PROGRESS with "
    "no resources. A later deploy of the same stack name proceeds normally; these "
    "commands have no delete authority, so removing an abandoned shell is an "
    "explicit administrator action."
)
INTERSECTION_NOTE = (
    "The base role's Bedrock ARN list is an inventory, not per-Scene authority. "
    "Effective authority is its intersection with the sealed session policy, which "
    "selects exactly one model or profile and its exact supporting resources."
)
TEMPLATE_NOTES: tuple[str, ...] = (
    BEDROCK_ACCESS_NOTE,
    EXTERNAL_ID_NOTE,
    PARTITION_NOTE,
    INTERSECTION_NOTE,
)


def _error_code(error: Exception) -> str:
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        inner = response.get("Error")
        if isinstance(inner, Mapping):
            code = inner.get("Code")
            if isinstance(code, str):
                return code
    return ""


def _looks_absent(error: Exception) -> bool:
    text = str(error).lower()
    return _error_code(error) in {
        "ValidationError",
        "ResourceNotFoundException",
        "StackNotFoundException",
        "NoSuchEntity",
        "404",
    } and any(fragment in text for fragment in _ABSENT_FRAGMENTS)


def _add_parameter_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument(
        "--orchestrator-principal-arn",
        required=required,
        metavar="ARN",
        help="IAM principal allowed to assume the Scene execution role",
    )
    parser.add_argument(
        "--project-bucket-arn-pattern",
        required=required,
        metavar="ARN",
        help="project bucket ARN, optionally with a controlled wildcard",
    )
    parser.add_argument(
        "--bedrock-model-arn",
        action="append",
        required=required,
        metavar="ARN",
        help=(
            "exact Bedrock regional foundation-model, account-scoped inference-profile, "
            "or global-profile regionless support ARN; repeat to build the role "
            "inventory, including every profile support resource"
        ),
    )
    parser.add_argument(
        "--role-name",
        metavar="NAME",
        help="IAM role name; also the default stack name",
    )
    parser.add_argument(
        "--max-session-duration",
        type=positive_int,
        metavar="SECONDS",
        help="maximum role session duration, 3600-43200",
    )
    parser.add_argument(
        "--stack-name",
        metavar="NAME",
        help="CloudFormation stack name (default: the role name, so they cannot diverge)",
    )
    parser.add_argument(
        "--template-file",
        metavar="PATH",
        help="override the template packaged with Pearls",
    )


def add_parser(subparsers: Any) -> None:
    """Register the ``infra`` group and its verbs."""
    infra = subparsers.add_parser(
        "infra",
        help="deploy and inspect Scene execution roles",
        description=(
            "Administrator verbs that mutate persistent IAM. Kept separate from "
            "'pearls scene', which runs with bounded Scene authority."
        ),
    )
    verbs = infra.add_subparsers(dest="verb", metavar="VERB", required=True)

    validate = verbs.add_parser(
        "validate",
        help="offline template and parameter checks; contacts no AWS API",
        description=(
            "Parse the template, optionally run the pinned cfn-lint, and check any "
            "supplied parameter values against the template's own constraints plus "
            "cross-parameter consistency the template cannot express."
        ),
    )
    _add_parameter_arguments(validate, required=False)
    validate.add_argument(
        "--lint",
        action="store_true",
        help="also run the pinned cfn-lint over the template",
    )
    add_aws_arguments(validate)
    validate.set_defaults(run=validate_command)

    plan = verbs.add_parser(
        "plan",
        help="render a change set without executing it",
        description=(
            "Create and render a CloudFormation change set, print the parameter diff, "
            "resource changes, and the rendered trust and inline role policies, then "
            "delete the change set. Never executes it and never changes a stack."
        ),
    )
    _add_parameter_arguments(plan, required=True)
    plan.add_argument(
        "--keep-change-set",
        action="store_true",
        help="leave the change set in place for out-of-band review",
    )
    plan.add_argument(
        "--wait-seconds",
        type=positive_int,
        default=300,
        help="maximum time to wait for the change set to be created (default: 300)",
    )
    add_aws_arguments(plan)
    plan.set_defaults(run=plan_command)

    deploy = verbs.add_parser(
        "deploy",
        help="execute a reviewed change against one stack",
        description=(
            "Render the change set, require confirmation, execute it with "
            f"{NAMED_IAM_CAPABILITY}, wait for a terminal stack status, and print the "
            f"{ROLE_ARN_OUTPUT} for use as 'pearls scene run --role-arn'."
        ),
    )
    _add_parameter_arguments(deploy, required=True)
    deploy.add_argument(
        "--plan-only",
        action="store_true",
        help="alias for 'pearls infra plan'; renders the change set and stops",
    )
    deploy.add_argument(
        "--wait-seconds",
        type=positive_int,
        default=900,
        help="maximum time to wait for a terminal stack status (default: 900)",
    )
    add_confirmation_argument(deploy)
    add_aws_arguments(deploy)
    deploy.set_defaults(run=deploy_command)

    status = verbs.add_parser(
        "status",
        help="report a deployed stack and the identity it grants",
        description=(
            "Print the stack status, its parameters, and the drift-relevant identity "
            "an operator needs to confirm a role still matches the intended Scene."
        ),
    )
    status.add_argument("--stack-name", metavar="NAME", help="stack to describe")
    status.add_argument("--role-name", metavar="NAME", help="derive the stack name from this")
    add_aws_arguments(status)
    status.set_defaults(run=status_command)

    assume = verbs.add_parser(
        "assume-policy",
        help="print the orchestrator's sts:AssumeRole policy for deployed roles",
        description=(
            "Print an orchestrator policy naming one or more Scene execution roles. "
            "A role can hold a model inventory while each Scene session still selects "
            "exactly one model or profile; applying requires confirmation."
        ),
    )
    assume.add_argument(
        "--role-arn",
        action="append",
        default=[],
        metavar="ARN",
        help="Scene execution role ARN; repeat for each role",
    )
    assume.add_argument(
        "--from-stack",
        action="append",
        default=[],
        metavar="NAME",
        help=f"resolve a role ARN from this stack's {ROLE_ARN_OUTPUT} output; repeatable",
    )
    assume.add_argument(
        "--policy-name",
        default=DEFAULT_ASSUME_POLICY_NAME,
        metavar="NAME",
        help=f"inline policy name used when applying (default: {DEFAULT_ASSUME_POLICY_NAME})",
    )
    target = assume.add_mutually_exclusive_group()
    target.add_argument(
        "--apply-to-user", metavar="NAME", help="apply as an IAM user inline policy"
    )
    target.add_argument(
        "--apply-to-role", metavar="NAME", help="apply as an IAM role inline policy"
    )
    add_confirmation_argument(assume)
    add_aws_arguments(assume)
    assume.set_defaults(run=assume_policy_command)


def _supplied_parameters(args: argparse.Namespace) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, attribute in _PARAMETER_ARGUMENTS:
        value = getattr(args, attribute, None)
        if value is not None:
            values[name] = str(value)
    model_arns = getattr(args, "bedrock_model_arn", None)
    if model_arns is not None:
        inventory = parse_bedrock_inventory(tuple(str(value) for value in model_arns))
        values[_BEDROCK_INVENTORY_PARAMETER] = ",".join(inventory)
    return values


def _resolve_region(runtime: Runtime, args: argparse.Namespace) -> str | None:
    if args.region:
        return str(args.region)
    region = getattr(build_session(runtime, args), "region_name", None)
    return str(region) if region else None


def _check_model_region(parameters: Mapping[str, str], region: str | None) -> str:
    """Validate every inventory ARN against the deployment-region convention."""
    serialized = parameters.get(_BEDROCK_INVENTORY_PARAMETER)
    if serialized is None:
        return f"{_BEDROCK_INVENTORY_PARAMETER} was not supplied, so no region comparison was made"
    inventory = parse_bedrock_inventory(tuple(serialized.split(",")))
    resource_regions = sorted(
        {parse_bedrock_resource(resource).region or "<global>" for resource in inventory}
    )
    if region is None:
        return (
            f"inventory regions {', '.join(resource_regions)} could not be compared: "
            "no --region and no region in the resolved AWS profile"
        )
    mismatches = bedrock_inventory_region_mismatches(inventory, region)
    if mismatches:
        raise CliRefusal(
            f"{_BEDROCK_INVENTORY_PARAMETER} contains resource(s) incompatible with "
            f"deployment region {region}: {', '.join(mismatches)}. A cross-region or "
            "regionless foundation model is accepted only as an exact support resource "
            "for an admitted inference profile in the deployment region; otherwise the "
            "Scene would fail closed."
        )
    routed = [
        resource for resource in inventory if parse_bedrock_resource(resource).region != region
    ]
    detail = f"all {len(inventory)} inventory resource(s) match deployment region {region}"
    if routed:
        detail += f" or are {len(routed)} exact cross-region profile support resource(s)"
    return detail


def _effective_parameters(template: RoleTemplate, supplied: Mapping[str, str]) -> dict[str, str]:
    """Return supplied values plus template defaults, for rendering and diffing."""
    effective = {
        name: str(spec.default)
        for name, spec in template.parameters.items()
        if spec.default is not None
    }
    effective.update(supplied)
    return effective


def _display_parameters(
    template: RoleTemplate, parameters: Mapping[str, str]
) -> dict[str, str | list[str]]:
    """Return parameter values with list types exposed instead of serialized strings."""
    displayed: dict[str, str | list[str]] = {}
    for name, value in parameters.items():
        spec = template.parameters.get(name)
        if spec is None:
            displayed[name] = value
            continue
        normalized = spec.normalize(value)
        if name == _BEDROCK_INVENTORY_PARAMETER and isinstance(normalized, list):
            normalized = list(parse_bedrock_inventory(tuple(normalized)))
        displayed[name] = normalized
    return displayed


def _validated_stack_name(name: str, *, derived_from: str | None) -> str:
    """Check a stack name locally, since CloudFormation's rules are narrower.

    The template admits role names containing ``_ + = , . @`` or starting with a
    digit, none of which CloudFormation accepts in a stack name. Deriving the
    stack name would otherwise pass every offline check and fail at the AWS call.
    """
    if _STACK_NAME_RE.fullmatch(name):
        return name
    if derived_from is None:
        raise CliRefusal(
            f"--stack-name {name!r} is not a valid CloudFormation stack name: it must "
            "start with a letter and contain only letters, digits, and hyphens, up to "
            "128 characters"
        )
    raise CliRefusal(
        f"the stack name defaulted from --{derived_from} is {name!r}, which "
        "CloudFormation does not accept: a stack name must start with a letter and "
        "contain only letters, digits, and hyphens, up to 128 characters. Pass an "
        "explicit --stack-name."
    )


def _stack_name(args: argparse.Namespace, template: RoleTemplate) -> str:
    """Default the stack name from the role name so the two cannot diverge."""
    if getattr(args, "stack_name", None):
        return _validated_stack_name(str(args.stack_name), derived_from=None)
    role_name = getattr(args, "role_name", None)
    if role_name:
        return _validated_stack_name(str(role_name), derived_from="role-name")
    default = template.parameters["RoleName"].default if "RoleName" in template.parameters else None
    if default is None:
        raise CliRefusal("supply --stack-name or --role-name")
    return _validated_stack_name(str(default), derived_from="role-name")


def _run_cfn_lint(template: RoleTemplate) -> list[str]:
    try:
        from cfnlint import api
    except ImportError as error:  # pragma: no cover - exercised by the import guard test
        raise CliRefusal(
            "--lint needs cfn-lint, which ships only with the 'dev' extra: "
            f"pip install 'raja-sts[dev]' ({error})"
        ) from error
    return [str(match) for match in api.lint_all(template.body)]


def validate_command(runtime: Runtime, args: argparse.Namespace) -> int:
    """Check the template and any supplied parameters without contacting AWS."""
    emitter = Emitter(runtime, args.json_output)
    template = load_role_template(args.template_file)
    supplied = _supplied_parameters(args)
    template.validate_parameters(supplied)
    region = _resolve_region(runtime, args)
    region_detail = _check_model_region(supplied, region)

    findings: list[str] = []
    if args.lint:
        findings = _run_cfn_lint(template)
        if findings:
            raise CliRefusal(
                "cfn-lint reported findings in " + template.source + ":\n" + "\n".join(findings)
            )

    checks = [
        {"name": "template-parsed", "detail": f"{template.source} declares {ROLE_ARN_OUTPUT}"},
        {
            "name": "parameter-constraints",
            "detail": (
                f"{len(supplied)} supplied value(s) match the template's AllowedPattern "
                "and numeric bounds"
            ),
        },
        {"name": "model-region-consistency", "detail": region_detail},
        {
            "name": "cfn-lint",
            "detail": "clean" if args.lint else "not requested (--lint)",
        },
    ]

    payload = envelope(INFRA_VALIDATE_SCHEMA, "ok")
    payload.update(
        {
            "template": template.source,
            "region": region,
            "parameters": _display_parameters(template, supplied),
            "parameters_effective": _display_parameters(
                template, _effective_parameters(template, supplied)
            ),
            "stack_name": _stack_name(args, template),
            "capabilities": [NAMED_IAM_CAPABILITY],
            "checks": checks,
            "mutations_performed": [],
            "notes": list(TEMPLATE_NOTES),
        }
    )

    for entry in checks:
        emitter.line(f"pass  {entry['name']}")
        emitter.line(f"      {entry['detail']}")
    emitter.line("")
    emitter.line("No AWS API was called. Notes:")
    for note in TEMPLATE_NOTES:
        emitter.line(f"  - {note}")
    emitter.document(payload)
    return EXIT_OK


def _describe_stack(client: Any, stack_name: str) -> Mapping[str, Any] | None:
    try:
        response = client.describe_stacks(StackName=stack_name)
    except Exception as error:
        if _looks_absent(error):
            return None
        raise CliRefusal(f"unable to describe stack {stack_name!r}: {error}") from error
    stacks = response.get("Stacks") or []
    if not stacks:
        return None
    first = stacks[0]
    if not isinstance(first, Mapping):
        raise CliRefusal(f"unexpected describe_stacks response for {stack_name!r}")
    return first


def _stack_outputs(stack: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(entry.get("OutputKey")): str(entry.get("OutputValue"))
        for entry in stack.get("Outputs") or []
        if isinstance(entry, Mapping)
    }


def _stack_parameters(stack: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(entry.get("ParameterKey")): str(entry.get("ParameterValue"))
        for entry in stack.get("Parameters") or []
        if isinstance(entry, Mapping)
    }


def _change_set_name(verb: str) -> str:
    return f"pearls-{verb}-{secrets.token_hex(6)}"


def _await_change_set(
    runtime: Runtime,
    client: Any,
    *,
    change_set_name: str,
    stack_name: str,
    wait_seconds: int,
) -> Mapping[str, Any]:
    waited = 0.0
    while True:
        try:
            described = client.describe_change_set(
                ChangeSetName=change_set_name, StackName=stack_name
            )
        except Exception as error:
            raise CliRefusal(f"unable to describe change set {change_set_name}: {error}") from error
        if not isinstance(described, Mapping):
            raise CliRefusal("unexpected describe_change_set response")
        if str(described.get("Status")) in _CHANGE_SET_DONE:
            return described
        if waited >= wait_seconds:
            raise CliRefusal(
                f"change set {change_set_name} did not reach a terminal status within "
                f"{wait_seconds}s"
            )
        runtime.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL


def _is_no_change(described: Mapping[str, Any]) -> bool:
    if str(described.get("Status")) != "FAILED":
        return False
    reason = str(described.get("StatusReason", "")).lower()
    return any(fragment in reason for fragment in _NO_CHANGE_FRAGMENTS)


def _delete_change_set(emitter: Emitter, client: Any, *, name: str, stack_name: str) -> None:
    try:
        client.delete_change_set(ChangeSetName=name, StackName=stack_name)
    except Exception as error:  # Cleanup only; the review already happened.
        emitter.note(f"warning: could not delete change set {name}: {error}")


def _rendered_policies(template: RoleTemplate, parameters: Mapping[str, str]) -> dict[str, Any]:
    properties = template.role_policies
    rendered_parameters = _display_parameters(template, parameters)
    return {
        "role_name": render_intrinsics(properties.get("RoleName"), rendered_parameters),
        "max_session_duration": render_intrinsics(
            properties.get("MaxSessionDuration"), rendered_parameters
        ),
        "trust_policy": render_intrinsics(
            properties.get("AssumeRolePolicyDocument"), rendered_parameters
        ),
        "inline_policies": render_intrinsics(properties.get("Policies"), rendered_parameters),
    }


def _parameter_diff(
    template: RoleTemplate,
    current: Mapping[str, str],
    requested: Mapping[str, str],
) -> list[dict[str, Any]]:
    before_values = _display_parameters(template, current)
    after_values = _display_parameters(template, requested)
    diff: list[dict[str, Any]] = []
    for name in sorted(set(before_values) | set(after_values)):
        before = before_values.get(name)
        after = after_values.get(name)
        diff.append(
            {
                "parameter": name,
                "current": before,
                "requested": after,
                "changed": before != after,
            }
        )
    return diff


def _resource_changes(described: Mapping[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for change in described.get("Changes") or []:
        if not isinstance(change, Mapping):
            continue
        detail = change.get("ResourceChange")
        if not isinstance(detail, Mapping):
            continue
        changes.append(
            {
                "action": str(detail.get("Action", "")),
                "logical_id": str(detail.get("LogicalResourceId", "")),
                "resource_type": str(detail.get("ResourceType", "")),
                "replacement": detail.get("Replacement"),
            }
        )
    return changes


class _Plan:
    """One rendered change set plus everything needed to execute or report it."""

    def __init__(
        self,
        *,
        template: RoleTemplate,
        stack_name: str,
        parameters: dict[str, str],
        operation: str,
        change_set_name: str | None,
        described: Mapping[str, Any] | None,
        no_change: bool,
        existing: Mapping[str, Any] | None,
    ) -> None:
        self.template = template
        self.stack_name = stack_name
        self.parameters = parameters
        self.operation = operation
        self.change_set_name = change_set_name
        self.described = described
        self.no_change = no_change
        self.existing = existing

    def document(self, schema: str, status: str) -> dict[str, Any]:
        payload = envelope(schema, status)
        payload.update(
            {
                "template": self.template.source,
                "stack_name": self.stack_name,
                "operation": self.operation,
                "change_set_name": self.change_set_name,
                "capabilities": [NAMED_IAM_CAPABILITY],
                "parameters": _display_parameters(self.template, self.parameters),
                "parameter_diff": _parameter_diff(
                    self.template,
                    _stack_parameters(self.existing) if self.existing else {},
                    self.parameters,
                ),
                "resource_changes": (
                    _resource_changes(self.described) if self.described is not None else []
                ),
                "rendered": _rendered_policies(self.template, self.parameters),
                "notes": list(TEMPLATE_NOTES),
            }
        )
        return payload

    def render(self, emitter: Emitter) -> None:
        """Print the review material an operator needs before approving.

        Written to the review channel, so ``--json`` moves it to stderr instead of
        suppressing it. A confirmation prompt must never be the first thing an
        operator sees.
        """
        write = emitter.review
        render_table(
            emitter,
            [
                ("template", self.template.source),
                ("stack", self.stack_name),
                ("operation", self.operation),
                ("capabilities", NAMED_IAM_CAPABILITY),
            ],
            review=True,
        )
        write("")
        write("Parameters:")
        for entry in _parameter_diff(
            self.template,
            _stack_parameters(self.existing) if self.existing else {},
            self.parameters,
        ):
            marker = "~" if entry["changed"] else " "
            write(
                f"  {marker} {entry['parameter']}: {entry['current']!r} -> {entry['requested']!r}"
            )
        write("")
        if self.no_change:
            write("Resource changes: none; the deployed stack already matches.")
        else:
            write("Resource changes:")
            for change in _resource_changes(self.described) if self.described else []:
                write(f"  {change['action']} {change['logical_id']} ({change['resource_type']})")
            write(
                f"  This creates or updates named IAM resources, so {NAMED_IAM_CAPABILITY} "
                "is passed explicitly."
            )
        write("")
        rendered = _rendered_policies(self.template, self.parameters)
        write("Rendered trust policy:")
        write(json.dumps(rendered["trust_policy"], indent=2, sort_keys=True))
        write("Rendered inline role policy:")
        write(json.dumps(rendered["inline_policies"], indent=2, sort_keys=True))
        write("")
        for note in TEMPLATE_NOTES:
            write(f"  - {note}")


def _build_plan(
    runtime: Runtime,
    args: argparse.Namespace,
    *,
    verb: str,
    emitter: Emitter,
    client: Any,
) -> _Plan:
    """Validate offline, then create and render one change set without executing it."""
    template = load_role_template(args.template_file)
    supplied = _supplied_parameters(args)
    template.validate_parameters(supplied)
    _check_model_region(supplied, _resolve_region(runtime, args))
    parameters = _effective_parameters(template, supplied)
    stack_name = _stack_name(args, template)

    existing = _describe_stack(client, stack_name)
    # A stack left in REVIEW_IN_PROGRESS by an unexecuted change set has no
    # resources yet, so CloudFormation still requires a CREATE change set.
    reviewable = existing is not None and str(existing.get("StackStatus")) == "REVIEW_IN_PROGRESS"
    operation = "UPDATE" if existing is not None and not reviewable else "CREATE"
    change_set_name = _change_set_name(verb)
    try:
        client.create_change_set(
            StackName=stack_name,
            ChangeSetName=change_set_name,
            ChangeSetType=operation,
            TemplateBody=template.body,
            Parameters=[
                {"ParameterKey": name, "ParameterValue": value}
                for name, value in sorted(parameters.items())
            ],
            Capabilities=[NAMED_IAM_CAPABILITY],
        )
    except Exception as error:
        raise CliRefusal(f"unable to create a change set for {stack_name!r}: {error}") from error

    try:
        described = _await_change_set(
            runtime,
            client,
            change_set_name=change_set_name,
            stack_name=stack_name,
            wait_seconds=args.wait_seconds,
        )
        no_change = _is_no_change(described)
        if not no_change and str(described.get("Status")) == "FAILED":
            reason = described.get("StatusReason", "no reason reported")
            raise CliRefusal(f"change set for {stack_name!r} failed: {reason}")
    except BaseException:
        # Never leave an unreviewed change set behind, whatever went wrong.
        _delete_change_set(emitter, client, name=change_set_name, stack_name=stack_name)
        raise

    return _Plan(
        template=template,
        stack_name=stack_name,
        parameters=parameters,
        operation=operation,
        change_set_name=change_set_name,
        described=described,
        no_change=no_change,
        existing=existing,
    )


def _finish_plan(
    emitter: Emitter,
    client: Any,
    plan: _Plan,
    *,
    keep_change_set: bool,
    trailer: str,
) -> int:
    """Report a rendered-but-unexecuted plan and clean up after it.

    Shared by ``pearls infra plan`` and ``pearls infra deploy --plan-only`` so the
    documented alias cannot report a different status or drop a warning.
    """
    status = "no-change" if plan.no_change else "planned"
    payload = plan.document(INFRA_PLAN_SCHEMA, status)
    if keep_change_set and not plan.no_change:
        emitter.note(f"change set {plan.change_set_name} was kept and was not executed")
        payload["change_set_retained"] = True
    else:
        _delete_change_set(
            emitter, client, name=plan.change_set_name or "", stack_name=plan.stack_name
        )
        payload["change_set_retained"] = False
    payload["mutations_performed"] = []
    emitter.line("")
    emitter.line(trailer)
    if plan.operation == "CREATE":
        emitter.note(REVIEW_SHELL_NOTE)
        payload["notes"] = [*payload["notes"], REVIEW_SHELL_NOTE]
    emitter.document(payload)
    return EXIT_OK


def plan_command(runtime: Runtime, args: argparse.Namespace) -> int:
    """Render a change set for review and delete it again."""
    emitter = Emitter(runtime, args.json_output)
    client = build_session(runtime, args).client("cloudformation")
    plan = _build_plan(runtime, args, verb="plan", emitter=emitter, client=client)
    plan.render(emitter)
    return _finish_plan(
        emitter,
        client,
        plan,
        keep_change_set=args.keep_change_set,
        trailer="This was a plan. No stack resource was created, updated, or deleted.",
    )


def _failure_reasons(client: Any, stack_name: str) -> list[str]:
    try:
        response = client.describe_stack_events(StackName=stack_name)
    except Exception as error:  # The status itself is already reported.
        return [f"stack events unavailable: {error}"]
    reasons: list[str] = []
    for event in response.get("StackEvents") or []:
        if not isinstance(event, Mapping):
            continue
        status = str(event.get("ResourceStatus", ""))
        reason = event.get("ResourceStatusReason")
        if status.endswith("FAILED") and isinstance(reason, str) and reason:
            entry = f"{event.get('LogicalResourceId')}: {status}: {reason}"
            if entry not in reasons:
                reasons.append(entry)
    return reasons


def _await_stack(
    runtime: Runtime,
    client: Any,
    *,
    stack_name: str,
    wait_seconds: int,
    before: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Wait for a terminal stack status that belongs to the change we executed.

    ``before`` is the stack as it looked prior to execution. An update of an
    already-complete stack would otherwise read back its previous terminal status
    on the first poll and report stale outputs as success, so a terminal status is
    only accepted once CloudFormation has visibly moved: either a non-terminal
    status was observed, or ``LastUpdatedTime`` advanced.
    """
    previous_status = str(before.get("StackStatus", "")) if before is not None else ""
    previous_updated = before.get("LastUpdatedTime") if before is not None else None
    moved = before is None or previous_status not in _STACK_SUCCESS | _STACK_FAILED
    waited = 0.0
    while True:
        stack = _describe_stack(client, stack_name)
        if stack is None:
            raise CliRefusal(f"stack {stack_name!r} disappeared while waiting")
        status = str(stack.get("StackStatus", ""))
        terminal = status in _STACK_SUCCESS or status in _STACK_FAILED
        if not moved and (not terminal or stack.get("LastUpdatedTime") != previous_updated):
            moved = True
        if moved and terminal:
            return stack
        if waited >= wait_seconds:
            detail = (
                f"was still {status} after {wait_seconds}s; the deployment is still in "
                "progress in CloudFormation"
                if moved
                else (
                    f"still reported its pre-execution status {status} after "
                    f"{wait_seconds}s, so the result of the change set could not be "
                    "confirmed; check CloudFormation directly"
                )
            )
            raise CliRefusal(f"stack {stack_name!r} {detail}")
        runtime.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL


def deploy_command(runtime: Runtime, args: argparse.Namespace) -> int:
    """Execute one reviewed change set against one stack."""
    emitter = Emitter(runtime, args.json_output)
    client = build_session(runtime, args).client("cloudformation")
    plan = _build_plan(runtime, args, verb="deploy", emitter=emitter, client=client)
    plan.render(emitter)

    if args.plan_only:
        return _finish_plan(
            emitter,
            client,
            plan,
            keep_change_set=False,
            trailer="--plan-only: nothing was executed.",
        )

    if plan.no_change:
        _delete_change_set(
            emitter, client, name=plan.change_set_name or "", stack_name=plan.stack_name
        )
        stack = plan.existing or {}
        role_arn = _stack_outputs(stack).get(ROLE_ARN_OUTPUT)
        payload = plan.document(INFRA_DEPLOY_SCHEMA, "no-change")
        payload["stack_status"] = str(stack.get("StackStatus", ""))
        payload["role_arn"] = role_arn
        payload["mutations_performed"] = []
        emitter.line("")
        emitter.line(f"No change: {plan.stack_name} already matches these parameters.")
        if role_arn:
            emitter.line(role_arn)
        emitter.document(payload)
        return EXIT_OK

    # A change set that is never executed must not outlive the command, whether
    # the operator declined, stdin could not answer, or the call failed. Once
    # CloudFormation accepts it for execution it belongs to the stack's history,
    # so success deliberately leaves it in place.
    executed = False
    try:
        confirm(
            runtime,
            question=(
                f"Execute this change set against stack {plan.stack_name!r} and create or "
                f"update named IAM resources with {NAMED_IAM_CAPABILITY}?"
            ),
            assume_yes=args.assume_yes,
        )
        client.execute_change_set(ChangeSetName=plan.change_set_name, StackName=plan.stack_name)
        executed = True
    except CliRefusal:
        raise
    except Exception as error:
        raise CliRefusal(
            f"unable to execute the change set for {plan.stack_name!r}: {error}"
        ) from error
    finally:
        if not executed:
            _delete_change_set(
                emitter, client, name=plan.change_set_name or "", stack_name=plan.stack_name
            )
            if plan.operation == "CREATE":
                emitter.note(REVIEW_SHELL_NOTE)
    emitter.note(f"executing change set {plan.change_set_name} against {plan.stack_name}")

    stack = _await_stack(
        runtime,
        client,
        stack_name=plan.stack_name,
        wait_seconds=args.wait_seconds,
        before=plan.existing,
    )
    stack_status = str(stack.get("StackStatus", ""))
    outputs = _stack_outputs(stack)
    role_arn = outputs.get(ROLE_ARN_OUTPUT)

    if stack_status not in _STACK_SUCCESS:
        reasons = _failure_reasons(client, plan.stack_name)
        payload = plan.document(INFRA_DEPLOY_SCHEMA, "failed")
        payload["stack_status"] = stack_status
        payload["failure_reasons"] = reasons
        payload["role_arn"] = role_arn
        payload["mutations_performed"] = ["execute_change_set"]
        emitter.document(payload)
        raise CliRefusal(
            f"stack {plan.stack_name!r} ended in {stack_status}. CloudFormation reported:\n"
            + ("\n".join(f"  - {reason}" for reason in reasons) or "  - no failure reason reported")
        )

    payload = plan.document(INFRA_DEPLOY_SCHEMA, "deployed")
    payload["stack_status"] = stack_status
    payload["role_arn"] = role_arn
    payload["outputs"] = outputs
    payload["mutations_performed"] = ["execute_change_set"]
    emitter.line("")
    emitter.line(f"{plan.stack_name}: {stack_status}")
    if role_arn is None:
        emitter.note(f"warning: the stack exports no {ROLE_ARN_OUTPUT} output")
    else:
        emitter.line(role_arn)
        emitter.line(f"Use it directly: pearls scene run --role-arn {role_arn} ...")
    emitter.line(BEDROCK_ACCESS_NOTE)
    emitter.document(payload)
    return EXIT_OK


def status_command(runtime: Runtime, args: argparse.Namespace) -> int:
    """Report one deployed stack and the identity it grants."""
    emitter = Emitter(runtime, args.json_output)
    stack_name = args.stack_name or args.role_name
    if not stack_name:
        raise CliRefusal("supply --stack-name or --role-name")
    client = build_session(runtime, args).client("cloudformation")
    stack = _describe_stack(client, str(stack_name))
    if stack is None:
        raise CliRefusal(f"stack {stack_name!r} does not exist in this account and region")

    parameters = _stack_parameters(stack)
    displayed_parameters = _display_parameters(load_role_template(), parameters)
    outputs = _stack_outputs(stack)
    inventory = displayed_parameters.get(_BEDROCK_INVENTORY_PARAMETER)
    identity = {
        "role_arn": outputs.get(ROLE_ARN_OUTPUT),
        "role_name": parameters.get("RoleName"),
        "admitted_bedrock_resources": inventory if isinstance(inventory, list) else [],
        "project_bucket_arn_pattern": parameters.get("ProjectBucketArnPattern"),
        "orchestrator_principal_arn": parameters.get("OrchestratorPrincipalArn"),
        "max_session_duration": parameters.get("MaxSessionDuration"),
    }
    payload = envelope(INFRA_STATUS_SCHEMA, "ok")
    payload.update(
        {
            "stack_name": str(stack_name),
            "stack_status": str(stack.get("StackStatus", "")),
            "parameters": displayed_parameters,
            "outputs": outputs,
            "identity": identity,
            "mutations_performed": [],
            "notes": [BEDROCK_ACCESS_NOTE, INTERSECTION_NOTE],
        }
    )
    render_table(
        emitter,
        [
            ("stack", str(stack_name)),
            ("status", str(stack.get("StackStatus", ""))),
            *[(key.replace("_", " "), str(value)) for key, value in identity.items()],
        ],
    )
    emitter.line("")
    emitter.line(INTERSECTION_NOTE)
    emitter.document(payload)
    return EXIT_OK


def _assume_role_document(role_arns: Sequence[str]) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AssumeSceneExecutionRoles",
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Resource": list(role_arns),
            }
        ],
    }


_IAM_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+$")


def assume_policy_command(runtime: Runtime, args: argparse.Namespace) -> int:
    """Print, and only optionally apply, the orchestrator's assume-role policy."""
    emitter = Emitter(runtime, args.json_output)
    session = build_session(runtime, args)
    role_arns: list[str] = []
    resolved_from: dict[str, str] = {}

    if args.from_stack:
        cloudformation = session.client("cloudformation")
        for stack_name in args.from_stack:
            stack = _describe_stack(cloudformation, str(stack_name))
            if stack is None:
                raise CliRefusal(f"stack {stack_name!r} does not exist in this account and region")
            role_arn = _stack_outputs(stack).get(ROLE_ARN_OUTPUT)
            if role_arn is None:
                raise CliRefusal(f"stack {stack_name!r} exports no {ROLE_ARN_OUTPUT} output")
            # Validated like an operator-supplied ARN: this value ends up as a
            # Resource in a granted sts:AssumeRole statement.
            if not _IAM_ROLE_ARN_RE.fullmatch(role_arn):
                raise CliRefusal(
                    f"stack {stack_name!r} exported {ROLE_ARN_OUTPUT}={role_arn!r}, "
                    "which is not a complete IAM role ARN"
                )
            resolved_from[str(stack_name)] = role_arn
            role_arns.append(role_arn)

    for role_arn in args.role_arn:
        if not _IAM_ROLE_ARN_RE.fullmatch(str(role_arn)):
            raise CliRefusal(f"not a complete IAM role ARN: {role_arn!r}")
        role_arns.append(str(role_arn))

    unique = sorted(dict.fromkeys(role_arns))
    if not unique:
        raise CliRefusal("supply at least one --role-arn or --from-stack")

    document = _assume_role_document(unique)
    target_type = "user" if args.apply_to_user else ("role" if args.apply_to_role else None)
    target_name = args.apply_to_user or args.apply_to_role
    payload = envelope(INFRA_ASSUME_POLICY_SCHEMA, "printed")
    payload.update(
        {
            "policy_name": args.policy_name,
            "role_arns": unique,
            "resolved_from_stacks": resolved_from,
            "policy_document": document,
            "applied": False,
            "target": None,
            "mutations_performed": [],
        }
    )

    emitter.line(json.dumps(document, indent=2, sort_keys=True))
    if target_type is None:
        emitter.line("")
        emitter.line(
            "Printed only. Attach it yourself, or re-run with --apply-to-user/--apply-to-role."
        )
        emitter.document(payload)
        return EXIT_OK

    emitter.note(
        f"warning: applying inline policy {args.policy_name!r} to IAM {target_type} "
        f"{target_name!r} replaces any existing inline policy of that exact name."
    )
    confirm(
        runtime,
        question=(
            f"Replace inline policy {args.policy_name!r} on IAM {target_type} {target_name!r} "
            f"with an sts:AssumeRole grant on {len(unique)} role(s)?"
        ),
        assume_yes=args.assume_yes,
    )
    iam = session.client("iam")
    body = json.dumps(document, sort_keys=True)
    try:
        if target_type == "user":
            iam.put_user_policy(
                UserName=target_name, PolicyName=args.policy_name, PolicyDocument=body
            )
        else:
            iam.put_role_policy(
                RoleName=target_name, PolicyName=args.policy_name, PolicyDocument=body
            )
    except Exception as error:
        raise CliRefusal(
            f"unable to apply inline policy {args.policy_name!r} to {target_type} "
            f"{target_name!r}: {error}"
        ) from error

    payload["status"] = "applied"
    payload["applied"] = True
    payload["target"] = {"type": target_type, "name": target_name}
    payload["mutations_performed"] = [f"put_{target_type}_policy"]
    emitter.line("")
    emitter.line(f"Applied {args.policy_name!r} to IAM {target_type} {target_name!r}.")
    emitter.document(payload)
    return EXIT_OK
