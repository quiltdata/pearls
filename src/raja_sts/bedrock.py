"""Exact Bedrock invocation identifiers and IAM resource closures."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .errors import InvalidScopeError

BedrockResourceKind = Literal["foundation-model", "inference-profile"]
BedrockProfileScope = Literal["us", "eu", "apac", "global"]
BedrockProfileLoader = Callable[[str], Sequence[str]]

# The ``aws`` partition has a narrower commercial region grammar than the
# generic Bedrock API pattern. In particular, this excludes us-gov, China, and
# ISO regions even if a malformed ARN pairs one with the commercial partition.
_COMMERCIAL_REGION = r"(?:af|ap|ca|eu|il|me|mx|sa|us)-[a-z]+-\d+"
_RESOURCE_ID = r"[A-Za-z0-9][A-Za-z0-9._:-]*"
_FOUNDATION_MODEL_RE = re.compile(
    rf"^arn:aws:bedrock:(?P<region>{_COMMERCIAL_REGION}):"
    rf":foundation-model/(?P<resource_id>{_RESOURCE_ID})$"
)
_REGIONLESS_FOUNDATION_MODEL_RE = re.compile(
    rf"^arn:aws:bedrock:::foundation-model/(?P<resource_id>{_RESOURCE_ID})$"
)
_INFERENCE_PROFILE_RE = re.compile(
    rf"^arn:aws:bedrock:(?P<region>{_COMMERCIAL_REGION}):"
    rf"(?P<account>\d{{12}}):inference-profile/"
    rf"(?P<scope>us|eu|apac|global)\.(?P<model_id>{_RESOURCE_ID})$"
)
_PROFILE_REGION_PREFIX: dict[BedrockProfileScope, str] = {
    "us": "us-",
    "eu": "eu-",
    "apac": "ap-",
    "global": "",
}


@dataclass(frozen=True, slots=True)
class BedrockResource:
    """One parsed exact commercial-partition Bedrock IAM resource."""

    arn: str
    kind: BedrockResourceKind
    region: str | None
    resource_id: str
    account: str | None = None
    profile_scope: BedrockProfileScope | None = None

    @property
    def foundation_model_id(self) -> str:
        """Return the underlying foundation model ID for either resource kind."""
        if self.kind == "foundation-model":
            return self.resource_id
        _scope, separator, model_id = self.resource_id.partition(".")
        if not separator:  # The profile parser always supplies a scoped ID.
            raise InvalidScopeError(f"invalid inference-profile ID: {self.resource_id!r}")
        return model_id


def parse_bedrock_resource(value: object, *, label: str = "Bedrock ARN") -> BedrockResource:
    """Parse one exact supported IAM ARN and reject wildcards or other resources."""
    if isinstance(value, str):
        foundation = _FOUNDATION_MODEL_RE.fullmatch(value)
        if foundation is not None:
            return BedrockResource(
                arn=value,
                kind="foundation-model",
                region=foundation.group("region"),
                resource_id=foundation.group("resource_id"),
            )
        regionless = _REGIONLESS_FOUNDATION_MODEL_RE.fullmatch(value)
        if regionless is not None:
            return BedrockResource(
                arn=value,
                kind="foundation-model",
                region=None,
                resource_id=regionless.group("resource_id"),
            )
        profile = _INFERENCE_PROFILE_RE.fullmatch(value)
        if profile is not None:
            scope = profile.group("scope")
            if scope not in _PROFILE_REGION_PREFIX:  # Narrow the regex result for mypy.
                raise InvalidScopeError(f"unsupported inference-profile scope: {scope!r}")
            typed_scope: BedrockProfileScope = scope
            region = profile.group("region")
            prefix = _PROFILE_REGION_PREFIX[typed_scope]
            if prefix and not region.startswith(prefix):
                raise InvalidScopeError(
                    f"{label} uses {typed_scope!r} profile scope from incompatible "
                    f"source region {region!r}"
                )
            return BedrockResource(
                arn=value,
                kind="inference-profile",
                region=region,
                account=profile.group("account"),
                resource_id=f"{scope}.{profile.group('model_id')}",
                profile_scope=typed_scope,
            )
    raise InvalidScopeError(
        f"{label} must be one exact commercial-partition regional foundation-model, "
        "global-profile foundation support, or account-scoped inference-profile ARN"
    )


def parse_bedrock_model(value: object) -> str | None:
    """Validate one exact regional selected invocation ARN, or ``None``."""
    if value is None:
        return None
    selected = parse_bedrock_resource(value, label="bedrock_model")
    if selected.kind == "foundation-model" and selected.region is None:
        raise InvalidScopeError("bedrock_model cannot be a regionless foundation-model support ARN")
    return selected.arn


def bedrock_model_region(model_arn: str) -> str:
    """Return the concrete AWS region named by a selected Bedrock invocation ARN."""
    selected = parse_bedrock_resource(parse_bedrock_model(model_arn), label="bedrock_model")
    if selected.region is None:  # Narrowed by parse_bedrock_model.
        raise InvalidScopeError("bedrock_model must name one concrete AWS region")
    return selected.region


def bedrock_model_account(model_arn: str) -> str | None:
    """Return a profile account, or ``None`` for a selected foundation model."""
    selected = parse_bedrock_resource(parse_bedrock_model(model_arn), label="bedrock_model")
    return selected.account


def _profile_supports(profile: BedrockResource, foundation: BedrockResource) -> bool:
    """Check support-resource shape, model identity, and geographic boundary.

    This deliberately does not claim that a same-model Region is an actual
    destination. Exact membership is verified separately against the models
    returned by ``GetInferenceProfile`` from the selected source Region.
    """
    if (
        profile.kind != "inference-profile"
        or foundation.kind != "foundation-model"
        or profile.profile_scope is None
        or profile.foundation_model_id != foundation.foundation_model_id
    ):
        return False
    if foundation.region is None:
        return profile.profile_scope == "global"
    prefix = _PROFILE_REGION_PREFIX[profile.profile_scope]
    return not prefix or foundation.region.startswith(prefix)


def _validate_profile_supports(
    profile: BedrockResource,
    supports: Sequence[BedrockResource],
    *,
    label: str,
) -> None:
    if not supports or any(resource.kind != "foundation-model" for resource in supports):
        raise InvalidScopeError(f"{label} requires one or more exact foundation-model support ARNs")
    if any(not _profile_supports(profile, resource) for resource in supports):
        raise InvalidScopeError(
            f"every {label} support must name the same exact foundation model and "
            "routing geography as the selected profile"
        )
    if profile.profile_scope == "global":
        if not any(resource.region is None for resource in supports):
            raise InvalidScopeError(
                "a global inference profile requires its exact regionless "
                "foundation-model support ARN"
            )
        if not any(resource.region is not None for resource in supports):
            raise InvalidScopeError(
                "a global inference profile also requires at least one exact regional "
                "foundation-model support ARN"
            )


@dataclass(frozen=True, slots=True)
class BedrockInvocation:
    """One selected Bedrock identifier and its exact IAM resource closure."""

    selected_model_id: str
    invoke_resources: tuple[str, ...]

    def __post_init__(self) -> None:
        selected_arn = parse_bedrock_model(self.selected_model_id)
        if selected_arn is None:  # The dataclass field is non-optional.
            raise InvalidScopeError("selected_model_id must be one exact Bedrock ARN")
        selected = parse_bedrock_resource(selected_arn, label="selected_model_id")
        if not self.invoke_resources:
            raise InvalidScopeError("invoke_resources must be a non-empty tuple of exact ARNs")
        parsed = tuple(
            parse_bedrock_resource(resource, label="invoke_resources entry")
            for resource in self.invoke_resources
        )
        arns = tuple(resource.arn for resource in parsed)
        if len(arns) != len(set(arns)):
            raise InvalidScopeError("invoke_resources must not contain duplicate ARNs")
        if selected.arn not in arns:
            raise InvalidScopeError("invoke_resources must include selected_model_id")

        if selected.kind == "foundation-model":
            if arns != (selected.arn,):
                raise InvalidScopeError(
                    "a foundation-model invocation admits exactly its selected model ARN"
                )
            object.__setattr__(self, "invoke_resources", (selected.arn,))
            return

        supporting = tuple(resource for resource in parsed if resource.arn != selected.arn)
        _validate_profile_supports(
            selected,
            supporting,
            label="inference-profile invocation",
        )
        canonical = (selected.arn, *sorted(resource.arn for resource in supporting))
        object.__setattr__(self, "invoke_resources", canonical)

    @property
    def selected(self) -> BedrockResource:
        """Return the parsed selected invocation resource."""
        return parse_bedrock_resource(self.selected_model_id, label="selected_model_id")

    @property
    def supporting_foundation_models(self) -> tuple[str, ...]:
        """Return exact foundation-model IAM resources evaluated through a profile."""
        if self.selected.kind == "foundation-model":
            return ()
        return self.invoke_resources[1:]

    def report(self) -> dict[str, Any]:
        """Return the non-secret selected identifier and exact IAM closure."""
        return {
            "selected_model_id": self.selected_model_id,
            "selected_resource_type": self.selected.kind,
            "invoke_resources": list(self.invoke_resources),
        }


def parse_bedrock_invocation_declaration(
    model_value: object,
    invoke_resources_value: object = None,
) -> BedrockInvocation | None:
    """Validate a caller's exact selection and expected resource closure.

    For an inference profile this proves only ARN shape and geographic/model
    consistency. :func:`parse_bedrock_invocation` additionally compares the
    declaration with authoritative profile metadata before it can be compiled.
    """
    model_id = parse_bedrock_model(model_value)
    if model_id is None:
        if invoke_resources_value is not None:
            raise InvalidScopeError(
                "bedrock_invoke_resources cannot be supplied without bedrock_model"
            )
        return None

    selected = parse_bedrock_resource(model_id)
    if invoke_resources_value is None:
        if selected.kind == "inference-profile":
            raise InvalidScopeError(
                "bedrock_invoke_resources must explicitly include the selected inference "
                "profile and every routed foundation-model ARN"
            )
        return BedrockInvocation(model_id, (model_id,))

    if (
        isinstance(invoke_resources_value, (str, bytes))
        or not isinstance(invoke_resources_value, Sequence)
        or not invoke_resources_value
    ):
        raise InvalidScopeError("bedrock_invoke_resources must be a non-empty sequence")
    if not all(isinstance(resource, str) for resource in invoke_resources_value):
        raise InvalidScopeError("bedrock_invoke_resources entries must be exact ARN strings")
    return BedrockInvocation(model_id, tuple(invoke_resources_value))


def parse_bedrock_invocation(
    model_value: object,
    invoke_resources_value: object = None,
    *,
    profile_loader: BedrockProfileLoader | None = None,
) -> BedrockInvocation | None:
    """Validate one selection and verify profile destinations authoritatively.

    A profile's exact destination set varies by source Region and can change for
    global profiles, so ARN parsing cannot derive it safely. Profile compilation
    therefore requires a loader backed by ``GetInferenceProfile`` (or an
    equivalent trusted snapshot) and refuses missing, stale, or extra resources.
    """
    declared = parse_bedrock_invocation_declaration(model_value, invoke_resources_value)
    if declared is None or declared.selected.kind == "foundation-model":
        return declared
    if profile_loader is None:
        raise InvalidScopeError(
            "an inference-profile invocation requires authoritative destination "
            "verification with bedrock_profile_loader"
        )

    try:
        loaded = profile_loader(declared.selected_model_id)
    except InvalidScopeError:
        raise
    except Exception as error:
        raise InvalidScopeError(
            f"unable to resolve inference profile {declared.selected_model_id!r}: {error}"
        ) from error
    if isinstance(loaded, (str, bytes)) or not isinstance(loaded, Sequence):
        raise InvalidScopeError("bedrock_profile_loader must return a sequence of model ARNs")
    if not all(isinstance(resource, str) for resource in loaded):
        raise InvalidScopeError("bedrock_profile_loader returned a non-string model ARN")

    authoritative = BedrockInvocation(
        declared.selected_model_id,
        (declared.selected_model_id, *tuple(loaded)),
    )
    if declared.invoke_resources != authoritative.invoke_resources:
        declared_supports = set(declared.supporting_foundation_models)
        authoritative_supports = set(authoritative.supporting_foundation_models)
        missing = sorted(authoritative_supports - declared_supports)
        unexpected = sorted(declared_supports - authoritative_supports)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise InvalidScopeError(
            "declared inference-profile resources do not match the exact "
            "GetInferenceProfile destinations" + (": " + "; ".join(details) if details else "")
        )
    return authoritative


def bedrock_profile_resources_from_response(
    profile_arn: str,
    response: object,
) -> tuple[str, ...]:
    """Extract and validate exact destination model ARNs from an AWS response."""
    selected_arn = parse_bedrock_model(profile_arn)
    if selected_arn is None:
        raise InvalidScopeError("inference profile ARN is required")
    selected = parse_bedrock_resource(selected_arn, label="inference profile ARN")
    if selected.kind != "inference-profile":
        raise InvalidScopeError("GetInferenceProfile resolution requires an inference-profile ARN")
    if not isinstance(response, Mapping):
        raise InvalidScopeError("GetInferenceProfile returned a non-mapping response")
    if response.get("inferenceProfileArn") != selected.arn:
        raise InvalidScopeError(
            "GetInferenceProfile returned an identity different from the selected profile"
        )
    if response.get("status") != "ACTIVE":
        raise InvalidScopeError("GetInferenceProfile did not return an ACTIVE profile")
    if response.get("type") != "SYSTEM_DEFINED":
        raise InvalidScopeError(
            "selected inference-profile ARN did not resolve to a SYSTEM_DEFINED profile"
        )

    models = response.get("models")
    if isinstance(models, (str, bytes)) or not isinstance(models, Sequence) or not models:
        raise InvalidScopeError("GetInferenceProfile returned no destination models")
    resources: list[str] = []
    for index, model in enumerate(models):
        if not isinstance(model, Mapping) or not isinstance(model.get("modelArn"), str):
            raise InvalidScopeError(f"GetInferenceProfile models[{index}] has no exact modelArn")
        resources.append(model["modelArn"])

    invocation = BedrockInvocation(selected.arn, (selected.arn, *resources))
    return invocation.supporting_foundation_models


def load_bedrock_profile_resources(client: Any, profile_arn: str) -> tuple[str, ...]:
    """Resolve exact profile destinations from the selected source Region."""
    try:
        response = client.get_inference_profile(inferenceProfileIdentifier=profile_arn)
    except Exception as error:
        raise InvalidScopeError(
            f"unable to resolve inference profile {profile_arn!r}: {error}"
        ) from error
    return bedrock_profile_resources_from_response(profile_arn, response)


def parse_bedrock_inventory(value: object) -> tuple[str, ...]:
    """Validate and canonicalize a nonempty base-role Bedrock ARN inventory.

    The inventory is an operator-managed superset, not a per-Scene routing
    declaration. Exact profile destinations are verified when a Scene compiles.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise InvalidScopeError("Bedrock model inventory must contain at least one exact ARN")
    if not all(isinstance(resource, str) for resource in value):
        raise InvalidScopeError("Bedrock model inventory entries must be exact ARN strings")

    parsed = tuple(
        parse_bedrock_resource(resource, label="Bedrock model inventory entry")
        for resource in value
    )
    arns = tuple(resource.arn for resource in parsed)
    if len(arns) != len(set(arns)):
        raise InvalidScopeError("Bedrock model inventory must not contain duplicate ARNs")

    profiles = tuple(resource for resource in parsed if resource.kind == "inference-profile")
    foundations = tuple(resource for resource in parsed if resource.kind == "foundation-model")
    for profile in profiles:
        supports = tuple(
            foundation for foundation in foundations if _profile_supports(profile, foundation)
        )
        try:
            _validate_profile_supports(
                profile, supports, label=f"inference profile {profile.arn!r}"
            )
        except InvalidScopeError as error:
            raise InvalidScopeError(str(error)) from error
    for foundation in foundations:
        if foundation.region is None and not any(
            _profile_supports(profile, foundation) for profile in profiles
        ):
            raise InvalidScopeError(
                f"regionless foundation-model support {foundation.arn!r} has no matching "
                "global inference profile in the inventory"
            )
    return tuple(sorted(arns))


def bedrock_inventory_region_mismatches(
    inventory: Sequence[str], deployment_region: str
) -> tuple[str, ...]:
    """Return resources incompatible with one deployment/source region.

    Profiles and directly invocable foundation models must name the deployment
    region. A regional or regionless foundation resource can differ only when it
    has the shape of support for an admitted profile in that deployment Region.
    Per-Scene compilation separately verifies exact destination membership.
    """
    parsed = tuple(parse_bedrock_resource(resource) for resource in inventory)
    profiles = tuple(
        resource
        for resource in parsed
        if resource.kind == "inference-profile" and resource.region == deployment_region
    )
    mismatches: list[str] = []
    for resource in parsed:
        if resource.region == deployment_region:
            continue
        if resource.kind == "foundation-model" and any(
            _profile_supports(profile, resource) for profile in profiles
        ):
            continue
        mismatches.append(resource.arn)
    return tuple(sorted(mismatches))
