"""Offline reading of the packaged Scene execution-role CloudFormation template.

The template stays the single source of truth for parameter constraints: the CLI
reads ``AllowedPattern``, ``MinValue``, and ``MaxValue`` from the template rather
than restating them, so client-side validation cannot drift from what
CloudFormation will enforce.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ._common import CliRefusal

TEMPLATE_RESOURCE = "scene-execution-role.yaml"
ROLE_RESOURCE_ID = "SceneExecutionRole"
ROLE_ARN_OUTPUT = "SceneExecutionRoleArn"


class _CfnLoader(yaml.SafeLoader):  # type: ignore[misc]  # yaml ships no stubs here
    """Safe loader that keeps CloudFormation short-form intrinsics inspectable.

    Subclassing keeps the registered intrinsic constructors off ``yaml.SafeLoader``
    itself, so parsing a template cannot change YAML behavior elsewhere.
    """


def _intrinsic(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> dict[str, Any]:
    name = "Fn::GetAtt" if suffix == "GetAtt" else ("Ref" if suffix == "Ref" else f"Fn::{suffix}")
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {name: value}


_CfnLoader.add_multi_constructor("!", _intrinsic)


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """One template parameter and the constraints the template declares for it."""

    name: str
    type: str
    default: Any | None = None
    allowed_pattern: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    description: str | None = None

    def normalize(self, value: str) -> str | list[str]:
        """Validate one supplied value and return its CloudFormation ``Ref`` shape."""
        if self.type == "CommaDelimitedList":
            entries = [entry.strip() for entry in value.split(",")]
            if not entries or any(not entry for entry in entries):
                raise CliRefusal(f"{self.name} must be a non-empty comma-delimited list")
            for entry in entries:
                self._validate_pattern(entry)
            return entries

        self._validate_pattern(value)
        if self.type == "Number":
            try:
                numeric = float(value)
            except ValueError as error:
                raise CliRefusal(f"{self.name} must be a number, got {value!r}") from error
            if self.min_value is not None and numeric < self.min_value:
                raise CliRefusal(f"{self.name} must be at least {self.min_value:g}, got {value}")
            if self.max_value is not None and numeric > self.max_value:
                raise CliRefusal(f"{self.name} must be at most {self.max_value:g}, got {value}")
        return value

    def _validate_pattern(self, value: str) -> None:
        if self.allowed_pattern is not None and not re.fullmatch(self.allowed_pattern, value):
            raise CliRefusal(
                f"{self.name} value {value!r} does not match the template's "
                f"AllowedPattern {self.allowed_pattern}"
            )

    def validate(self, value: str) -> None:
        """Check one supplied value against the template's own constraints."""
        self.normalize(value)


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    """A parsed Scene execution-role template."""

    source: str
    body: str
    document: Mapping[str, Any]
    parameters: Mapping[str, ParameterSpec]

    def validate_parameters(self, values: Mapping[str, str]) -> None:
        """Validate supplied values and reject names the template does not declare."""
        unknown = sorted(set(values) - set(self.parameters))
        if unknown:
            raise CliRefusal("the template declares no such parameter(s): " + ", ".join(unknown))
        for name, value in values.items():
            self.parameters[name].validate(value)

    def normalize_parameters(self, values: Mapping[str, str]) -> dict[str, str | list[str]]:
        """Return values in the shape produced by CloudFormation ``Ref``."""
        self.validate_parameters(values)
        return {name: self.parameters[name].normalize(value) for name, value in values.items()}

    @property
    def role_policies(self) -> Mapping[str, Any]:
        """Return the role resource's properties for rendering."""
        resources_section = self.document.get("Resources")
        if not isinstance(resources_section, Mapping):
            raise CliRefusal(f"{self.source} declares no Resources section")
        role = resources_section.get(ROLE_RESOURCE_ID)
        if not isinstance(role, Mapping):
            raise CliRefusal(f"{self.source} declares no {ROLE_RESOURCE_ID} resource")
        properties = role.get("Properties")
        if not isinstance(properties, Mapping):
            raise CliRefusal(f"{ROLE_RESOURCE_ID} in {self.source} has no Properties")
        return properties


def _parameter_specs(document: Mapping[str, Any], source: str) -> dict[str, ParameterSpec]:
    section = document.get("Parameters")
    if not isinstance(section, Mapping) or not section:
        raise CliRefusal(f"{source} declares no Parameters section")
    specs: dict[str, ParameterSpec] = {}
    for name, raw in section.items():
        if not isinstance(raw, Mapping):
            raise CliRefusal(f"parameter {name!r} in {source} is not a mapping")
        specs[str(name)] = ParameterSpec(
            name=str(name),
            type=str(raw.get("Type", "String")),
            default=raw.get("Default"),
            allowed_pattern=(
                str(raw["AllowedPattern"]) if raw.get("AllowedPattern") is not None else None
            ),
            min_value=float(raw["MinValue"]) if raw.get("MinValue") is not None else None,
            max_value=float(raw["MaxValue"]) if raw.get("MaxValue") is not None else None,
            description=str(raw["Description"]) if raw.get("Description") is not None else None,
        )
    return specs


def load_role_template(template_file: str | None = None) -> RoleTemplate:
    """Load the packaged template, or an explicit ``--template-file`` override."""
    if template_file is None:
        source = f"raja_sts/templates/{TEMPLATE_RESOURCE}"
        try:
            body = (
                resources.files("raja_sts.templates")
                .joinpath(TEMPLATE_RESOURCE)
                .read_text(encoding="utf-8")
            )
        except (FileNotFoundError, ModuleNotFoundError, OSError) as error:
            raise CliRefusal(f"the installed package has no {source}: {error}") from error
    else:
        path = Path(template_file)
        source = str(path)
        if not path.is_file():
            raise CliRefusal(f"template file does not exist: {path}")
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise CliRefusal(f"template file is not readable UTF-8: {error}") from error

    try:
        document = yaml.load(body, Loader=_CfnLoader)
    except yaml.YAMLError as error:
        raise CliRefusal(f"{source} is not parseable YAML: {error}") from error
    if not isinstance(document, Mapping):
        raise CliRefusal(f"{source} must contain a CloudFormation template mapping")
    outputs = document.get("Outputs")
    if not isinstance(outputs, Mapping) or ROLE_ARN_OUTPUT not in outputs:
        raise CliRefusal(f"{source} does not export the {ROLE_ARN_OUTPUT} output")

    return RoleTemplate(
        source=source,
        body=body,
        document=document,
        parameters=_parameter_specs(document, source),
    )


def render_intrinsics(value: Any, parameters: Mapping[str, Any]) -> Any:
    """Substitute ``Ref`` and ``Fn::Sub`` parameter references for review output.

    Only the forms this template uses are supported. Anything else is left as a
    marked placeholder rather than silently rendered wrong.
    """
    if isinstance(value, Mapping):
        if set(value) == {"Ref"}:
            name = str(value["Ref"])
            return parameters.get(name, f"<unresolved Ref {name}>")
        if set(value) == {"Fn::Sub"} and isinstance(value["Fn::Sub"], str):
            return re.sub(
                r"\$\{([A-Za-z0-9:]+)\}",
                lambda match: (
                    str(parameters[match.group(1)])
                    if match.group(1) in parameters
                    else f"<unresolved Sub {match.group(1)}>"
                ),
                value["Fn::Sub"],
            )
        if set(value) == {"Fn::GetAtt"}:
            target = value["Fn::GetAtt"]
            joined = ".".join(target) if isinstance(target, list) else str(target)
            return f"<resolved at deploy time: {joined}>"
        return {key: render_intrinsics(item, parameters) for key, item in value.items()}
    if isinstance(value, list):
        return [render_intrinsics(item, parameters) for item in value]
    return value
