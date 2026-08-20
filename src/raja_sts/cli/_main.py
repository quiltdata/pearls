"""The ``pearls`` console entry point and its single typed-error funnel."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from botocore.exceptions import BotoCoreError, ClientError

from .. import __version__
from ..errors import RajaStsError
from . import infra, scene
from ._common import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Runtime

_DESCRIPTION = """\
Operator commands for one sealed-genesis Scene run.

  scene   check | run                                  bounded Scene authority
  infra   validate | plan | deploy | status | assume-policy   administrator IAM

Read-only verbs never prompt. Mutating verbs require confirmation or --yes.
Human-readable output goes to stdout; --json emits a stable, versioned document.
Diagnostics go to stderr. No command prints credentials or bearer tokens.
"""


def build_parser() -> argparse.ArgumentParser:
    """Return the root ``pearls`` parser with both command groups registered."""
    parser = argparse.ArgumentParser(
        prog="pearls",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"pearls {__version__}")
    groups = parser.add_subparsers(dest="group", metavar="GROUP", required=True)
    scene.add_parser(groups)
    infra.add_parser(groups)
    return parser


def main(argv: Sequence[str] | None = None, *, runtime: Runtime | None = None) -> int:
    """Parse arguments, dispatch one command, and map failures to exit codes.

    Returns ``0`` on success, ``2`` for a usage error, and ``1`` for any typed
    refusal or execution failure. Refusals are reported on stderr as
    ``ErrorType: message`` so a caller can distinguish them without parsing
    stdout.
    """
    active = runtime if runtime is not None else Runtime()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:
        code = exit_request.code
        return code if isinstance(code, int) else EXIT_USAGE
    try:
        result = args.run(active, args)
    except RajaStsError as error:
        print(f"{type(error).__name__}: {error}", file=active.stderr)
        return EXIT_REFUSED
    except (BotoCoreError, ClientError, ValueError, OSError) as error:
        # Credential resolution, region resolution, and AWS API refusals are
        # expected operator conditions, not defects. Report them like any other
        # refusal instead of printing a traceback.
        print(f"{type(error).__name__}: {error}", file=active.stderr)
        return EXIT_REFUSED
    return result if isinstance(result, int) else EXIT_OK
