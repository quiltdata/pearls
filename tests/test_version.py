from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

import pytest

import pearls
from pearls import _version
from pearls._version import UNKNOWN_VERSION, resolve_version

PYPROJECT = """
[project]
name = "pearls"
version = "9.9.9"
"""


class FakeDistribution:
    """Stands in for the installed distribution's metadata."""

    def __init__(self, package_dir: Path | None, version: Any = "1.2.3") -> None:
        self._package_dir = package_dir
        self.version = version

    def locate_file(self, path: str) -> Path:
        assert path == "pearls"
        if self._package_dir is None:
            raise OSError("no location")
        return self._package_dir


def write_checkout(root: Path, body: str | bytes = PYPROJECT) -> Path:
    """Lay out src/pearls/_version.py under root and return that module path."""
    module = root / "src" / "pearls" / "_version.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    if isinstance(body, bytes):
        (root / "pyproject.toml").write_bytes(body)
    else:
        (root / "pyproject.toml").write_text(body)
    return module


def install_distribution(
    monkeypatch: pytest.MonkeyPatch, distribution: FakeDistribution | None
) -> None:
    def from_name(name: str) -> FakeDistribution:
        assert name == "pearls"
        if distribution is None:
            raise PackageNotFoundError(name)
        return distribution

    monkeypatch.setattr(_version.Distribution, "from_name", staticmethod(from_name))


def test_installed_metadata_wins_when_it_describes_this_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = write_checkout(tmp_path)
    install_distribution(monkeypatch, FakeDistribution(module.parent))

    # The declared 9.9.9 is ignored: the installed distribution owns this file.
    assert resolve_version(module) == "1.2.3"


def test_uninstalled_checkout_reports_its_own_declared_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = write_checkout(tmp_path)
    install_distribution(monkeypatch, None)

    assert resolve_version(module) == "9.9.9"


def test_shadowed_install_does_not_lend_its_version_to_other_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = write_checkout(tmp_path)
    elsewhere = tmp_path / "site-packages" / "pearls"
    install_distribution(monkeypatch, FakeDistribution(elsewhere))

    # 1.2.3 is installed but is not this code, so the checkout's version wins.
    assert resolve_version(module) == "9.9.9"


def test_unlocatable_install_falls_back_to_the_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = write_checkout(tmp_path)
    install_distribution(monkeypatch, FakeDistribution(None))

    assert resolve_version(module) == "9.9.9"


def test_blank_installed_version_is_not_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = write_checkout(tmp_path)
    install_distribution(monkeypatch, FakeDistribution(module.parent, version=""))

    assert resolve_version(module) == "9.9.9"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('[project]\nname = "other"\nversion = "9.9.9"\n', id="other-project"),
        pytest.param('[project]\nname = "pearls"\n', id="no-version"),
        pytest.param('[project]\nname = "pearls"\nversion = 9\n', id="non-string-version"),
        pytest.param('[project]\nname = "pearls"\nversion = ""\n', id="blank-version"),
        pytest.param("[project\n", id="malformed-toml"),
        pytest.param(b'[project]\nname = "pearls"\nversion = "\xff"\n', id="undecodable"),
    ],
)
def test_unidentifiable_source_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str | bytes
) -> None:
    module = write_checkout(tmp_path, body)
    install_distribution(monkeypatch, None)

    assert resolve_version(module) == UNKNOWN_VERSION


def test_missing_pyproject_reports_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = tmp_path / "src" / "pearls" / "_version.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    install_distribution(monkeypatch, None)

    assert resolve_version(module) == UNKNOWN_VERSION


def test_package_version_is_resolved_and_not_restated() -> None:
    # Whichever path applied, the reported version must identify running code.
    assert pearls.__version__ != UNKNOWN_VERSION
    assert pearls.__version__ == resolve_version(Path(_version.__file__))
