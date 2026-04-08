"""Scenario manifest discovery, validation, and gallery publishing.

Phase 1B of the manifest-driven scenario runner.  This module is
import-safe (no side effects at module level) and has no external
dependencies beyond stdlib.

Usage::

    from scenarios import discover_scenarios, validate_manifest, publish_gallery

    manifests = discover_scenarios("tests/scenarios/")
    publish_gallery({"hero-board": "/tmp/hero-board.png"})
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: All action verbs a step may use.  Extending this set is a breaking change
#: because existing manifests that reference an unknown action will fail
#: validation.  Add new verbs here and update the spec together.
VALID_ACTIONS: frozenset[str] = frozenset(
    {
        "open",
        "reload",
        "click",
        "double_click",
        "fill",
        "select",
        "press",
        "wait_for",
        "assert_visible",
        "assert_text",
        "capture",
    }
)

#: Pattern that scenario IDs must match.  Lower-case alphanumerics and hyphens
#: only — no spaces, no underscores, no uppercase.
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Default viewport when the manifest omits the ``viewport`` field.
DEFAULT_VIEWPORT: dict[str, int] = {"width": 1440, "height": 1024}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScenarioValidationError(ValueError):
    """Raised when a scenario manifest fails schema validation.

    Attributes:
        filepath:   Source file that triggered the error (may be empty string
                    when validating an in-memory dict).
        field:      Dot-path of the offending field (e.g. ``"steps[2].actor"``).
        reason:     Human-readable description of the violation.
    """

    def __init__(self, filepath: str, field: str, reason: str) -> None:
        self.filepath = filepath
        self.field = field
        self.reason = reason
        super().__init__(f"{filepath!r} — field '{field}': {reason}")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _check_required(data: dict[str, Any], field: str, expected_type: type, filepath: str) -> Any:
    """Assert *field* is present in *data* and is an instance of *expected_type*.

    Returns the field value on success; raises :exc:`ScenarioValidationError`
    otherwise.
    """
    if field not in data:
        raise ScenarioValidationError(filepath, field, "required field is missing")
    value = data[field]
    if not isinstance(value, expected_type):
        raise ScenarioValidationError(
            filepath,
            field,
            f"expected {expected_type.__name__}, got {type(value).__name__}",
        )
    return value


def _validate_step(step: Any, index: int, actors: set[str], filepath: str) -> None:
    """Validate a single step dict from the ``steps`` list.

    Raises :exc:`ScenarioValidationError` on the first violation found.
    """
    if not isinstance(step, dict):
        raise ScenarioValidationError(
            filepath,
            f"steps[{index}]",
            f"each step must be a dict, got {type(step).__name__}",
        )

    # --- actor ---
    actor = _check_required(step, "actor", str, filepath)
    # Rewrite field path so errors reference the step position.
    _field = lambda name: f"steps[{index}].{name}"

    if actor not in actors:
        raise ScenarioValidationError(
            filepath,
            _field("actor"),
            f"actor {actor!r} is not declared in the top-level 'actors' dict",
        )

    # --- action ---
    action = _check_required(step, "action", str, filepath)
    if action not in VALID_ACTIONS:
        sorted_valid = sorted(VALID_ACTIONS)
        raise ScenarioValidationError(
            filepath,
            _field("action"),
            f"unknown action {action!r}; valid actions are: {sorted_valid}",
        )

    # --- fill requires value ---
    if action == "fill":
        if "value" not in step:
            raise ScenarioValidationError(
                filepath,
                _field("value"),
                "steps with action 'fill' must include a 'value' field",
            )

    # --- target shape (optional but validated when present) ---
    if "target" in step:
        target = step["target"]
        if not isinstance(target, dict):
            raise ScenarioValidationError(
                filepath,
                _field("target"),
                f"'target' must be a dict, got {type(target).__name__}",
            )
        known_target_keys = {"testid", "title", "seed_ref", "css", "text", "role", "open"}
        present_keys = set(target.keys())
        unknown_keys = present_keys - known_target_keys
        if unknown_keys:
            raise ScenarioValidationError(
                filepath,
                _field("target"),
                f"unknown target key(s): {sorted(unknown_keys)}; "
                f"allowed: {sorted(known_target_keys)}",
            )

    # --- capture shape (optional but validated when present) ---
    if "capture" in step:
        capture = step["capture"]
        if not isinstance(capture, dict):
            raise ScenarioValidationError(
                filepath,
                _field("capture"),
                f"'capture' must be a dict, got {type(capture).__name__}",
            )
        if "name" not in capture:
            raise ScenarioValidationError(
                filepath,
                _field("capture.name"),
                "'capture' dict must include a 'name' field",
            )

    # --- assert shape (optional but validated when present) ---
    if "assert" in step:
        assertion = step["assert"]
        if not isinstance(assertion, dict):
            raise ScenarioValidationError(
                filepath,
                _field("assert"),
                f"'assert' must be a dict, got {type(assertion).__name__}",
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_manifest(data: dict[str, Any], filepath: str = "") -> dict[str, Any]:
    """Validate *data* against the scenario manifest schema.

    Parameters
    ----------
    data:
        Parsed JSON dict representing a scenario manifest.
    filepath:
        Human-readable source path included in error messages.  Defaults to
        an empty string when validating an in-memory dict.

    Returns
    -------
    dict
        The validated manifest dict (same object, not a copy).

    Raises
    ------
    ScenarioValidationError
        On the first schema violation encountered.
    """
    # --- top-level required fields ---
    _check_required(data, "id", str, filepath)
    _check_required(data, "title", str, filepath)
    _check_required(data, "tags", list, filepath)
    _check_required(data, "actors", dict, filepath)
    _check_required(data, "seed", dict, filepath)
    steps = _check_required(data, "steps", list, filepath)

    # --- id format ---
    scenario_id: str = data["id"]
    if not scenario_id:
        raise ScenarioValidationError(filepath, "id", "'id' must not be empty")
    if not _ID_PATTERN.match(scenario_id):
        raise ScenarioValidationError(
            filepath,
            "id",
            f"'id' must contain only lower-case alphanumerics and hyphens "
            f"(pattern: [a-z0-9][a-z0-9-]*), got {scenario_id!r}",
        )

    # --- title must be non-empty ---
    if not data["title"].strip():
        raise ScenarioValidationError(filepath, "title", "'title' must not be blank")

    # --- tags must be a list of strings ---
    for i, tag in enumerate(data["tags"]):
        if not isinstance(tag, str):
            raise ScenarioValidationError(
                filepath,
                f"tags[{i}]",
                f"each tag must be a string, got {type(tag).__name__}",
            )

    # --- actors must be a non-empty dict of dicts ---
    actors_dict: dict[str, Any] = data["actors"]
    if not actors_dict:
        raise ScenarioValidationError(filepath, "actors", "'actors' dict must not be empty")
    for actor_name, actor_def in actors_dict.items():
        if not isinstance(actor_def, dict):
            raise ScenarioValidationError(
                filepath,
                f"actors.{actor_name}",
                f"each actor definition must be a dict, got {type(actor_def).__name__}",
            )

    # --- viewport (optional) ---
    if "viewport" in data:
        vp = data["viewport"]
        if not isinstance(vp, dict):
            raise ScenarioValidationError(
                filepath,
                "viewport",
                f"'viewport' must be a dict, got {type(vp).__name__}",
            )
        for dim in ("width", "height"):
            if dim not in vp:
                raise ScenarioValidationError(
                    filepath,
                    f"viewport.{dim}",
                    f"'viewport' must include '{dim}'",
                )
            if not isinstance(vp[dim], int) or vp[dim] <= 0:
                raise ScenarioValidationError(
                    filepath,
                    f"viewport.{dim}",
                    f"'viewport.{dim}' must be a positive integer",
                )
    else:
        # Inject default so callers always see a complete viewport.
        data.setdefault("viewport", DEFAULT_VIEWPORT.copy())

    # --- theme (optional) ---
    if "theme" in data:
        theme = data["theme"]
        if theme not in ("dark", "light"):
            raise ScenarioValidationError(
                filepath, "theme", f"'theme' must be 'dark' or 'light', got '{theme}'"
            )

    # --- validate each step ---
    known_actors: set[str] = set(actors_dict.keys())
    for i, step in enumerate(steps):
        _validate_step(step, i, known_actors, filepath)

    return data


def discover_scenarios(path: str = "tests/scenarios/") -> list[dict[str, Any]]:
    """Find, load, and validate all scenario manifests under *path*.

    Scans *path* recursively for ``*.json`` files.  Each file is parsed as
    JSON and passed through :func:`validate_manifest`.  Files that fail
    validation are skipped with a warning printed to stdout; all other
    discovered manifests are returned.

    Parameters
    ----------
    path:
        Directory to scan.  Relative paths are resolved from the current
        working directory.

    Returns
    -------
    list[dict]
        Validated manifest dicts, sorted by ``id``.
    """
    root = Path(path)
    if not root.is_dir():
        print(f"[scenarios] discovery path does not exist or is not a directory: {root}")
        return []

    manifests: list[dict[str, Any]] = []

    for json_file in sorted(root.rglob("*.json")):
        filepath = str(json_file)
        try:
            raw = json_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[scenarios] WARNING: could not read {filepath}: {exc}")
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[scenarios] WARNING: JSON parse error in {filepath}: {exc}")
            continue

        if not isinstance(data, dict):
            print(f"[scenarios] WARNING: {filepath} is not a JSON object, skipping")
            continue

        try:
            validate_manifest(data, filepath)
        except ScenarioValidationError as exc:
            print(f"[scenarios] WARNING: validation failed for {filepath}: {exc}")
            continue

        manifests.append(data)

    manifests.sort(key=lambda m: m.get("id", ""))
    return manifests


def publish_gallery(
    screenshots: dict[str, str],
    gallery_dir: str = "docs/scenarios/gallery/",
) -> Path:
    """Copy screenshots to the gallery directory and update ``index.json``.

    Parameters
    ----------
    screenshots:
        Mapping of ``publish_slot`` → absolute or relative path to the source
        screenshot file.  Example::

            {"hero-board": "/tmp/run-abc/hero-board.png"}

    gallery_dir:
        Destination directory.  Created if it does not exist.

    Returns
    -------
    Path
        Absolute path to the updated ``index.json`` file.

    Notes
    -----
    Existing entries in ``index.json`` are preserved; entries for slots
    present in *screenshots* are overwritten with the new timestamp and path.
    """
    dest = Path(gallery_dir)
    dest.mkdir(parents=True, exist_ok=True)

    index_path = dest / "index.json"

    # Load existing index so we can merge without losing other slots.
    if index_path.exists():
        try:
            existing: dict[str, Any] = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
    else:
        existing = {}

    now = datetime.now(tz=timezone.utc).isoformat()

    for slot, src_path_str in screenshots.items():
        src = Path(src_path_str)
        if not src.exists():
            print(f"[scenarios] WARNING: screenshot source not found for slot {slot!r}: {src}")
            continue

        # Preserve original extension (.png, .jpg, …).
        dest_filename = f"{slot}{src.suffix}"
        dest_file = dest / dest_filename

        shutil.copy2(src, dest_file)

        existing[slot] = {
            "slot": slot,
            "file": dest_filename,
            "source_path": str(src.resolve()),
            "published_at": now,
        }

    index_path.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return index_path.resolve()
