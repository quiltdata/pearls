"""AWS-native minimum-viable TAJ compiler and bounded Scene runner."""

from .errors import (
    AuthorityModeError,
    HandlerContractError,
    InputIntegrityError,
    InvalidAssumeRoleRequest,
    InvalidManifestURI,
    InvalidScopeError,
    ManifestLoadError,
    MintError,
    PolicySizeError,
    PublicationError,
    RajaStsError,
    WorkflowContractError,
)
from .models import CompiledPolicy, ManifestObject, OutputSelector, ScopedCredentials
from .policy import compile
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
    "CompiledPolicy",
    "GenesisPreflight",
    "HandlerContractError",
    "InputIntegrityError",
    "InvalidAssumeRoleRequest",
    "InvalidManifestURI",
    "InvalidScopeError",
    "ManifestLoadError",
    "ManifestObject",
    "MaterializedInput",
    "MintError",
    "OutputSelector",
    "PolicySizeError",
    "PublicationError",
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
    "load_sealed_handler",
    "mint",
    "mint_compiled",
    "preflight_genesis",
    "resolve_workflow_stamp",
    "run_scene",
    "run_scene_from_genesis",
    "verify_sealed_handler",
]

__version__ = "0.1.4"
