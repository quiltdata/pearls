"""AWS-native minimum-viable TAJ compiler."""

from .errors import (
    InvalidAssumeRoleRequest,
    InvalidManifestURI,
    InvalidScopeError,
    ManifestLoadError,
    MintError,
    PolicySizeError,
    RajaStsError,
)
from .models import CompiledPolicy, ManifestObject, ScopedCredentials
from .policy import compile
from .sts import mint, mint_compiled

__all__ = [
    "CompiledPolicy",
    "InvalidAssumeRoleRequest",
    "InvalidManifestURI",
    "InvalidScopeError",
    "ManifestLoadError",
    "ManifestObject",
    "MintError",
    "PolicySizeError",
    "RajaStsError",
    "ScopedCredentials",
    "compile",
    "mint",
    "mint_compiled",
]

__version__ = "0.1.0"
