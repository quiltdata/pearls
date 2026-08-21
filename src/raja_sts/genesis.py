"""Shared, side-effect-free validation for genesis package controls and layout."""

from __future__ import annotations

import json
from collections.abc import Iterable

from .errors import InvalidScopeError, ManifestLoadError
from .manifest import parse_output_selectors
from .models import OutputSelector

README_KEY = "README.md"
OUTPUTS_KEY = "outputs.json"
SEALED_HANDLER_KEY = "protocol/handler.py"
INPUT_PREFIX = "input/"


def validate_genesis_layout_keys(
    logical_keys: Iterable[str],
    *,
    require_handler: bool,
) -> tuple[str, ...]:
    """Validate the logical-key shape shared by authored and executed genesis packages.

    Runtime preflight does not require the conventional sealed handler because the
    lower-level API may receive a handler directly. Operator authoring does require
    it, matching ``pearls scene``'s sealed-handler convention without changing that
    runtime boundary ahead of the separate runtime-requirements lane.
    """
    keys = tuple(logical_keys)
    unique = set(keys)
    if len(unique) != len(keys):
        seen: set[str] = set()
        for key in keys:
            if key in seen:
                raise ManifestLoadError(f"Genesis package contains duplicate logical key: {key!r}")
            seen.add(key)
        raise AssertionError("duplicate logical key was not found")

    required = {README_KEY, OUTPUTS_KEY}
    if require_handler:
        required.add(SEALED_HANDLER_KEY)
    missing = required - unique
    if missing:
        raise ManifestLoadError(
            "Genesis package is missing required control object(s): " + ", ".join(sorted(missing))
        )
    if not any(key.startswith(INPUT_PREFIX) and key != INPUT_PREFIX for key in keys):
        raise ManifestLoadError("Genesis package must contain at least one input/ object")
    return tuple(sorted(keys))


def decode_genesis_readme(content: bytes) -> str:
    """Decode the genesis control prompt as UTF-8 without interpreting it."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManifestLoadError("Genesis README.md must be UTF-8 text") from error


def parse_genesis_outputs(content: bytes) -> tuple[dict[str, object], tuple[OutputSelector, ...]]:
    """Parse and validate the genesis output-selector control document."""
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidScopeError(
            f"outputs.json must contain a UTF-8 JSON object: {error}"
        ) from error
    if not isinstance(value, dict):
        raise InvalidScopeError("outputs.json must contain a URI-to-description object")
    writable: dict[str, object] = value
    return writable, parse_output_selectors(writable)
