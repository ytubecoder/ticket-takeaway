"""workflow_config.py — loads repo-owned policy files for the Kitchen orchestrator.

Two files at the project/repo root:
  WORKFLOW.toml  — typed config (Python 3.11+ tomllib, no extra deps)
  PROMPT.md      — agent prompt template (plain markdown, returned as-is)

See docs/KITCHEN.md §11 for the full policy-file specification.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults (mirrors KITCHEN.md §11 exactly)
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    "automation": {
        "default_mode": "manual",
        "max_concurrent_runs": 3,
        "max_concurrent_per_project": 1,
    },
    "agent": {
        "command": "claude -p",
        "sandbox": "workspace-write",
        "max_turns": 20,
        "base_ref": "origin/main",
    },
    "workspace": {
        "retention_days_after_done": 21,
    },
    "evidence": {
        "live_days": 30,
        "summarised_days": 60,
    },
    "hooks": {
        "timeout_ms": 60000,
        "after_create": "",
        "before_run": "",
        "after_run": "",
        "before_remove": "",
    },
    # M4: ScenarioRunner reads base_url. Project_id is substituted by the
    # runner — '{project_id}' in the URL is a literal placeholder.
    "scenario": {
        "base_url": "http://localhost:8787/{project_id}",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: override's leaves win; unknown keys are preserved."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_workflow_config(project_path: Path) -> dict:
    """Load WORKFLOW.toml from *project_path*, deep-merge over DEFAULTS.

    Missing file  → exact copy of DEFAULTS (no mutation).
    Empty file    → exact copy of DEFAULTS.
    Partial file  → user values override DEFAULTS at the leaf level.
    Unknown top-level sections / leaf keys → preserved (forward-compat).
    Invalid TOML  → raises ValueError with a clear message including file path.

    Returns a NEW dict; DEFAULTS is never mutated.
    """
    toml_path = project_path / "WORKFLOW.toml"

    if not toml_path.exists():
        return copy.deepcopy(DEFAULTS)

    raw = toml_path.read_bytes()
    if not raw.strip():
        return copy.deepcopy(DEFAULTS)

    try:
        user_config = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"Invalid TOML in {toml_path}: {exc}"
        ) from exc

    return _deep_merge(DEFAULTS, user_config)


def load_prompt_template(project_path: Path) -> str:
    """Load PROMPT.md from *project_path*.

    Missing file → "".
    Empty file   → "".
    Returns trimmed string content (placeholders such as {{subject.id}} kept intact).
    """
    prompt_path = project_path / "PROMPT.md"

    if not prompt_path.exists():
        return ""

    content = prompt_path.read_text(encoding="utf-8").strip()
    return content
