"""Conventions shared by every ``pearls`` command group.

One skeleton keeps privilege boundaries legible: ``genesis`` commands use the
operator's caller-selected identity to author immutable packages, ``scene``
commands operate with bounded short-lived Scene authority, and ``infra`` commands
mutate persistent IAM with administrator authority. Commands resolve credentials
through boto3, separate human and JSON output, and never emit credential material.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import boto3

from .. import __version__
from ..authoring import (
    GenesisAuthoringPlan,
    GenesisAuthoringResult,
    prepare_genesis_authoring,
    publish_genesis_authoring,
)
from ..errors import PearlsError
from ..manifest import ManifestLoader
from ..runner import (
    GenesisPreflight,
    SceneRunResult,
    preflight_genesis,
    run_scene_from_genesis,
)
from ..sealed import SealedSource, verify_sealed_handler

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

_SCHEMA_ROOT = "https://quiltdata.com/schemas/pearls"
SCENE_CHECK_SCHEMA = f"{_SCHEMA_ROOT}/cli-scene-check/v2"
SCENE_RUN_SCHEMA = f"{_SCHEMA_ROOT}/cli-scene-run/v3"
GENESIS_AUTHOR_SCHEMA = f"{_SCHEMA_ROOT}/cli-genesis-author/v1"
INFRA_VALIDATE_SCHEMA = f"{_SCHEMA_ROOT}/cli-infra-validate/v2"
INFRA_PLAN_SCHEMA = f"{_SCHEMA_ROOT}/cli-infra-plan/v2"
INFRA_DEPLOY_SCHEMA = f"{_SCHEMA_ROOT}/cli-infra-deploy/v2"
INFRA_STATUS_SCHEMA = f"{_SCHEMA_ROOT}/cli-infra-status/v2"
INFRA_ASSUME_POLICY_SCHEMA = f"{_SCHEMA_ROOT}/cli-infra-assume-policy/v1"


class CliRefusal(PearlsError):
    """The command refused to act on the arguments or the operator's answer."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Runtime:
    """Injectable process boundary, so every command is testable offline.

    Defaults are the real ones. Tests replace ``session_factory`` and the runner
    hooks with fakes, so no test performs an AWS call.
    """

    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    #: Builds the session for calls made with the operator's own identity:
    #: genesis authoring/control reads, AssumeRole, CloudFormation, and IAM.
    session_factory: Callable[..., Any] = boto3.Session
    #: Builds the Scene session from minted credentials. Deliberately separate:
    #: --profile and --region never apply to it, so worker authority comes only
    #: from the minted credential and the selected model/profile ARN's region.
    worker_session_factory: Callable[..., Any] = boto3.Session
    prompt: Callable[[str], str] = input
    clock: Callable[[], datetime] = _utcnow
    sleep: Callable[[float], None] = time.sleep
    manifest_loader: ManifestLoader | None = None
    prepare_genesis_authoring: Callable[..., GenesisAuthoringPlan] = prepare_genesis_authoring
    publish_genesis_authoring: Callable[..., GenesisAuthoringResult] = publish_genesis_authoring
    preflight_genesis: Callable[..., GenesisPreflight] = preflight_genesis
    verify_sealed_handler: Callable[..., SealedSource] = verify_sealed_handler
    run_scene_from_genesis: Callable[..., SceneRunResult] = run_scene_from_genesis


@dataclass
class Emitter:
    """Stdout/stderr split with a single JSON switch.

    Diagnostics always go to stderr so that ``--json`` stdout stays parseable.
    """

    runtime: Runtime
    json_output: bool

    def note(self, text: str) -> None:
        """Write one human diagnostic line to stderr."""
        print(text, file=self.runtime.stderr)

    def line(self, text: str) -> None:
        """Write one human result line to stdout, unless JSON was requested."""
        if not self.json_output:
            print(text, file=self.runtime.stdout)

    def review(self, text: str) -> None:
        """Write one line of material the operator must read before confirming.

        Under ``--json`` this goes to stderr rather than being suppressed, so an
        interactive confirmation is never asked without showing what is being
        approved, while stdout stays a single parseable document.
        """
        print(text, file=self.runtime.stderr if self.json_output else self.runtime.stdout)

    def document(self, payload: Mapping[str, Any]) -> None:
        """Write the stable JSON document to stdout when JSON was requested."""
        if self.json_output:
            json.dump(payload, self.runtime.stdout, indent=2, sort_keys=True, allow_nan=False)
            print(file=self.runtime.stdout)


def add_aws_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the credential and output conventions shared by all groups."""
    parser.add_argument("--profile", help="named AWS profile resolved by boto3")
    parser.add_argument("--region", help="AWS region for the calls this command makes")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a stable, versioned JSON document on stdout",
    )


def add_confirmation_argument(parser: argparse.ArgumentParser) -> None:
    """Add ``--yes`` to a mutating verb."""
    parser.add_argument(
        "--yes",
        action="store_true",
        dest="assume_yes",
        help="skip the interactive confirmation for this mutating command",
    )


def build_session(runtime: Runtime, args: argparse.Namespace) -> Any:
    """Return the boto3 session for the calls this command makes directly."""
    kwargs: dict[str, Any] = {}
    if args.profile:
        kwargs["profile_name"] = args.profile
    if args.region:
        kwargs["region_name"] = args.region
    return runtime.session_factory(**kwargs)


def confirm(
    runtime: Runtime,
    *,
    question: str,
    assume_yes: bool,
) -> None:
    """Require an explicit ``yes`` before a mutating action.

    Raises :class:`CliRefusal` when the operator declines or when stdin cannot
    answer, so an unattended invocation without ``--yes`` fails closed.
    """
    if assume_yes:
        return
    print(f"{question} [type 'yes' to continue]: ", end="", file=runtime.stderr)
    try:
        answer = runtime.prompt("")
    except (EOFError, KeyboardInterrupt) as error:
        raise CliRefusal("no confirmation was given on stdin; re-run with --yes") from error
    if answer.strip().lower() != "yes":
        raise CliRefusal("confirmation declined; nothing was changed")


def read_json_argument(value: str, *, label: str) -> dict[str, Any]:
    """Parse a JSON object given inline or as ``@path`` to a UTF-8 JSON file."""
    if value.startswith("@"):
        path = Path(value[1:])
        if not path.is_file():
            raise CliRefusal(f"{label} file does not exist: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise CliRefusal(f"{label} file is not readable UTF-8 text: {error}") from error
    else:
        text = value

    def reject_constant(name: str) -> Any:
        # json.loads accepts NaN/Infinity/-Infinity by default, but publication
        # serializes with allow_nan=False. Rejecting them here keeps a preflight
        # honest instead of deferring the failure until after the Scene has run.
        raise CliRefusal(
            f"{label} must be standard JSON; {name} is not a valid value and would be "
            "rejected at publication"
        )

    try:
        parsed = json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        raise CliRefusal(f"{label} must be a JSON object: {error}") from error
    if not isinstance(parsed, dict):
        raise CliRefusal(f"{label} must be a JSON object, not {type(parsed).__name__}")
    return parsed


def positive_int(value: str) -> int:
    """Parse a strictly positive integer argument."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return parsed


def iso8601(value: datetime) -> str:
    """Render one timezone-aware timestamp in the record format used elsewhere."""
    if value.tzinfo is None:
        raise CliRefusal("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def envelope(schema: str, status: str) -> dict[str, Any]:
    """Start a versioned JSON document with the fields every command shares."""
    return {"$schema": schema, "status": status, "pearls_version": __version__}


def render_table(
    emitter: Emitter,
    rows: Sequence[tuple[str, str]],
    *,
    review: bool = False,
) -> None:
    """Print aligned ``label: value`` rows.

    Set ``review`` for material that must survive ``--json`` because the operator
    is about to be asked to approve it.
    """
    if not rows:
        return
    write = emitter.review if review else emitter.line
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        write(f"{label.ljust(width)}  {value}")
