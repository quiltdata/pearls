"""Public exception hierarchy for :mod:`raja_sts`."""


class RajaStsError(Exception):
    """Base class for all expected RAJA STS failures."""


class InvalidManifestURI(RajaStsError, ValueError):
    """The manifest URI is malformed or does not identify an immutable revision."""


class ManifestLoadError(RajaStsError):
    """The pinned Quilt manifest could not be loaded or verified."""


class InvalidScopeError(RajaStsError, ValueError):
    """The logical read, model, or output scope is invalid."""


class PolicySizeError(RajaStsError):
    """The compiled inline session policy exceeds the configured STS limit."""


class InvalidAssumeRoleRequest(RajaStsError, ValueError):
    """The role, session name, or duration cannot be sent safely to STS."""


class MintError(RajaStsError):
    """STS rejected the request or returned an invalid credential response."""


class AuthorityModeError(RajaStsError):
    """A Scene runner request does not have exact, version-pinned authority."""


class InputIntegrityError(RajaStsError):
    """A selected input could not be read or did not match its sealed digest."""


class HandlerContractError(RajaStsError, ValueError):
    """A Scene handler returned unsafe objects or non-serializable metadata."""


class PublicationError(RajaStsError):
    """The bounded output package could not be published."""


class WorkflowContractError(RajaStsError):
    """The output package does not satisfy the destination registry's workflow contract."""
