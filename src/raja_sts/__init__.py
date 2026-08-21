"""AWS-native minimum-viable TAJ compiler and bounded Scene runner."""

from .bedrock import BedrockInvocation, BedrockProfileLoader, load_bedrock_profile_resources
from .errors import (
    AuthorityModeError,
    DomainInvariantError,
    HandlerContractError,
    InputIntegrityError,
    InvalidAssumeRoleRequest,
    InvalidManifestURI,
    InvalidScopeError,
    InvariantFinding,
    ManifestLoadError,
    MintError,
    PolicySizeError,
    PublicationConflictError,
    PublicationError,
    RajaStsError,
    WorkflowContractError,
)
from .models import CompiledPolicy, ManifestObject, OutputSelector, ScopedCredentials
from .policy import compile
from .publication import (
    PublicationContext,
    PublicationInvariant,
    PublicationResult,
    PublicationStatus,
    publish_package,
)
from .runner import (
    GenesisPreflight,
    MaterializedInput,
    SceneContext,
    SceneHandler,
    SceneHandlerResult,
    SceneRunResult,
    preflight_genesis,
    run_scene,
    run_scene_from_genesis,
)
from .sealed import (
    SEALED_HANDLER_KEY,
    SealedHandler,
    SealedSource,
    load_sealed_handler,
    verify_sealed_handler,
)
from .sts import mint, mint_compiled
from .workflows import resolve_workflow_stamp

__all__ = [
    "SEALED_HANDLER_KEY",
    "AuthorityModeError",
    "BedrockInvocation",
    "BedrockProfileLoader",
    "CompiledPolicy",
    "DomainInvariantError",
    "GenesisPreflight",
    "HandlerContractError",
    "InputIntegrityError",
    "InvalidAssumeRoleRequest",
    "InvalidManifestURI",
    "InvalidScopeError",
    "InvariantFinding",
    "ManifestLoadError",
    "ManifestObject",
    "MaterializedInput",
    "MintError",
    "OutputSelector",
    "PolicySizeError",
    "PublicationConflictError",
    "PublicationContext",
    "PublicationError",
    "PublicationInvariant",
    "PublicationResult",
    "PublicationStatus",
    "RajaStsError",
    "SceneContext",
    "SceneHandler",
    "SceneHandlerResult",
    "SceneRunResult",
    "ScopedCredentials",
    "SealedHandler",
    "SealedSource",
    "WorkflowContractError",
    "compile",
    "load_bedrock_profile_resources",
    "load_sealed_handler",
    "mint",
    "mint_compiled",
    "preflight_genesis",
    "publish_package",
    "resolve_workflow_stamp",
    "run_scene",
    "run_scene_from_genesis",
    "verify_sealed_handler",
]

__version__ = "0.1.4"
