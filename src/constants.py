"""Canonical source of shared constants for Ticket Takeaway.

Every module (tickets-cli.py, generate.py, etc.) should import from here
rather than defining its own copies.  If a constant lives in this file it
is the single source of truth.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DASHBOARD_DIR = Path.home() / ".claude" / "ticket-takeaway"
DB_PATH = DASHBOARD_DIR / "tickets.db"
REGISTRY_PATH = DASHBOARD_DIR / "registry.json"

# ---------------------------------------------------------------------------
# Section slugs (CLI / HTML aliases)
# ---------------------------------------------------------------------------

SECTION_ORDER = ["WIP", "For Review", "Backlog", "Ideas", "Bugs", "Icebox", "Done", "Won't Do"]

SECTION_SLUGS = {
    "Ideas": "ideas",
    "Backlog": "backlog",
    "WIP": "wip",
    "For Review": "review",
    "Done": "done",
    "Won't Do": "wontdo",
    "Icebox": "icebox",
    "Bugs": "bugs",
}

# Lowercase aliases for section names (used in CLI args)
SLUG_TO_SECTION = {v: k for k, v in SECTION_SLUGS.items()}

# ID prefix by section for auto-generation
SECTION_PREFIX = {
    "Ideas": "I",
    "Backlog": "B",
    "WIP": "B",
    "For Review": "B",
    "Done": "R",
    "Won't Do": "W",
    "Icebox": "Z",
    "Bugs": "BUG",
}

# ---------------------------------------------------------------------------
# Status defaults per section
# ---------------------------------------------------------------------------

DEFAULT_STATUS_BY_SECTION = {
    "Ideas": "proposed",
    "Backlog": "proposed",
    "WIP": "in-progress",
    "For Review": "for-review",
    "Done": "done",
    "Won't Do": "wontdo",
    "Icebox": "icebox",
    "Bugs": "bug",
}

# ---------------------------------------------------------------------------
# CSS class per section slug (used by generate.py dashboard renderer)
# ---------------------------------------------------------------------------

CARD_CLASS_BY_SLUG = {
    "backlog": "backlog-card",
    "wip": "wip-card",
    "review": "review-card",
    "ideas": "idea-card",
    "done": "done-card",
    "wontdo": "wontdo-card",
    "icebox": "icebox-card",
    "bugs": "bug-card",
}

# ---------------------------------------------------------------------------
# Canonical status registry — the ONE place statuses are defined
# ---------------------------------------------------------------------------

STATUSES = [
    "proposed", "specified", "ready",       # pre-work
    "in-progress", "blocked", "rework",     # active work
    "for-review",                           # review
    "done", "released",                     # complete
    "bug", "bug-fixed",                     # bugs
    "icebox", "wontdo",                     # terminal/parked
]

# Which statuses are valid in which sections
VALID_STATUSES_BY_SECTION = {
    "Ideas":      {"proposed"},
    "Backlog":    {"proposed", "specified", "ready"},
    "WIP":        {"in-progress", "blocked", "rework"},
    "For Review": {"for-review", "rework", "done", "blocked"},
    "Done":       {"done", "released"},
    "Won't Do":   {"wontdo"},
    "Icebox":     {"icebox"},
    "Bugs":       {"bug", "bug-fixed"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_status_on_move(current_status: str, target_section: str) -> str:
    """Determine status after a section move.

    Preserves current status if it's valid in the target section.
    Otherwise, falls back to the section's default.
    """
    valid = VALID_STATUSES_BY_SECTION.get(target_section, set())
    if current_status in valid:
        return current_status
    return DEFAULT_STATUS_BY_SECTION.get(target_section, "proposed")


# ---------------------------------------------------------------------------
# Workflow bounce
# ---------------------------------------------------------------------------

WORKFLOW_AGENT_TIMEOUT = 120   # seconds per agent CLI call
WORKFLOW_POLL_INTERVAL = 2000  # ms, frontend polling interval
WORKFLOW_RUN_STATUSES = ("pending", "running", "paused", "completed", "failed", "cancelled")

# ---------------------------------------------------------------------------
# Gate banners — one-line guidance per section shown above criteria panel.
# Used by the full-page ticket view (Lane B) to communicate what the user must
# provide so the automation can advance the ticket.
# ---------------------------------------------------------------------------

GATE_BANNER_BY_SECTION: dict[str, str] = {
    "Ideas":      "Add a description and at least one criterion → auto-moves to Backlog.",
    "Backlog":    "Resolve dependencies → auto-moves to WIP.",
    "WIP":        "Land a commit → auto-moves to For Review.",
    "For Review": "All criteria checked + no open bugs → auto-accepts.",
    "Done":       "Ticket accepted — learnings can be captured in the L flag.",
    "Bugs":       "Link this bug to a parent ticket and mark it fixed.",
    "Icebox":     "Shelved. Move back to Backlog when ready to resume.",
    "Won't Do":   "Closed as won't do. Re-open by moving back to Backlog.",
}

# ---------------------------------------------------------------------------
# Activity event kind labels — short human-readable label for each event_kind
# recorded in the activity_events table.  Used by the Activity tab timeline.
# ---------------------------------------------------------------------------

EVENT_KIND_LABELS: dict[str, str] = {
    "run_started":        "Run started",
    "run_succeeded":      "Run succeeded",
    "run_failed":         "Run failed",
    "run_cancelled":      "Run cancelled",
    "section_change":     "Moved",
    "status_change":      "Status changed",
    "criteria_check":     "Criterion checked",
    "criteria_added":     "Criterion added",
    "hook_started":       "Hook started",
    "hook_succeeded":     "Hook succeeded",
    "hook_failed":        "Hook failed",
    "workspace_created":  "Workspace created",
    "agent_output":       "Agent output",
    "pause_set":          "Paused",
    "pause_cleared":      "Resumed",
    "handoff_recorded":   "Handoff recorded",
}

# ---------------------------------------------------------------------------
# Activity event kind icons — maps each event_kind to a Lucide-style icon name
# present in SVG_ICONS in generate.py.  Lane B renders these via _svg_icon().
# For kinds where no perfect semantic match exists, the closest neighbour is
# used and a TODO comment is added.
# ---------------------------------------------------------------------------

EVENT_KIND_ICONS: dict[str, str] = {
    "run_started":        "play",
    "run_succeeded":      "check",         # green check on success
    "run_failed":         "x",             # X on failure
    "run_cancelled":      "square",        # stop square
    "section_change":     "arrow-right",
    "status_change":      "zap",           # TODO: ideal icon would be 'badge' or 'dot'
    "criteria_check":     "check-square",
    "criteria_added":     "plus",
    "hook_started":       "zap",           # TODO: ideal icon would be 'hook' or 'bolt'
    "hook_succeeded":     "check",
    "hook_failed":        "x",
    "workspace_created":  "grid",          # TODO: ideal icon would be 'folder-plus'
    "agent_output":       "file-text",
    "pause_set":          "square",        # stop/pause shape
    "pause_cleared":      "play",
    "handoff_recorded":   "file-text",     # TODO: ideal icon would be 'clipboard-check'
}

# ---------------------------------------------------------------------------
# Feedbacks integration
# ---------------------------------------------------------------------------

FEEDBACKS_DEFAULT_PORT = 8080
FEEDBACKS_REPO_URL = "https://github.com/ytubecoder/feedbacks"
FEEDBACKS_TRIAGE_TIMEOUT = 90  # seconds
FEEDBACKS_DETECTION_CACHE_TTL = 30  # seconds
