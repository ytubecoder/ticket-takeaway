"""Workspace manager — per-subject git worktrees + lifecycle hooks.

M1a stub. M3 fills in:
  - worktree create/reuse under ~/.claude/ticket-takeaway/workspaces/{project}/{subject_type}/{subject_id}/
  - branch naming `kitchen/{subject_type}/{subject_id}` per docs/KITCHEN.md §10b
  - hooks: after_create (one-time, marker-guarded), before_run, after_run, before_remove
  - safety invariants from §10:
      cwd MUST equal workspace_path
      workspace_path MUST live inside workspace_root
      workspace key sanitized to [A-Za-z0-9._-]
"""

from __future__ import annotations

import os
from pathlib import Path

from constants import DASHBOARD_DIR

WORKSPACE_ROOT = DASHBOARD_DIR / "workspaces"


def workspace_path_for(project_id: str, subject_type: str, subject_id: str) -> Path:
    """Return the deterministic workspace path for a subject. Does not create it."""
    safe = _sanitize_key(subject_id)
    return WORKSPACE_ROOT / project_id / subject_type / safe


def _sanitize_key(key: str) -> str:
    """Per §10 safety invariant 3: only [A-Za-z0-9._-] allowed."""
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in key)
