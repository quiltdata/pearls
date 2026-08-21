"""Shared validation for immutable Genesis package controls."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import keyword
import platform
from collections.abc import Callable, Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .errors import InvalidScopeError, ManifestLoadError, RuntimeRequirementsError
from .manifest import parse_output_selectors
from .models import OutputSelector

README_KEY = "README.md"
OUTPUTS_KEY = "outputs.json"
SEALED_HANDLER_KEY = "protocol/handler.py"
RUNTIME_REQUIREMENTS_KEY = "protocol/requirements.json"
RUNTIME_REQUIREMENTS_SCHEMA = (
    "https://quiltdata.com/schemas/raja-sts/handler-runtime-requirements/v1"
)
INPUT_PREFIX = "input/"

# These limits bound parse/probe work, diagnostics, and receipt growth for an
# untrusted declaration inspected with the bootstrap identity.
RUNTIME_REQUIREMENTS_MAX_BYTES = 65_536
RUNTIME_REQUIREMENTS_MAX_IMPORTS = 128
RUNTIME_REQUIREMENTS_MAX_PYTHON_CHARACTERS = 256
RUNTIME_REQUIREMENTS_MAX_MODULE_CHARACTERS = 128
RUNTIME_REQUIREMENTS_MAX_REQUIREMENT_CHARACTERS = 1_024
RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS = 256
RUNTIME_REQUIREMENTS_MAX_DIAGNOSTIC_FINDINGS = 20
RUNTIME_REQUIREMENTS_MAX_DIAGNOSTIC_CHARACTERS = 512

RUNTIME_REQUIREMENTS_LIMITATIONS: tuple[str, ...] = (
    "Distribution-version and module-resolution checks are independent; preflight "
    "does not prove that the named distribution supplied the resolved module.",
    "Module resolution does not execute the declared module body and does not prove "
    "import-time Python or native initialization.",
    "Preflight does not install or resolve packages, verify undeclared transitive "
    "requirements, validate ABIs, lock the environment, or establish reproducibility.",
)

RuntimeRequirementApplicability = Literal["applicable", "not-applicable"]
DistributionVersionStatus = Literal["compatible", "not-checked"]
ModuleResolutionStatus = Literal["resolvable", "not-checked"]
ModuleFinder = Callable[[str], object | None]
DistributionVersionLoader = Callable[[str], str]


def _validate_python_specifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeRequirementsError(f"{label} must be a nonblank specifier")
    normalized = value.strip()
    if len(normalized) > RUNTIME_REQUIREMENTS_MAX_PYTHON_CHARACTERS:
        raise RuntimeRequirementsError(
            f"{label} exceeds {RUNTIME_REQUIREMENTS_MAX_PYTHON_CHARACTERS} characters"
        )
    try:
        SpecifierSet(normalized)
    except InvalidSpecifier as error:
        raise RuntimeRequirementsError(
            f"{label} has an invalid version specifier: {error}"
        ) from error
    return normalized


def _validate_import_parts(
    module: object,
    raw_requirement: object,
    *,
    label: str,
) -> tuple[str, str, str, str | None, str | None]:
    if not isinstance(module, str):
        raise RuntimeRequirementsError(
            f"{label}.module must be one top-level Python import identifier"
        )
    if len(module) > RUNTIME_REQUIREMENTS_MAX_MODULE_CHARACTERS:
        raise RuntimeRequirementsError(
            f"{label}.module exceeds {RUNTIME_REQUIREMENTS_MAX_MODULE_CHARACTERS} characters"
        )
    if not module.isidentifier() or keyword.iskeyword(module):
        raise RuntimeRequirementsError(
            f"{label}.module must be one top-level Python import identifier"
        )

    if not isinstance(raw_requirement, str) or not raw_requirement.strip():
        raise RuntimeRequirementsError(f"{label}.requirement must be a nonblank PEP 508 string")
    normalized = raw_requirement.strip()
    if len(normalized) > RUNTIME_REQUIREMENTS_MAX_REQUIREMENT_CHARACTERS:
        raise RuntimeRequirementsError(
            f"{label}.requirement exceeds "
            f"{RUNTIME_REQUIREMENTS_MAX_REQUIREMENT_CHARACTERS} characters"
        )
    try:
        parsed = Requirement(normalized)
    except InvalidRequirement as error:
        raise RuntimeRequirementsError(f"{label}.requirement is invalid: {error}") from error
    if parsed.url is not None:
        raise RuntimeRequirementsError(
            f"{label}.requirement must not use a direct URL; preflight does not install artifacts"
        )
    if parsed.extras:
        raise RuntimeRequirementsError(
            f"{label}.requirement must not use extras; declare importable distributions directly"
        )
    canonical = str(parsed)
    if len(canonical) > RUNTIME_REQUIREMENTS_MAX_REQUIREMENT_CHARACTERS:
        raise RuntimeRequirementsError(
            f"{label}.requirement canonical form exceeds "
            f"{RUNTIME_REQUIREMENTS_MAX_REQUIREMENT_CHARACTERS} characters"
        )
    marker = str(parsed.marker) if parsed.marker is not None else None
    specifier = str(parsed.specifier) or None
    return module, canonical, parsed.name, marker, specifier


@dataclass(frozen=True, slots=True)
class RuntimeImportRequirement:
    """One validated import name and its separately named PEP 508 distribution."""

    module: str
    requirement: str
    distribution: str = field(init=False)
    marker: str | None = field(init=False)
    specifier: str | None = field(init=False)

    def __post_init__(self) -> None:
        module, requirement, distribution, marker, specifier = _validate_import_parts(
            self.module,
            self.requirement,
            label="runtime import requirement",
        )
        object.__setattr__(self, "module", module)
        object.__setattr__(self, "requirement", requirement)
        object.__setattr__(self, "distribution", distribution)
        object.__setattr__(self, "marker", marker)
        object.__setattr__(self, "specifier", specifier)

    def report(self) -> dict[str, Any]:
        """Return derived declaration labels that callers cannot set independently."""
        return {
            "module": self.module,
            "requirement": self.requirement,
            "distribution": self.distribution,
            "marker": self.marker,
        }


@dataclass(frozen=True, slots=True)
class RuntimeRequirements:
    """Bounded sealed runtime declaration parsed without inspecting the environment."""

    python: str
    imports: tuple[RuntimeImportRequirement, ...]
    schema: str = field(init=False, default=RUNTIME_REQUIREMENTS_SCHEMA)

    def __post_init__(self) -> None:
        python = _validate_python_specifier(
            self.python,
            label="runtime requirements python",
        )
        if not isinstance(self.imports, Sequence) or isinstance(self.imports, (str, bytes)):
            raise RuntimeRequirementsError("runtime requirements imports must be a sequence")
        if len(self.imports) > RUNTIME_REQUIREMENTS_MAX_IMPORTS:
            raise RuntimeRequirementsError(
                f"runtime requirements imports exceeds {RUNTIME_REQUIREMENTS_MAX_IMPORTS} entries"
            )
        imports: list[RuntimeImportRequirement] = []
        seen_modules: set[str] = set()
        for index, item in enumerate(self.imports):
            if not isinstance(item, RuntimeImportRequirement):
                raise RuntimeRequirementsError(
                    f"runtime requirements imports[{index}] is not a validated declaration"
                )
            canonical = RuntimeImportRequirement(item.module, item.requirement)
            if canonical.module in seen_modules:
                raise RuntimeRequirementsError(
                    f"runtime requirements duplicate module {canonical.module!r}"
                )
            seen_modules.add(canonical.module)
            imports.append(canonical)
        object.__setattr__(self, "python", python)
        object.__setattr__(self, "imports", tuple(sorted(imports, key=lambda item: item.module)))

    def report(self) -> dict[str, Any]:
        """Return the stable declaration shape with only derived labels."""
        return {
            "schema": self.schema,
            "python": self.python,
            "imports": [item.report() for item in self.imports],
        }


@dataclass(frozen=True, slots=True, init=False)
class RuntimeRequirementCheck:
    """Independent local probe outcomes created only by the verifier."""

    requirement: RuntimeImportRequirement
    applicability: RuntimeRequirementApplicability
    installed_version: str | None
    distribution_version_status: DistributionVersionStatus
    module_resolution_status: ModuleResolutionStatus

    def __init__(self, *_: object, **__: object) -> None:
        raise TypeError("RuntimeRequirementCheck values are created by verify_runtime_requirements")

    @classmethod
    def _verified(
        cls,
        requirement: RuntimeImportRequirement,
        *,
        applicability: RuntimeRequirementApplicability,
        installed_version: str | None,
        distribution_version_status: DistributionVersionStatus,
        module_resolution_status: ModuleResolutionStatus,
    ) -> RuntimeRequirementCheck:
        if applicability == "not-applicable":
            if (
                installed_version is not None
                or distribution_version_status != "not-checked"
                or module_resolution_status != "not-checked"
            ):
                raise RuntimeRequirementsError(
                    "A marker-skipped requirement cannot carry local probe results"
                )
        elif (
            not isinstance(installed_version, str)
            or not installed_version
            or distribution_version_status != "compatible"
            or module_resolution_status != "resolvable"
        ):
            raise RuntimeRequirementsError(
                "An applicable requirement report must carry both successful probe outcomes"
            )
        value = object.__new__(cls)
        object.__setattr__(value, "requirement", requirement)
        object.__setattr__(value, "applicability", applicability)
        object.__setattr__(value, "installed_version", installed_version)
        object.__setattr__(value, "distribution_version_status", distribution_version_status)
        object.__setattr__(value, "module_resolution_status", module_resolution_status)
        return value

    def report(self) -> dict[str, Any]:
        """Return separate applicability, distribution, and module outcomes."""
        return {
            "requirement": self.requirement.requirement,
            "marker": self.requirement.marker,
            "applicability": self.applicability,
            "distribution_version": {
                "name": self.requirement.distribution,
                "specifier": self.requirement.specifier,
                "installed": self.installed_version,
                "status": self.distribution_version_status,
            },
            "module_resolution": {
                "name": self.requirement.module,
                "status": self.module_resolution_status,
            },
        }


@dataclass(frozen=True, slots=True, init=False)
class RuntimeRequirementsReport:
    """Successful compatibility evidence created only by the verifier."""

    declaration: RuntimeRequirements
    actual_python: str
    checks: tuple[RuntimeRequirementCheck, ...]

    def __init__(self, *_: object, **__: object) -> None:
        raise TypeError(
            "RuntimeRequirementsReport values are created by verify_runtime_requirements"
        )

    @classmethod
    def _verified(
        cls,
        declaration: RuntimeRequirements,
        *,
        actual_python: str,
        checks: Sequence[RuntimeRequirementCheck],
    ) -> RuntimeRequirementsReport:
        ordered = tuple(checks)
        if tuple(check.requirement for check in ordered) != declaration.imports:
            raise RuntimeRequirementsError(
                "Runtime requirement checks do not match the canonical declaration"
            )
        value = object.__new__(cls)
        object.__setattr__(value, "declaration", declaration)
        object.__setattr__(value, "actual_python", actual_python)
        object.__setattr__(value, "checks", ordered)
        return value

    @property
    def applicable_count(self) -> int:
        """Return how many declarations were probed on this environment."""
        return sum(check.applicability == "applicable" for check in self.checks)

    @property
    def not_applicable_count(self) -> int:
        """Return how many declarations were skipped by a false marker."""
        return len(self.checks) - self.applicable_count

    def report(self) -> dict[str, Any]:
        """Return bounded evidence without unrelated environment inventory."""
        return {
            "schema": self.declaration.schema,
            "python": {
                "required": self.declaration.python,
                "actual": self.actual_python,
                "status": "compatible",
            },
            "summary": {
                "declared": len(self.checks),
                "applicable": self.applicable_count,
                "not_applicable": self.not_applicable_count,
            },
            "imports": [check.report() for check in self.checks],
            "limitations": list(RUNTIME_REQUIREMENTS_LIMITATIONS),
        }


def validate_genesis_layout_keys(
    logical_keys: Iterable[str],
    *,
    require_handler: bool,
) -> tuple[str, ...]:
    """Validate the logical-key shape shared by authored and executed Genesis packages.

    Runtime preflight may receive a handler callable directly, so only operator
    conventions require the default handler key. Every Genesis package must make
    runtime requirements explicit; an empty declaration is valid.
    """
    keys = tuple(logical_keys)
    unique = set(keys)
    if len(unique) != len(keys):
        seen: set[str] = set()
        for key in keys:
            if key in seen:
                raise ManifestLoadError(f"Genesis package contains duplicate logical key: {key!r}")
            seen.add(key)
        raise AssertionError("duplicate logical key was not found")

    required = {README_KEY, OUTPUTS_KEY, RUNTIME_REQUIREMENTS_KEY}
    if require_handler:
        required.add(SEALED_HANDLER_KEY)
    missing = required - unique
    if missing:
        raise ManifestLoadError(
            "Genesis package is missing required control object(s): " + ", ".join(sorted(missing))
        )
    if not any(key.startswith(INPUT_PREFIX) and key != INPUT_PREFIX for key in keys):
        raise ManifestLoadError("Genesis package must contain at least one input/ object")
    return tuple(sorted(keys))


def decode_genesis_readme(content: bytes) -> str:
    """Decode the Genesis control prompt as UTF-8 without interpreting it."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManifestLoadError("Genesis README.md must be UTF-8 text") from error


def parse_genesis_outputs(content: bytes) -> tuple[dict[str, object], tuple[OutputSelector, ...]]:
    """Parse and validate the Genesis output-selector control document."""
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidScopeError(
            f"outputs.json must contain a UTF-8 JSON object: {error}"
        ) from error
    if not isinstance(value, dict):
        raise InvalidScopeError("outputs.json must contain a URI-to-description object")
    writable: dict[str, object] = value
    return writable, parse_output_selectors(writable)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise RuntimeRequirementsError(
                f"{RUNTIME_REQUIREMENTS_KEY} contains duplicate JSON key {key!r}"
            )
        value[key] = child
    return value


def _require_exact_fields(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise RuntimeRequirementsError(
            f"{label} is missing required field(s): " + ", ".join(sorted(missing))
        )
    if unknown:
        raise RuntimeRequirementsError(
            f"{label} contains unknown field(s): " + ", ".join(sorted(unknown))
        )


def parse_runtime_requirements(content: bytes) -> RuntimeRequirements:
    """Parse one strictly bounded sealed declaration without probing this machine."""
    if not isinstance(content, bytes):
        raise RuntimeRequirementsError(f"{RUNTIME_REQUIREMENTS_KEY} content must be bytes")
    if len(content) > RUNTIME_REQUIREMENTS_MAX_BYTES:
        raise RuntimeRequirementsError(
            f"{RUNTIME_REQUIREMENTS_KEY} exceeds {RUNTIME_REQUIREMENTS_MAX_BYTES} bytes"
        )
    try:
        text = content.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_unique_json_object)
    except RuntimeRequirementsError:
        raise
    except (ValueError, RecursionError) as error:
        raise RuntimeRequirementsError(
            f"{RUNTIME_REQUIREMENTS_KEY} must contain a UTF-8 JSON object: {error}"
        ) from error
    if not isinstance(value, dict):
        raise RuntimeRequirementsError(f"{RUNTIME_REQUIREMENTS_KEY} must contain a JSON object")
    _require_exact_fields(
        value,
        {"$schema", "python", "imports"},
        label=RUNTIME_REQUIREMENTS_KEY,
    )

    if value["$schema"] != RUNTIME_REQUIREMENTS_SCHEMA:
        raise RuntimeRequirementsError(
            f"{RUNTIME_REQUIREMENTS_KEY} must use schema {RUNTIME_REQUIREMENTS_SCHEMA!r}"
        )
    python_specifier = _validate_python_specifier(
        value["python"],
        label="runtime requirements python",
    )

    raw_imports = value["imports"]
    if not isinstance(raw_imports, list):
        raise RuntimeRequirementsError("runtime requirements imports must be an array")
    if len(raw_imports) > RUNTIME_REQUIREMENTS_MAX_IMPORTS:
        raise RuntimeRequirementsError(
            f"runtime requirements imports exceeds {RUNTIME_REQUIREMENTS_MAX_IMPORTS} entries"
        )
    imports: list[RuntimeImportRequirement] = []
    seen_modules: set[str] = set()
    for index, raw_item in enumerate(raw_imports):
        label = f"runtime requirements imports[{index}]"
        if not isinstance(raw_item, dict):
            raise RuntimeRequirementsError(f"{label} must be an object")
        _require_exact_fields(raw_item, {"module", "requirement"}, label=label)
        module, requirement, _distribution, _marker, _specifier = _validate_import_parts(
            raw_item["module"],
            raw_item["requirement"],
            label=label,
        )
        if module in seen_modules:
            raise RuntimeRequirementsError(f"runtime requirements duplicate module {module!r}")
        seen_modules.add(module)
        imports.append(RuntimeImportRequirement(module, requirement))

    return RuntimeRequirements(python=python_specifier, imports=tuple(imports))


def _canonical_declaration(declaration: RuntimeRequirements) -> RuntimeRequirements:
    if not isinstance(declaration, RuntimeRequirements):
        raise RuntimeRequirementsError(
            "verify_runtime_requirements requires a RuntimeRequirements declaration"
        )
    try:
        imports = tuple(
            RuntimeImportRequirement(item.module, item.requirement) for item in declaration.imports
        )
        return RuntimeRequirements(python=declaration.python, imports=imports)
    except RuntimeRequirementsError:
        raise
    except Exception as error:
        raise RuntimeRequirementsError(
            f"Runtime requirements declaration is invalid: {type(error).__name__}: {error}"
        ) from error


def _bounded_error(error: Exception) -> str:
    detail = str(error)
    if len(detail) > RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS:
        detail = detail[: RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS - 3] + "..."
    return f"{type(error).__name__}: {detail}"


def verify_runtime_requirements(
    declaration: RuntimeRequirements,
    *,
    python_version: str | None = None,
    module_finder: ModuleFinder = importlib.util.find_spec,
    distribution_version: DistributionVersionLoader = importlib.metadata.version,
    marker_environment: Mapping[str, str] | None = None,
) -> RuntimeRequirementsReport:
    """Verify bounded local metadata and resolvability without importing modules.

    This performs no installation and executes no declared dependency or handler
    module body. Distribution metadata and module resolution are independent
    probes; successful evidence does not claim that one supplied the other.
    """
    canonical = _canonical_declaration(declaration)
    actual_python = python_version or platform.python_version()
    if not isinstance(actual_python, str) or not actual_python:
        raise RuntimeRequirementsError("Current Python version must be a nonblank string")
    if len(actual_python) > RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS:
        raise RuntimeRequirementsError(
            "Current Python version exceeds "
            f"{RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS} characters"
        )
    try:
        parsed_python = Version(actual_python)
    except InvalidVersion as error:
        raise RuntimeRequirementsError(
            f"Current Python version {actual_python!r} is not PEP 440 compatible"
        ) from error

    findings: list[str] = []
    finding_count = 0

    def record_finding(message: str) -> None:
        nonlocal finding_count
        finding_count += 1
        if len(findings) >= RUNTIME_REQUIREMENTS_MAX_DIAGNOSTIC_FINDINGS:
            return
        if len(message) > RUNTIME_REQUIREMENTS_MAX_DIAGNOSTIC_CHARACTERS:
            message = message[: RUNTIME_REQUIREMENTS_MAX_DIAGNOSTIC_CHARACTERS - 3] + "..."
        findings.append(message)

    if not SpecifierSet(canonical.python).contains(parsed_python, prereleases=True):
        record_finding(f"Python {actual_python} does not satisfy {canonical.python}")

    environment = cast(dict[str, str | Set[str]], default_environment())
    environment["python_full_version"] = str(parsed_python)
    environment["python_version"] = ".".join(str(part) for part in parsed_python.release[:2])
    if marker_environment is not None:
        if not isinstance(marker_environment, Mapping):
            raise RuntimeRequirementsError("marker_environment must be a mapping")
        if len(marker_environment) > 32:
            raise RuntimeRequirementsError("marker_environment exceeds 32 entries")
        for key, value in marker_environment.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or len(key) > 64
                or len(value) > RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS
            ):
                raise RuntimeRequirementsError(
                    "marker_environment keys and values must be bounded strings"
                )
            environment[key] = value

    checks: list[RuntimeRequirementCheck] = []
    for item in canonical.imports:
        requirement = Requirement(item.requirement)
        if requirement.marker is not None:
            try:
                applies = requirement.marker.evaluate(environment, context="requirement")
            except Exception as error:
                record_finding(
                    f"requirement {item.requirement!r} marker cannot be evaluated in the "
                    f"runtime requirement context: {_bounded_error(error)}"
                )
                continue
            if not applies:
                checks.append(
                    RuntimeRequirementCheck._verified(
                        item,
                        applicability="not-applicable",
                        installed_version=None,
                        distribution_version_status="not-checked",
                        module_resolution_status="not-checked",
                    )
                )
                continue

        findings_before = finding_count
        installed_version: str | None = None
        try:
            loaded_version = distribution_version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            record_finding(f"distribution {requirement.name!r} is not installed")
        except Exception as error:
            record_finding(
                f"distribution {requirement.name!r} version lookup failed: {_bounded_error(error)}"
            )
        else:
            if not isinstance(loaded_version, str) or not loaded_version:
                record_finding(
                    f"distribution {requirement.name!r} returned a nonblank-string "
                    "version requirement violation"
                )
            elif len(loaded_version) > RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS:
                record_finding(
                    f"distribution {requirement.name!r} installed version exceeds "
                    f"{RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS} characters"
                )
            else:
                installed_version = loaded_version
                try:
                    parsed_installed = Version(installed_version)
                except InvalidVersion:
                    record_finding(
                        f"distribution {requirement.name!r} has invalid installed version "
                        f"{installed_version!r}"
                    )
                else:
                    if requirement.specifier and not requirement.specifier.contains(
                        parsed_installed, prereleases=True
                    ):
                        record_finding(
                            f"distribution {requirement.name!r} version {installed_version} "
                            f"does not satisfy {requirement.specifier}"
                        )

        try:
            resolvable = module_finder(item.module) is not None
        except Exception as error:
            record_finding(f"module {item.module!r} resolution failed: {_bounded_error(error)}")
        else:
            if not resolvable:
                record_finding(f"module {item.module!r} is not resolvable")

        if finding_count == findings_before and installed_version is not None:
            checks.append(
                RuntimeRequirementCheck._verified(
                    item,
                    applicability="applicable",
                    installed_version=installed_version,
                    distribution_version_status="compatible",
                    module_resolution_status="resolvable",
                )
            )

    if finding_count:
        omitted = finding_count - len(findings)
        suffix = f"; {omitted} additional finding(s) omitted" if omitted else ""
        raise RuntimeRequirementsError(
            "Runtime requirements are not satisfied: " + "; ".join(findings) + suffix
        )
    return RuntimeRequirementsReport._verified(
        canonical,
        actual_python=actual_python,
        checks=checks,
    )
