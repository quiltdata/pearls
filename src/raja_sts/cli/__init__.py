"""The three explicit ``pearls`` authority planes.

``pearls genesis`` authors packages with the selected operator identity,
``pearls scene`` runs bounded work with short-lived Scene authority, and
``pearls infra`` administers persistent execution roles. Keeping these groups
separate makes each trust and mutation boundary visible.
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
