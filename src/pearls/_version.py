"""Resolve the version of the Pearls code that is actually executing.

``pyproject.toml`` is the single declaration of the version, so the value is
never restated in code: a second declaration drifts, and this one is reported by
``pearls --version`` and stamped into every JSON receipt as ``pearls_version``.

Installed distribution metadata is the primary source, but it describes the
distribution, not necessarily the module that got imported. A source checkout on
``sys.path`` shadows a differently versioned installed distribution, and an
editable install's metadata freezes at install time, so in both cases the
metadata can name code that is not running. This module therefore trusts
metadata only when the installed distribution actually contains this file, falls
back to the checkout's own ``pyproject.toml``, and refuses to guess otherwise.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import Distribution, PackageNotFoundError
from pathlib import Path

DISTRIBUTION_NAME = "pearls"
UNKNOWN_VERSION = "0.0.0+unknown"


def _installed_version(module_path: Path) -> str | None:
    """Return the installed version only if it describes ``module_path``."""
    try:
        distribution = Distribution.from_name(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return None
    try:
        # The installed package directory, compared against the directory this
        # module was imported from. Comparing directories rather than file names
        # keeps the check independent of which module inside the package asks.
        located = distribution.locate_file(DISTRIBUTION_NAME)
        installed_path = Path(str(located)).resolve()
    except (OSError, ValueError):
        return None
    if installed_path != module_path.parent:
        return None
    version = distribution.version
    return version if isinstance(version, str) and version else None


def _source_tree_version(module_path: Path) -> str | None:
    """Return the version declared by the checkout that contains this module."""
    # src/pearls/_version.py -> src/pearls -> src -> the checkout root.
    pyproject = module_path.parent.parent.parent / "pyproject.toml"
    try:
        raw = pyproject.read_bytes()
    except OSError:
        return None
    try:
        project = tomllib.loads(raw.decode("utf-8")).get("project", {})
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(project, dict) or project.get("name") != DISTRIBUTION_NAME:
        return None
    version = project.get("version")
    return version if isinstance(version, str) and version else None


def resolve_version(module_path: Path | None = None) -> str:
    """Report the version of the code at ``module_path``, defaulting to this one.

    Returns :data:`UNKNOWN_VERSION` rather than a version belonging to different
    code, so a receipt never claims provenance that cannot be established.
    """
    resolved = (module_path or Path(__file__)).resolve()
    installed = _installed_version(resolved)
    if installed is not None:
        return installed
    declared = _source_tree_version(resolved)
    return declared if declared is not None else UNKNOWN_VERSION
