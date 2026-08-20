"""The installed ``pearls`` command line.

``pearls scene`` runs one bounded Scene with short-lived Scene authority.
``pearls infra`` deploys and inspects the persistent Scene execution roles with
administrator authority. The split is deliberate: it keeps the privilege boundary
legible instead of blurring it behind one convenience command.
"""

from __future__ import annotations

import sys

from ._common import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    CliRefusal,
    Runtime,
)
from ._main import build_parser, main

__all__ = [
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_USAGE",
    "CliRefusal",
    "Runtime",
    "build_parser",
    "main",
    "run",
]


def run() -> None:
    """Console-script wrapper that exits with the command's status code."""
    sys.exit(main())
