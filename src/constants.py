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

SECTION_ORDER = [
    "WIP",
    "For Review",
    "Backlog",
    "Ideas",
    "Bugs",
    "Icebox",
    "Done",
    "Won't Do",
]

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
    "proposed",
    "specified",
    "ready",  # pre-work
    "in-progress",
    "blocked",
    "rework",  # active work
    "for-review",  # review
    "done",
    "released",  # complete
    "bug",
    "bug-fixed",  # bugs
    "icebox",
    "wontdo",  # terminal/parked
]

# Which statuses are valid in which sections
VALID_STATUSES_BY_SECTION = {
    "Ideas": {"proposed"},
    "Backlog": {"proposed", "specified", "ready"},
    "WIP": {"in-progress", "blocked", "rework"},
    "For Review": {"for-review", "rework", "done", "blocked"},
    "Done": {"done", "released"},
    "Won't Do": {"wontdo"},
    "Icebox": {"icebox"},
    "Bugs": {"bug", "bug-fixed"},
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
# Readiness flags — the ONE registry both surfaces (CLI + HTTP) validate against
# ---------------------------------------------------------------------------
#
# `readiness_flags` accepts any flag name at the DB level, so a flag is only
# usable once it is (a) allowed by both surfaces and (b) round-trips through
# PRODUCT_BACKLOG.md. The label below is the markdown line prefix used by both
# the writer and the parser — a flag with no label would be silently dropped on
# the next regeneration, which is exactly the trap this registry exists to close.
#
#   reviewed  — legacy name for the L-pane (Learnings), set at review time
#   spec      — OpenSpec linkage: "<lane letter>:<change name>", e.g.
#               "A:b-44-knowledge-ingestion-pipeline"; lane C may carry "C:" alone
#   verified  — real verify-command evidence: command, exit code, output tail,
#               and the commit sha the run was made against
READINESS_FLAG_LABELS: dict[str, str] = {
    "reviewed": "Reviewed",
    "spec": "Spec",
    "verified": "Verified",
}

VALID_READINESS_FLAGS: set[str] = set(READINESS_FLAG_LABELS)

# Markdown line prefix -> flag name, for the PRODUCT_BACKLOG.md parser.
READINESS_LABEL_TO_FLAG: dict[str, str] = {
    f"{label}:": flag for flag, label in READINESS_FLAG_LABELS.items()
}

# ---------------------------------------------------------------------------
# Spec lanes (see docs/LIFECYCLE.md) — chosen by intent, not by size
# ---------------------------------------------------------------------------
#
# The lane changes how work is *described* up front. It never changes how work
# is *closed*: every lane goes through the same verify + obligation + archive
# gate in actions.accept_ticket().
SPEC_LANES: dict[str, str] = {
    "A": "Spec'd — proposal + spec delta + design + tasks, written before work starts",
    "B": "Interviewed — proposal + spec delta only",
    "C": "Direct — no up-front artifacts; spec delta written from the diff at close, if behaviour changed",
}

DEFAULT_SPEC_LANE = "B"

# Derived spec status — computed from the `spec` readiness flag + the target
# project's openspec/changes/ directory. Filesystem-only: deriving this NEVER
# shells out to the openspec CLI (validation is separately covered by the
# subprocess-priced `spec_validates` predicate).
SPEC_STATUSES: tuple[str, ...] = (
    "undeclared",        # no spec flag, no matching change dir on disk
    "unrecorded_change", # no/empty spec flag, but >=1 matching live change dir exists
    "declared_invalid",  # spec flag set but unparseable (or lane with empty change name)
    "no_delta",          # lane C sentinel: change == "none" with a reason
    "linked",            # flag names a change and its live dir exists
    "linked_missing",    # flag names a change but no live dir and no archive copy
    "archived",          # flag names a change; no live dir, but an archive copy exists
    "forced",            # ticket was accepted with --force (override recorded on the flag)
)

# ---------------------------------------------------------------------------
# Workflow bounce
# ---------------------------------------------------------------------------

WORKFLOW_AGENT_TIMEOUT = 120  # seconds per agent CLI call
WORKFLOW_POLL_INTERVAL = 2000  # ms, frontend polling interval
WORKFLOW_RUN_STATUSES = (
    "pending",
    "running",
    "paused",
    "completed",
    "failed",
    "cancelled",
)

# ---------------------------------------------------------------------------
# Gate banners — one-line guidance per section shown above criteria panel.
# Used by the full-page ticket view (Lane B) to communicate what the user must
# provide so the automation can advance the ticket.
# ---------------------------------------------------------------------------

GATE_BANNER_BY_SECTION: dict[str, str] = {
    "Ideas": "Add a description and at least one criterion → auto-moves to Backlog.",
    "Backlog": "Declare a spec lane (tickets-cli spec) + resolve dependencies → eligible for WIP dispatch.",
    "WIP": "Land a commit + a passing verify → auto-moves to For Review.",
    "For Review": "Verify passes at HEAD + spec validates → accept archives the change and closes.",
    "Done": "Ticket accepted — learnings can be captured in the L flag.",
    "Bugs": "Link this bug to a parent ticket and mark it fixed.",
    "Icebox": "Shelved. Move back to Backlog when ready to resume.",
    "Won't Do": "Closed as won't do. Re-open by moving back to Backlog.",
}

# ---------------------------------------------------------------------------
# Activity event kind labels — short human-readable label for each event_kind
# recorded in the activity_events table.  Used by the Activity tab timeline.
# ---------------------------------------------------------------------------

EVENT_KIND_LABELS: dict[str, str] = {
    "run_started": "Run started",
    "run_succeeded": "Run succeeded",
    "run_failed": "Run failed",
    "run_cancelled": "Run cancelled",
    "section_change": "Moved",
    "status_change": "Status changed",
    "criteria_check": "Criterion checked",
    "criteria_added": "Criterion added",
    "hook_started": "Hook started",
    "hook_succeeded": "Hook succeeded",
    "hook_failed": "Hook failed",
    "workspace_created": "Workspace created",
    "agent_output": "Agent output",
    "pause_set": "Paused",
    "pause_cleared": "Resumed",
    "handoff_recorded": "Handoff recorded",
    "ticket_created": "Ticket created",
    "gate_override": "Accept gate overridden",
}

# ---------------------------------------------------------------------------
# Activity event kind icons — maps each event_kind to a Lucide-style icon name
# present in SVG_ICONS in generate.py.  Lane B renders these via _svg_icon().
# For kinds where no perfect semantic match exists, the closest neighbour is
# used and a TODO comment is added.
# ---------------------------------------------------------------------------

EVENT_KIND_ICONS: dict[str, str] = {
    "run_started": "play",
    "run_succeeded": "check",  # green check on success
    "run_failed": "x",  # X on failure
    "run_cancelled": "square",  # stop square
    "section_change": "arrow-right",
    "status_change": "zap",  # TODO: ideal icon would be 'badge' or 'dot'
    "criteria_check": "check-square",
    "criteria_added": "plus",
    "hook_started": "zap",  # TODO: ideal icon would be 'hook' or 'bolt'
    "hook_succeeded": "check",
    "hook_failed": "x",
    "workspace_created": "grid",  # TODO: ideal icon would be 'folder-plus'
    "agent_output": "file-text",
    "pause_set": "square",  # stop/pause shape
    "pause_cleared": "play",
    "handoff_recorded": "file-text",  # TODO: ideal icon would be 'clipboard-check'
    "ticket_created": "plus",  # TODO: ideal icon would be 'sparkles' or 'file-plus'
    "gate_override": "zap",  # TODO: ideal icon would be 'shield-off'
}

# ---------------------------------------------------------------------------
# Activity event kind groups — maps each event_kind to a coarse-grained
# "category" used for filter chips and badge styling on the Activity tab.
# Multiple event kinds can map to the same group so the user gets a small,
# scannable filter set without losing event-level detail in the row text.
# ---------------------------------------------------------------------------

EVENT_KIND_GROUPS: dict[str, str] = {
    "ticket_created": "Created",
    "section_change": "Moved",
    "status_change": "Status",
    "criteria_check": "Criteria",
    "criteria_added": "Criteria",
    "criteria_removed": "Criteria",
    "criteria_changed": "Criteria",
    "field_changed": "Field",
    "input_provided": "Input",
    "run_started": "Run",
    "run_succeeded": "Run",
    "run_failed": "Run",
    "run_cancelled": "Run",
    "run_stalled": "Run",
    "run_discarded": "Run",
    "agent_output": "Run",
    "handoff_recorded": "Run",
    "hook_started": "Hook",
    "hook_succeeded": "Hook",
    "hook_failed": "Hook",
    "workspace_created": "Workspace",
    "pane_linked": "Workspace",
    "pane_unlinked": "Workspace",
    "pause_set": "Pause",
    "pause_cleared": "Pause",
    "mode_changed": "Pause",
    "kitchen_paused": "Pause",
    "kitchen_resumed": "Pause",
    "gate_override": "Gate",
}

# Display order for the filter chip row; groups not in this list are appended
# alphabetically at the end so new event kinds don't disappear silently.
EVENT_GROUP_ORDER: list[str] = [
    "Created",
    "Moved",
    "Status",
    "Criteria",
    "Run",
    "Hook",
    "Workspace",
    "Field",
    "Input",
    "Pause",
    "Gate",
]

# Per-group accent colour. Picked from the existing palette so light & dark
# themes inherit contrast from CSS variables; values here are direct hex
# fallbacks for the badge background-tint.
EVENT_GROUP_COLORS: dict[str, str] = {
    "Created": "#22c55e",  # green — birth events
    "Moved": "#3b82f6",  # blue — section transitions
    "Status": "#a855f7",  # purple — state transitions
    "Criteria": "#14b8a6",  # teal — acceptance work
    "Run": "#f59e0b",  # amber — agent activity
    "Hook": "#f97316",  # orange — automation hooks
    "Workspace": "#94a3b8",  # slate — infra
    "Field": "#64748b",  # slate-darker — text edits
    "Input": "#ec4899",  # pink — human-in-the-loop
    "Pause": "#ef4444",  # red — interruptions
    "Gate": "#dc2626",  # deep red — a gate was bypassed on purpose
}

# Headline pick order for Follow-mode coalesced steps: when several events on
# one ticket collapse into a single animated step, the earliest kind in this
# list becomes the caption; the rest render as "+N more". Unknown kinds rank
# below everything listed.
FOLLOW_KIND_PRECEDENCE: list[str] = [
    "section_change",
    "ticket_created",
    "status_change",
    "gate_override",
    "run_failed",
    "run_stalled",
    "run_succeeded",
    "run_cancelled",
    "run_discarded",
    "run_started",
    "input_provided",
    "kitchen_paused",
    "kitchen_resumed",
    "pause_set",
    "pause_cleared",
    "mode_changed",
    "handoff_recorded",
    "agent_output",
    "hook_failed",
    "hook_started",
    "hook_succeeded",
    "workspace_created",
    "criteria_check",
    "criteria_added",
    "criteria_removed",
    "criteria_changed",
    "field_changed",
    "pane_linked",
    "pane_unlinked",
]

# ---------------------------------------------------------------------------
# Feedbacks integration
# ---------------------------------------------------------------------------

FEEDBACKS_DEFAULT_PORT = 8080
FEEDBACKS_REPO_URL = "https://github.com/ytubecoder/feedbacks"
FEEDBACKS_TRIAGE_TIMEOUT = 90  # seconds
FEEDBACKS_DETECTION_CACHE_TTL = 30  # seconds

# ---------------------------------------------------------------------------
# Pane-link attention states (migration 23 / src/pane_links.py)
# ---------------------------------------------------------------------------

ATTENTION_NONE = "none"
ATTENTION_QUESTION = "question"
ATTENTION_EXCEPTION = "exception"
ATTENTION_IDLE = "idle"
ATTENTION_STATES = (
    ATTENTION_NONE,
    ATTENTION_QUESTION,
    ATTENTION_EXCEPTION,
    ATTENTION_IDLE,
)

# Pane link tail limits
PANE_TAIL_MAX_LINES = 200
PANE_TAIL_MAX_BYTES = 8 * 1024
PANE_CAPTURE_INTERVAL_S = 2.0
PANE_IDLE_THRESHOLD_S = 30.0
PANE_SEND_KEYS_MAX_BYTES = 4 * 1024
PANE_SEND_KEYS_RATE_PER_S = 10
