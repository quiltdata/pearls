"""Public exception hierarchy for :mod:`pearls`."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InvariantFinding:
    """One caller-supplied domain-invariant refusal finding."""

    invariant: str
    message: str


class PearlsError(Exception):
    """Base class for all expected Pearls failures."""


class InvalidManifestURI(PearlsError, ValueError):
    """The manifest URI is malformed or does not identify an immutable revision."""


class ManifestLoadError(PearlsError):
    """The pinned Quilt manifest could not be loaded or verified."""


class InvalidScopeError(PearlsError, ValueError):
    """The logical read, model, or output scope is invalid."""


class PolicySizeError(PearlsError):
    """The compiled inline session policy exceeds the configured STS limit."""


class InvalidAssumeRoleRequest(PearlsError, ValueError):
    """The role, session name, or duration cannot be sent safely to STS."""


class MintError(PearlsError):
    """STS rejected the request or returned an invalid credential response."""


class AuthorityModeError(PearlsError):
    """A Scene runner request does not have exact, version-pinned authority."""


class InputIntegrityError(PearlsError):
    """A selected input could not be read or did not match its sealed digest."""


class HandlerContractError(PearlsError, ValueError):
    """A Scene handler returned unsafe objects or non-serializable metadata."""


class GenesisAuthoringError(PearlsError, ValueError):
    """A local genesis tree or authoring declaration is unsafe or ambiguous."""


class RuntimeRequirementsError(PearlsError, ValueError):
    """A sealed runtime declaration is malformed or incompatible with this interpreter."""


class PublicationError(PearlsError):
    """The bounded output package could not be published."""


class PublicationConflictError(PublicationError):
    """A publication revision identity was reused for different content."""


class WorkflowContractError(PearlsError):
    """The output package does not satisfy the destination registry's workflow contract."""


class DomainInvariantError(PearlsError):
    """Caller-supplied domain invariants refused an output before publication."""

    def __init__(self, findings: tuple[InvariantFinding, ...]) -> None:
        self.findings = findings
        detail = "; ".join(f"{finding.invariant}: {finding.message}" for finding in findings)
        super().__init__(f"Output package failed domain invariants: {detail}")
