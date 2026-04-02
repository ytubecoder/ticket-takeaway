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
