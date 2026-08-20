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
)
from .models import CompiledPolicy, ManifestObject, OutputSelector, ScopedCredentials
from .policy import compile
from .runner import (
    MaterializedInput,
    SceneContext,
    SceneHandler,
    SceneHandlerResult,
    SceneRunResult,
    run_scene,
    run_scene_from_genesis,
)
from .sts import mint, mint_compiled

__all__ = [
    "AuthorityModeError",
    "CompiledPolicy",
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
    "compile",
    "mint",
    "mint_compiled",
    "run_scene",
    "run_scene_from_genesis",
]

__version__ = "0.1.2"
