"""Ticket operations — pure DB + business logic, no markdown sync or dashboard regen.

Extracted from tickets-cli.py so callers (CLI, serve.py, future API) can share
the same logic without side effects.  Every function here operates on an open
sqlite3.Connection and returns a result.  Callers are responsible for:

    1. Calling conn.commit() if they want to persist (functions do NOT commit).
    2. Running sync_to_markdown() / regenerate_dashboard() afterwards.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from constants import (
    DEFAULT_STATUS_BY_SECTION,
    SECTION_ORDER,
    SECTION_PREFIX,
    compute_status_on_move,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def capture_commit_hash(project_path: str) -> str:
    """Return the short commit hash of HEAD for *project_path*, or '' on failure."""
    if not project_path:
        return ""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h"],
            cwd=project_path, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Typed errors — paired with an HTTP-status mapper in serve.py
# ---------------------------------------------------------------------------
# These subclass ValueError so legacy `except ValueError:` blocks keep working.
# New call sites should catch the specific type they care about, and route
# handlers can use DashboardHandler._send_typed_error() to emit a uniform
# {"code", "error"} body with the matching status code.

class AppError(ValueError):
    """Base class for application errors with an HTTP status mapping."""
    http_status: int = 500
    code: str = "internal_error"


class NotFoundError(AppError):
    http_status = 404
    code = "not_found"


class TicketNotFoundError(NotFoundError):
    code = "ticket_not_found"


class ProjectNotFoundError(NotFoundError):
    code = "project_not_found"


class ValidationError(AppError):
    http_status = 400
    code = "validation_error"


class ConflictError(AppError):
    http_status = 409
    code = "conflict"


def _find_ticket(conn: sqlite3.Connection, project_id: str, ticket_id: str) -> sqlite3.Row:
    """Locate a ticket by ID (case-insensitive). Raises TicketNotFoundError if not found."""
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
        (ticket_id, project_id),
    ).fetchone()
    if not ticket:
        raise TicketNotFoundError(f"Ticket '{ticket_id}' not found in project '{project_id}'.")
    return ticket


def _next_sort_order(conn: sqlite3.Connection, project_id: str, section: str) -> int:
    """Return max(sort_order)+1 for *section*, or 0 if empty."""
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order "
        "FROM tickets WHERE project_id = ? AND section = ?",
        (project_id, section),
    ).fetchone()
    return row["next_order"]


def auto_generate_id(conn: sqlite3.Connection, project_id: str, section: str) -> str:
    """Generate the next ticket ID for a section (e.g., B-14, BUG-10, I-03)."""
    prefix = SECTION_PREFIX.get(section, "B")
    sep = "-"
    pattern = f"{prefix}{sep}%"

    rows = conn.execute(
        "SELECT id FROM tickets WHERE project_id = ? AND id LIKE ?",
        (project_id, pattern),
    ).fetchall()

    max_num = 0
    for row in rows:
        tid = row["id"]
        suffix = tid[len(prefix) + len(sep):]
        try:
            num = int(suffix)
            if num > max_num:
                max_num = num
        except ValueError:
            pass

    return f"{prefix}{sep}{max_num + 1:02d}"


# ---------------------------------------------------------------------------
# Kitchen — actor attribution, audit emission, eligibility, mode actions.
# See docs/KITCHEN.md §6-§9b. Activity events are written in the same DB
# transaction as the mutation that produced them; callers commit once.
# ---------------------------------------------------------------------------

CANONICAL_SECTIONS_FOR_ELIGIBILITY = ("Backlog", "WIP", "For Review")
CLEARED_DEP_STATUSES = ("done", "released")
ACTIVE_RUN_STATUSES = ("queued", "preparing", "running", "needs_input")


def utcnow_iso() -> str:
    """ISO-8601 UTC timestamp suitable for activity_events.occurred_at and similar."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ActorContext:
    """Who is performing a mutation. Threaded through every actions.py call.

    actor_type ∈ {'human', 'agent', 'system'}.
    actor_id is run_id (as str) for agent, user identifier for human, None for system.
    """
    actor_type: str
    actor_id: Optional[str] = None

    @classmethod
    def human(cls, user_id: Optional[str] = None) -> "ActorContext":
        return cls(actor_type="human", actor_id=user_id)

    @classmethod
    def agent(cls, run_id: int | str) -> "ActorContext":
        return cls(actor_type="agent", actor_id=str(run_id))

    @classmethod
    def system(cls) -> "ActorContext":
        return cls(actor_type="system", actor_id=None)


def emit_event(
    conn: sqlite3.Connection,
    project_id: str,
    subject_type: str,
    subject_id: str,
    event_kind: str,
    payload: dict[str, Any],
    actor: ActorContext,
) -> int:
    """Insert one activity_events row in the caller's open transaction.

    Returns the new row id. Caller must commit. Mutation and emit_event MUST
    share a transaction so the audit log can never disagree with state.
    """
    cur = conn.execute(
        """
        INSERT INTO activity_events
            (project_id, subject_type, subject_id, actor_type, actor_id,
             event_kind, payload_json, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id, subject_type, subject_id,
            actor.actor_type, actor.actor_id,
            event_kind, json.dumps(payload, ensure_ascii=False),
            utcnow_iso(),
        ),
    )
    return cur.lastrowid


# ---- Eligibility -----------------------------------------------------------

@dataclass(frozen=True)
class EligibilityResult:
    """Outcome of an eligibility check. Always carries reasons (for UI tooltips)."""
    eligible: bool
    reasons: tuple[str, ...]


def _has_active_run(
    conn: sqlite3.Connection, project_id: str, subject_type: str, subject_id: str
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM runs
        WHERE project_id = ? AND subject_type = ? AND subject_id = ?
              AND status IN ('queued', 'preparing', 'running', 'needs_input')
        LIMIT 1
        """,
        (project_id, subject_type, subject_id),
    ).fetchone()
    return row is not None


def _automation_mode(conn: sqlite3.Connection, project_id: str, subject_type: str, subject_id: str) -> str:
    """Return automation_mode for a subject; 'manual' if no row exists."""
    row = conn.execute(
        "SELECT automation_mode FROM automation_subjects "
        "WHERE project_id = ? AND subject_type = ? AND subject_id = ?",
        (project_id, subject_type, subject_id),
    ).fetchone()
    return row[0] if row else "manual"


def _deps_clear(conn: sqlite3.Connection, project_id: str, ticket_id: str) -> tuple[bool, list[str]]:
    """Return (clear, blocking_reasons). See docs/KITCHEN.md §7 'Deps clear means'."""
    deps = conn.execute(
        "SELECT depends_on_id FROM depends WHERE ticket_id = ? AND project_id = ?",
        (ticket_id, project_id),
    ).fetchall()
    blocking: list[str] = []
    for (dep_id,) in deps:
        row = conn.execute(
            "SELECT id, section, status, archived FROM tickets "
            "WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (dep_id, project_id),
        ).fetchone()
        if not row:
            blocking.append(f"missing dep: {dep_id}")
            continue
        if row["archived"] == 1:
            blocking.append(f"dep {row['id']} is archived")
            continue
        if row["section"] != "Done" and row["status"] not in CLEARED_DEP_STATUSES:
            blocking.append(f"dep {row['id']} not done (section={row['section']}, status={row['status']})")
    return (len(blocking) == 0, blocking)


def _journey_compiles_and_validates(conn: sqlite3.Connection, project_id: str, journey_id: str) -> bool:
    """True if the journey can be compiled to a manifest and that manifest validates."""
    try:
        from journeys import compile_to_manifest
        from scenarios import validate_manifest
    except ImportError:
        return False
    try:
        manifest = compile_to_manifest(conn, project_id, journey_id)
        validate_manifest(manifest)
        return True
    except Exception:
        return False


def _tests_covered(conn: sqlite3.Connection, ticket: sqlite3.Row) -> tuple[bool, list[str]]:
    """Return (covered, reasons). See docs/KITCHEN.md §7 'Tests covered means'."""
    reasons: list[str] = []
    project_id = ticket["project_id"]
    ticket_id = ticket["id"]

    # Path 1: readiness_flags row with flag='tests' AND non-empty content.
    tests_row = conn.execute(
        "SELECT content FROM readiness_flags "
        "WHERE ticket_id = ? AND project_id = ? AND flag = 'tests'",
        (ticket_id, project_id),
    ).fetchone()
    if tests_row and (tests_row["content"] or "").strip():
        return (True, ["tests readiness flag has content"])

    # Path 2: any linked journey that compiles + validates.
    journey_rows = conn.execute(
        "SELECT journey_id FROM journey_tickets "
        "WHERE ticket_id = ? AND project_id = ?",
        (ticket_id, project_id),
    ).fetchall()
    for (journey_id,) in journey_rows:
        if _journey_compiles_and_validates(conn, project_id, journey_id):
            return (True, [f"linked journey {journey_id} compiles+validates"])

    # Path 3: explicit no_test_required with non-empty note.
    if ticket["no_test_required"] == 1 and (ticket["no_test_required_note"] or "").strip():
        return (True, ["no_test_required (explicit)"])

    # No path satisfied — explain.
    if not tests_row:
        reasons.append("no tests readiness flag")
    elif not (tests_row["content"] or "").strip():
        reasons.append("tests readiness flag is empty")
    if journey_rows and not any(
        _journey_compiles_and_validates(conn, project_id, jid) for (jid,) in journey_rows
    ):
        reasons.append("linked journeys do not compile/validate")
    elif not journey_rows:
        reasons.append("no linked journey")
    if ticket["no_test_required"] != 1:
        reasons.append("no_test_required not set")
    elif not (ticket["no_test_required_note"] or "").strip():
        reasons.append("no_test_required has no rationale note")
    return (False, reasons)


def _ticket_eligibility(conn: sqlite3.Connection, ticket: sqlite3.Row) -> EligibilityResult:
    """Eligibility for a ticket subject. See docs/KITCHEN.md §7."""
    reasons: list[str] = []
    project_id = ticket["project_id"]
    ticket_id = ticket["id"]

    mode = _automation_mode(conn, project_id, "ticket", ticket_id)
    if mode != "auto":
        reasons.append(f"automation_mode is {mode}, not auto")

    if ticket["section"] not in CANONICAL_SECTIONS_FOR_ELIGIBILITY:
        reasons.append(f"section {ticket['section']!r} is not Backlog/WIP/For Review")

    if ticket["draft"] == 1:
        reasons.append("ticket is draft")
    if ticket["archived"] == 1:
        reasons.append("ticket is archived")

    if not (ticket["description"] or "").strip():
        reasons.append("description is empty")

    crit_count = conn.execute(
        "SELECT COUNT(*) FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
        (ticket_id, project_id),
    ).fetchone()[0]
    if crit_count == 0:
        reasons.append("no acceptance criteria")

    deps_ok, dep_reasons = _deps_clear(conn, project_id, ticket_id)
    if not deps_ok:
        reasons.extend(dep_reasons)

    tests_ok, test_reasons = _tests_covered(conn, ticket)
    if not tests_ok:
        reasons.extend(test_reasons)

    if _has_active_run(conn, project_id, "ticket", ticket_id):
        reasons.append("active run already exists")

    eligible = (
        mode == "auto"
        and ticket["section"] in CANONICAL_SECTIONS_FOR_ELIGIBILITY
        and ticket["draft"] != 1
        and ticket["archived"] != 1
        and (ticket["description"] or "").strip()
        and crit_count > 0
        and deps_ok
        and tests_ok
        and not _has_active_run(conn, project_id, "ticket", ticket_id)
    )
    return EligibilityResult(eligible=bool(eligible), reasons=tuple(reasons))


def _journey_eligibility(conn: sqlite3.Connection, project_id: str, journey_id: str) -> EligibilityResult:
    """Eligibility for a journey subject. See docs/KITCHEN.md §7."""
    reasons: list[str] = []
    mode = _automation_mode(conn, project_id, "journey", journey_id)
    if mode != "auto":
        reasons.append(f"automation_mode is {mode}, not auto")
    if not _journey_compiles_and_validates(conn, project_id, journey_id):
        reasons.append("manifest does not compile/validate")
    if _has_active_run(conn, project_id, "journey", journey_id):
        reasons.append("active run already exists")
    eligible = (
        mode == "auto"
        and _journey_compiles_and_validates(conn, project_id, journey_id)
        and not _has_active_run(conn, project_id, "journey", journey_id)
    )
    return EligibilityResult(eligible=bool(eligible), reasons=tuple(reasons))


def eligibility(conn: sqlite3.Connection, project_id: str, subject_type: str, subject_id: str) -> EligibilityResult:
    """Compute Kitchen eligibility for any subject. See docs/KITCHEN.md §7.

    Always returns reasons, even when eligible=True (for UI tooltips).
    """
    if subject_type == "ticket":
        try:
            ticket = _find_ticket(conn, project_id, subject_id)
        except ValueError:
            return EligibilityResult(False, (f"ticket {subject_id!r} not found",))
        return _ticket_eligibility(conn, ticket)
    if subject_type == "journey":
        return _journey_eligibility(conn, project_id, subject_id)
    if subject_type == "investigation":
        return EligibilityResult(False, ("investigations not implemented in M1a",))
    return EligibilityResult(False, (f"unknown subject_type {subject_type!r}",))


# ---- Mode actions ----------------------------------------------------------

def _upsert_subject(
    conn: sqlite3.Connection,
    project_id: str,
    subject_type: str,
    subject_id: str,
    actor: ActorContext,
) -> None:
    """Lazy-create the automation_subjects row at default 'manual' if missing."""
    now = utcnow_iso()
    actor_str = f"{actor.actor_type}:{actor.actor_id}" if actor.actor_id else actor.actor_type
    conn.execute(
        """
        INSERT INTO automation_subjects
            (project_id, subject_type, subject_id, automation_mode,
             created_at, created_by, updated_at, updated_by)
        VALUES (?, ?, ?, 'manual', ?, ?, ?, ?)
        ON CONFLICT (project_id, subject_type, subject_id) DO NOTHING
        """,
        (project_id, subject_type, subject_id, now, actor_str, now, actor_str),
    )


def set_automation_mode(
    conn: sqlite3.Connection,
    project_id: str,
    subject_type: str,
    subject_id: str,
    mode: str,
    actor: ActorContext,
    pause_reason: str | None = None,
) -> None:
    """Set a subject's automation_mode. Emits the appropriate event(s).
    Caller must commit.

    Valid modes: 'manual', 'auto', 'paused'. `pause_reason` is optional —
    empty/whitespace strings are normalised to None. Lazy-creates the
    automation_subjects row if it doesn't exist.
    """
    if mode not in ("manual", "auto", "paused"):
        raise ValueError(f"invalid mode: {mode!r}")
    if mode == "paused":
        pause_reason = (pause_reason or "").strip() or None

    _upsert_subject(conn, project_id, subject_type, subject_id, actor)

    prior = conn.execute(
        "SELECT automation_mode, pause_reason FROM automation_subjects "
        "WHERE project_id = ? AND subject_type = ? AND subject_id = ?",
        (project_id, subject_type, subject_id),
    ).fetchone()
    prior_mode = prior["automation_mode"]
    prior_reason = prior["pause_reason"]

    if prior_mode == mode and (mode != "paused" or prior_reason == pause_reason):
        return  # no-op

    now = utcnow_iso()
    actor_str = f"{actor.actor_type}:{actor.actor_id}" if actor.actor_id else actor.actor_type
    conn.execute(
        """
        UPDATE automation_subjects
        SET automation_mode = ?, pause_reason = ?, updated_at = ?, updated_by = ?
        WHERE project_id = ? AND subject_type = ? AND subject_id = ?
        """,
        (mode, pause_reason if mode == "paused" else None, now, actor_str,
         project_id, subject_type, subject_id),
    )

    if mode == "paused":
        emit_event(
            conn, project_id, subject_type, subject_id,
            "pause_set",
            {"before": prior_mode, "after": "paused", "reason": pause_reason},
            actor,
        )
    elif prior_mode == "paused":
        emit_event(
            conn, project_id, subject_type, subject_id,
            "pause_cleared",
            {"before": "paused", "after": mode, "prior_reason": prior_reason},
            actor,
        )
    else:
        emit_event(
            conn, project_id, subject_type, subject_id,
            "mode_changed",
            {"before": prior_mode, "after": mode},
            actor,
        )


def set_no_test_required(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    enabled: bool,
    note: str,
    actor: ActorContext,
) -> None:
    """Toggle the no_test_required eligibility bypass on a ticket.

    When enabled=True, note must be non-empty (the rationale).
    No event_kind in the M1a spine covers this — field_changed lands in M1b.
    Caller must commit.
    """
    if enabled and not (note or "").strip():
        raise ValueError("no_test_required requires a non-empty rationale note")

    ticket = _find_ticket(conn, project_id, ticket_id)
    flag = 1 if enabled else 0
    note_text = note.strip() if enabled else ""
    if ticket["no_test_required"] == flag and (ticket["no_test_required_note"] or "") == note_text:
        return  # no-op

    conn.execute(
        "UPDATE tickets SET no_test_required = ?, no_test_required_note = ?, updated_at = ? "
        "WHERE id = ? AND project_id = ?",
        (flag, note_text, utcnow_iso(), ticket["id"], project_id),
    )


# ---------------------------------------------------------------------------
# Core ticket operations
# ---------------------------------------------------------------------------

def move_ticket(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    target_section: str,
    project_path: str = "",
    actor: ActorContext = ActorContext.human(),
) -> str:
    """Move a ticket to *target_section*.

    Uses compute_status_on_move() to decide the new status (preserves the
    current status when it is valid in the target section).  Captures the
    HEAD commit hash when moving to Done. Emits M1a spine events
    `section_change` and (when status changes) `status_change`.

    Returns the canonical ticket ID.
    """
    ticket = _find_ticket(conn, project_id, ticket_id)
    tid = ticket["id"]
    old_section = ticket["section"]
    old_status = ticket["status"]

    new_status = compute_status_on_move(old_status, target_section)
    sort_order = _next_sort_order(conn, project_id, target_section)

    # Capture commit hash when moving to Done
    commit_hash_val = ""
    if target_section == "Done":
        commit_hash_val = capture_commit_hash(project_path)

    now = datetime.now().isoformat()

    if commit_hash_val:
        conn.execute(
            "UPDATE tickets SET section = ?, status = ?, sort_order = ?, "
            "updated_at = ?, commit_hash = ? "
            "WHERE id = ? AND project_id = ?",
            (target_section, new_status, sort_order, now, commit_hash_val, tid, project_id),
        )
    else:
        conn.execute(
            "UPDATE tickets SET section = ?, status = ?, sort_order = ?, "
            "updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            (target_section, new_status, sort_order, now, tid, project_id),
        )

    # M1a spine events — emit BEFORE side-effect hooks so they appear first in history.
    if old_section != target_section:
        emit_event(conn, project_id, "ticket", tid, "section_change",
                   {"before": old_section, "after": target_section}, actor)
    if old_status != new_status:
        emit_event(conn, project_id, "ticket", tid, "status_change",
                   {"before": old_status, "after": new_status}, actor)

    # Post-change hooks. Phase A migration (tidy-newt) moved parent-promote
    # and auto-accept into system workflows; what remains here is the journey
    # cascade and commit-hash capture in _after_section_change.
    _after_section_change(conn, project_id, tid, old_section, target_section)
    _after_status_change(conn, project_id, tid, old_status, new_status)

    return tid


def accept_ticket(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    project_path: str,
    project_name: str,
    actor: ActorContext = ActorContext.human(),
) -> str:
    """Accept a ticket: move to Done with status 'done' and append to PRODUCT_SPECIFICATION.md.

    Emits M1a spine events `section_change` and `status_change` as needed.
    Returns the canonical ticket ID.
    """
    ticket = _find_ticket(conn, project_id, ticket_id)
    tid = ticket["id"]
    old_section = ticket["section"]
    old_status = ticket["status"]

    commit_hash_val = capture_commit_hash(project_path)
    sort_order = _next_sort_order(conn, project_id, "Done")
    now = datetime.now().isoformat()

    conn.execute(
        "UPDATE tickets SET section = 'Done', status = 'done', "
        "sort_order = ?, updated_at = ?, commit_hash = ? "
        "WHERE id = ? AND project_id = ?",
        (sort_order, now, commit_hash_val, tid, project_id),
    )

    if old_section != "Done":
        emit_event(conn, project_id, "ticket", tid, "section_change",
                   {"before": old_section, "after": "Done"}, actor)
    if old_status != "done":
        emit_event(conn, project_id, "ticket", tid, "status_change",
                   {"before": old_status, "after": "done"}, actor)

    # Append to PRODUCT_SPECIFICATION.md
    spec_path = Path(project_path) / "PRODUCT_SPECIFICATION.md"
    today = datetime.now().strftime("%Y-%m-%d")
    entry = f"\n### {tid}: {ticket['title']}\n"
    entry += f"Priority: {ticket['priority']} | Status: released\n"
    commit_info = f" | Commit: {commit_hash_val}" if commit_hash_val else ""
    entry += f"Released: {today}{commit_info}\n"
    if ticket["description"]:
        entry += f"{ticket['description']}\n"

    if spec_path.exists():
        content = spec_path.read_text(encoding="utf-8")
        # Insert before ## Archive if it exists, otherwise append
        if "## Archive" in content:
            content = content.replace("## Archive", entry + "\n## Archive")
        else:
            content = content.rstrip() + "\n" + entry
        spec_path.write_text(content, encoding="utf-8")
    else:
        spec_path.write_text(
            f"# Product Specification \u2014 {project_name}\n{entry}\n",
            encoding="utf-8",
        )

    # Post-change hooks
    _after_section_change(conn, project_id, tid, old_section, "Done")
    _after_status_change(conn, project_id, tid, old_status, "done")

    return tid


def add_ticket(
    conn: sqlite3.Connection,
    project_id: str,
    title: str,
    section: str = "Backlog",
    priority: str = "medium",
    description: str = "",
    parent: Optional[str] = None,
    draft: bool = False,
    source_attachment_id: Optional[int] = None,
    tags: Optional[list[str]] = None,
) -> str:
    """Add a new ticket.  Auto-generates the ID from *section* prefix.

    Returns the new ticket ID.
    """
    status = DEFAULT_STATUS_BY_SECTION[section]
    ticket_id = auto_generate_id(conn, project_id, section)
    sort_order = _next_sort_order(conn, project_id, section)

    conn.execute(
        "INSERT INTO tickets (id, project_id, title, priority, status, "
        "section, description, parent, sort_order, draft, source_attachment_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, project_id, title, priority, status,
         section, description, parent, sort_order, int(draft), source_attachment_id),
    )

    if tags:
        for tag in tags:
            tag = tag.strip().lower()
            if tag:
                conn.execute(
                    "INSERT OR IGNORE INTO ticket_tags (ticket_id, project_id, tag) "
                    "VALUES (?, ?, ?)",
                    (ticket_id, project_id, tag),
                )

    return ticket_id


def update_ticket(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    *,
    title: Optional[str] = None,
    priority: Optional[str] = None,
    status: Optional[str] = None,
    description: Optional[str] = None,
    parent: Optional[str] = ...,  # sentinel — None means "clear parent"
    summary: Optional[str] = None,
    add_criteria: Optional[list[str]] = None,
    check_criteria: Optional[int] = None,
    uncheck_criteria: Optional[int] = None,
    remove_criteria: Optional[int] = None,
    add_depends: Optional[list[str]] = None,
    remove_depends: Optional[list[str]] = None,
    add_tags: Optional[list[str]] = None,
    remove_tags: Optional[list[str]] = None,
    add_branches: Optional[list[str]] = None,
    remove_branches: Optional[list[str]] = None,
    actor: ActorContext = ActorContext.human(),
) -> str:
    """Partial update of a ticket.  Only fields that are not None/sentinel are changed.

    Emits M1a spine `status_change` and `criteria_check` events. Other field
    edits (title, description, criteria text, deps) emit no event in M1a — they
    move to M1b's `field_changed` / `criteria_added` / `criteria_removed` /
    `criteria_changed` / `dependency_changed` vocabulary.

    Returns the canonical ticket ID.
    """
    ticket = _find_ticket(conn, project_id, ticket_id)
    tid = ticket["id"]
    old_status = ticket["status"]

    # ---- scalar field updates ----
    updates: dict[str, object] = {}
    if title is not None:
        updates["title"] = title
    if priority is not None:
        updates["priority"] = priority.lower()
    if status is not None:
        updates["status"] = status.lower()
    if description is not None:
        updates["description"] = description
    if parent is not ...:
        # None clears the parent; a string sets it
        updates["parent"] = parent if parent else None
    if summary is not None:
        updates["summary"] = summary

    if updates:
        updates["updated_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [tid, project_id]
        conn.execute(
            f"UPDATE tickets SET {set_clause} WHERE id = ? AND project_id = ?",
            values,
        )

    # ---- acceptance criteria operations ----
    if add_criteria:
        for text in add_criteria:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next "
                "FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
                (tid, project_id),
            ).fetchone()
            conn.execute(
                "INSERT INTO acceptance_criteria "
                "(ticket_id, project_id, text, checked, sort_order) VALUES (?, ?, ?, 0, ?)",
                (tid, project_id, text, row["next"]),
            )

    if check_criteria is not None:
        _update_criterion(conn, tid, project_id, check_criteria, checked=1, actor=actor)

    if uncheck_criteria is not None:
        _update_criterion(conn, tid, project_id, uncheck_criteria, checked=0, actor=actor)

    if remove_criteria is not None:
        _remove_criterion(conn, tid, project_id, remove_criteria)

    # ---- depends operations ----
    if add_depends:
        for dep in add_depends:
            conn.execute(
                "INSERT OR IGNORE INTO depends (ticket_id, project_id, depends_on_id) "
                "VALUES (?, ?, ?)",
                (tid, project_id, dep),
            )

    if remove_depends:
        for dep in remove_depends:
            conn.execute(
                "DELETE FROM depends "
                "WHERE ticket_id = ? AND project_id = ? AND depends_on_id = ?",
                (tid, project_id, dep),
            )

    # ---- tag operations ----
    if add_tags:
        for tag in add_tags:
            tag = tag.strip().lower()
            if tag:
                conn.execute(
                    "INSERT OR IGNORE INTO ticket_tags (ticket_id, project_id, tag) "
                    "VALUES (?, ?, ?)",
                    (tid, project_id, tag),
                )

    if remove_tags:
        for tag in remove_tags:
            tag = tag.strip().lower()
            if tag:
                conn.execute(
                    "DELETE FROM ticket_tags "
                    "WHERE ticket_id = ? AND project_id = ? AND tag = ?",
                    (tid, project_id, tag),
                )

    # ---- branch operations ----
    if add_branches:
        for branch in add_branches:
            branch = branch.strip()
            if branch:
                conn.execute(
                    "INSERT OR IGNORE INTO ticket_branches "
                    "(ticket_id, project_id, branch_name) VALUES (?, ?, ?)",
                    (tid, project_id, branch),
                )

    if remove_branches:
        for branch in remove_branches:
            branch = branch.strip()
            if branch:
                conn.execute(
                    "DELETE FROM ticket_branches "
                    "WHERE ticket_id = ? AND project_id = ? AND branch_name = ?",
                    (tid, project_id, branch),
                )

    # M1a spine event for status change. Field/dep/criteria-text events land in M1b.
    # Other field edits (title, description, criteria text, deps) emit no event in
    # M1a — they move to M1b's `field_changed` / `criteria_added` / `criteria_removed`
    # / `criteria_changed` / `dependency_changed` vocabulary.
    new_status = updates.get("status", old_status)
    if new_status != old_status:
        emit_event(conn, project_id, "ticket", tid, "status_change",
                   {"before": old_status, "after": new_status}, actor)
        _after_status_change(conn, project_id, tid, old_status, new_status)

    return tid


def confirm_ticket(conn: sqlite3.Connection, project_id: str, ticket_id: str) -> str:
    """Confirm a draft ticket — sets draft=0 and clears source_attachment_id."""
    ticket = _find_ticket(conn, project_id, ticket_id)
    tid = ticket["id"]
    conn.execute(
        "UPDATE tickets SET draft = 0, source_attachment_id = NULL, updated_at = ? "
        "WHERE id = ? AND project_id = ?",
        (datetime.now().isoformat(), tid, project_id),
    )
    return tid


# ---------------------------------------------------------------------------
# Criteria helpers (internal)
# ---------------------------------------------------------------------------

def _update_criterion(
    conn: sqlite3.Connection, tid: str, project_id: str, index: int, checked: int,
    actor: ActorContext = ActorContext.human(),
):
    """Update the checked state of the Nth criterion (1-indexed). Emits `criteria_check`."""
    criteria = conn.execute(
        "SELECT id, checked FROM acceptance_criteria "
        "WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
        (tid, project_id),
    ).fetchall()
    if 1 <= index <= len(criteria):
        criterion = criteria[index - 1]
        before = bool(criterion["checked"])
        after = bool(checked)
        if before == after:
            return  # no-op
        conn.execute(
            "UPDATE acceptance_criteria SET checked = ? WHERE id = ?",
            (checked, criterion["id"]),
        )
        emit_event(conn, project_id, "ticket", tid, "criteria_check",
                   {"criterion_id": criterion["id"], "before": before, "after": after}, actor)
    else:
        raise IndexError(
            f"Criterion index {index} out of range (1-{len(criteria)})"
        )


def _remove_criterion(
    conn: sqlite3.Connection, tid: str, project_id: str, index: int
):
    """Remove the Nth criterion (1-indexed)."""
    criteria = conn.execute(
        "SELECT id FROM acceptance_criteria "
        "WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
        (tid, project_id),
    ).fetchall()
    if 1 <= index <= len(criteria):
        conn.execute(
            "DELETE FROM acceptance_criteria WHERE id = ?",
            (criteria[index - 1]["id"],),
        )
    else:
        raise IndexError(
            f"Criterion index {index} out of range (1-{len(criteria)})"
        )


# ---------------------------------------------------------------------------
# Post-change hooks
# ---------------------------------------------------------------------------

def _has_open_bugs(conn: sqlite3.Connection, project_id: str, ticket_id: str) -> bool:
    """Return True if *ticket_id* has any child bugs not in a terminal status."""
    terminal = {"done", "for-review", "bug-fixed"}
    children = conn.execute(
        "SELECT status FROM tickets WHERE parent = ? AND project_id = ?",
        (ticket_id, project_id),
    ).fetchall()
    return any(c["status"] not in terminal for c in children)


def _after_status_change(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    old_status: str,
    new_status: str,
) -> None:
    """Run post-status-change side effects.

    Phase A migration (tidy-newt): the parent-promote rule was migrated to a
    system workflow ("Parent auto-promote", workflows_seed.py). The legacy
    _maybe_promote_parent() helper is kept defined for safety but no longer
    invoked from this hook — the dispatcher's next tick picks up parents whose
    children all reached terminal status and applies the move via the workflow
    engine.
    """
    return  # noqa: F811 — explicit: no synchronous side effects


def _after_section_change(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    old_section: str,
    new_section: str,
) -> None:
    """Run post-move logic after a ticket changes section.

    Phase A migration (tidy-newt): parent-promote moved to a system workflow.
    This hook now only handles the journey cascade and commit-hash capture
    (both of which are write side effects this transaction owns).
    """
    # Kitchen M4: when a ticket reaches Done, cascade to any linked journeys.
    # Each linked journey gets a scenario run queued with triggered_by='journey-cascade'.
    # If the journey already has an active run, the partial unique index simply
    # rejects the second claim — that's fine.
    if new_section == "Done" and old_section != "Done":
        _cascade_to_linked_journeys(conn, project_id, ticket_id)

    # Capture commit hash when moving to Done (if not already set)
    if new_section == "Done":
        ticket = conn.execute(
            "SELECT commit_hash FROM tickets WHERE id = ? AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()
        if ticket and not ticket["commit_hash"]:
            # Try to find project path from registry for commit hash capture
            try:
                from db import REGISTRY_PATH
                import json as _json
                registry = _json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
                for p in registry.get("projects", []):
                    if p["id"] == project_id:
                        project_path = os.path.expanduser(p.get("path", ""))
                        commit_hash_val = capture_commit_hash(project_path)
                        if commit_hash_val:
                            conn.execute(
                                "UPDATE tickets SET commit_hash = ? WHERE id = ? AND project_id = ?",
                                (commit_hash_val, ticket_id, project_id),
                            )
                        break
            except Exception:
                pass  # Best-effort — move_ticket already captures this in most paths


def _cascade_to_linked_journeys(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
) -> None:
    """Queue a scenario run for every journey linked to *ticket_id*.

    Best-effort: if kitchen isn't available (e.g. tests not exercising it,
    or the project has no resolvable path), we log and move on. The journey
    table may also not exist in pre-migration DBs.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT journey_id FROM journey_tickets "
            "WHERE ticket_id = ? AND project_id = ?",
            (ticket_id, project_id),
        ).fetchall()
    except sqlite3.OperationalError:
        return  # journey_tickets table doesn't exist
    if not rows:
        return

    # Lazy import to break the actions ↔ kitchen ↔ runners cycle.
    try:
        import kitchen as _kitchen
        from db import get_db
    except ImportError:
        return

    for r in rows:
        jid = r["journey_id"] if "journey_id" in r.keys() else r[0]
        # Each cascade run is a separate trigger_run call so a partial unique
        # index conflict on one journey doesn't block others. trigger_run
        # opens its own connection — passes get_db so it can.
        try:
            _kitchen.trigger_run(
                get_db, project_id, "journey", jid, {},
                triggered_by="journey-cascade",
            )
        except Exception:
            pass  # don't break the section move on a cascade failure



# ---------------------------------------------------------------------------
# Branch operations
# ---------------------------------------------------------------------------

def link_branch(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    branch_name: str,
    remote: str = "origin",
    auto_linked: bool = False,
) -> bool:
    """Link a branch to a ticket.  Returns True if a new link was created."""
    _find_ticket(conn, project_id, ticket_id)  # validate ticket exists
    cur = conn.execute(
        "INSERT OR IGNORE INTO ticket_branches "
        "(ticket_id, project_id, branch_name, remote, auto_linked) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticket_id, project_id, branch_name, remote, int(auto_linked)),
    )
    return cur.rowcount > 0


def unlink_branch(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    branch_name: str,
) -> bool:
    """Unlink a branch from a ticket.  Returns True if a link was removed."""
    cur = conn.execute(
        "DELETE FROM ticket_branches "
        "WHERE ticket_id = ? AND project_id = ? AND branch_name = ?",
        (ticket_id, project_id, branch_name),
    )
    return cur.rowcount > 0


def get_ticket_branches(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
) -> list[dict]:
    """Return all branches linked to a ticket."""
    rows = conn.execute(
        "SELECT * FROM ticket_branches "
        "WHERE ticket_id = ? AND project_id = ? ORDER BY created_at",
        (ticket_id, project_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_project_branches(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[dict]:
    """Return all branch links for a project."""
    rows = conn.execute(
        "SELECT * FROM ticket_branches WHERE project_id = ? ORDER BY ticket_id, created_at",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _match_branch_to_ticket(branch_name: str, ticket_ids: list[str]) -> Optional[str]:
    """Check if branch_name starts with a ticket ID (case-insensitive).

    Handles both direct names (B-01-feature) and path prefixes (feature/B-01-thing).
    Returns the matched ticket ID or None.
    """
    # Strip remote prefix if present
    if "/" in branch_name:
        parts = branch_name.split("/")
        # origin/B-01-feature → B-01-feature
        # origin/feature/B-01-thing → feature/B-01-thing → B-01-thing
        candidates = ["/".join(parts[i:]) for i in range(len(parts))]
    else:
        candidates = [branch_name]

    lower_name_candidates = [c.lower() for c in candidates]

    # Sort ticket IDs longest-first to match BUG-03 before B-0
    for tid in sorted(ticket_ids, key=len, reverse=True):
        tid_lower = tid.lower()
        for cand in lower_name_candidates:
            # Must start with ticket ID followed by end-of-string, dash, or slash
            if cand == tid_lower or cand.startswith(tid_lower + "-") or cand.startswith(tid_lower + "/"):
                return tid
    return None


def scan_branches(
    conn: sqlite3.Connection,
    project_id: str,
    project_path: str,
) -> dict:
    """Scan remote branches and auto-link those matching ticket IDs.

    Returns {"linked": N, "total_remote": N} or {"error": str}.
    """
    if not project_path:
        return {"error": "no project path", "linked": 0, "total_remote": 0}

    # Get remote branch names
    try:
        result = subprocess.run(
            ["git", "branch", "-r", "--list", "origin/*"],
            cwd=project_path, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {"error": "git branch failed", "linked": 0, "total_remote": 0}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {"error": "git not available", "linked": 0, "total_remote": 0}

    branches = []
    for line in result.stdout.strip().splitlines():
        name = line.strip()
        if " -> " in name:
            continue  # skip HEAD pointers like origin/HEAD -> origin/main
        if name:
            branches.append(name)

    # Load all ticket IDs for this project
    ticket_rows = conn.execute(
        "SELECT id FROM tickets WHERE project_id = ?", (project_id,)
    ).fetchall()
    ticket_ids = [r["id"] for r in ticket_rows]

    linked = 0
    for branch in branches:
        # Strip "origin/" for matching but store the short name
        short_name = branch.replace("origin/", "", 1) if branch.startswith("origin/") else branch
        matched_tid = _match_branch_to_ticket(short_name, ticket_ids)
        if matched_tid:
            cur = conn.execute(
                "INSERT OR IGNORE INTO ticket_branches "
                "(ticket_id, project_id, branch_name, remote, auto_linked) "
                "VALUES (?, ?, ?, 'origin', 1)",
                (matched_tid, project_id, short_name),
            )
            if cur.rowcount > 0:
                linked += 1

    # Update ahead/behind for all linked branches
    all_linked = conn.execute(
        "SELECT ticket_id, branch_name FROM ticket_branches WHERE project_id = ?",
        (project_id,),
    ).fetchall()

    for row in all_linked:
        try:
            res = subprocess.run(
                ["git", "rev-list", "--count", "--left-right",
                 f"origin/main...origin/{row['branch_name']}"],
                cwd=project_path, capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0 and "\t" in res.stdout.strip():
                behind_str, ahead_str = res.stdout.strip().split("\t")
                conn.execute(
                    "UPDATE ticket_branches SET ahead = ?, behind = ?, last_synced = ? "
                    "WHERE ticket_id = ? AND project_id = ? AND branch_name = ?",
                    (int(ahead_str), int(behind_str),
                     datetime.now().isoformat(),
                     row["ticket_id"], project_id, row["branch_name"]),
                )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
            pass  # best-effort

    return {"linked": linked, "total_remote": len(branches)}


def scan_prs(
    conn: sqlite3.Connection,
    project_id: str,
    project_path: str,
) -> dict:
    """Enrich branch links with PR metadata via `gh pr list`.

    Returns {"updated": N} or {"error": str}.
    """
    if not project_path:
        return {"error": "no project path", "updated": 0}

    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--json",
             "number,title,headRefName,state,url,isDraft",
             "--limit", "100", "--state", "all"],
            cwd=project_path, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return {"error": "gh pr list failed", "updated": 0}
        prs = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError,
            OSError, ValueError):
        return {"error": "gh not available", "updated": 0}

    # Load ticket IDs for auto-linking unlinked PR branches
    ticket_rows = conn.execute(
        "SELECT id FROM tickets WHERE project_id = ?", (project_id,)
    ).fetchall()
    ticket_ids = [r["id"] for r in ticket_rows]

    updated = 0
    for pr in prs:
        head_ref = pr.get("headRefName", "")
        if not head_ref:
            continue

        pr_number = pr.get("number")
        pr_url = pr.get("url", "")
        state = pr.get("state", "").upper()
        is_draft = pr.get("isDraft", False)

        if state == "MERGED":
            pr_status = "merged"
        elif state == "CLOSED":
            pr_status = "closed"
        elif is_draft:
            pr_status = "draft"
        else:
            pr_status = "open"

        # Try to update existing branch link
        cur = conn.execute(
            "UPDATE ticket_branches SET pr_number = ?, pr_status = ?, pr_url = ?, "
            "last_synced = ? "
            "WHERE project_id = ? AND branch_name = ?",
            (pr_number, pr_status, pr_url,
             datetime.now().isoformat(), project_id, head_ref),
        )
        if cur.rowcount > 0:
            updated += cur.rowcount
            continue

        # Try auto-linking if the branch matches a ticket ID
        matched_tid = _match_branch_to_ticket(head_ref, ticket_ids)
        if matched_tid:
            conn.execute(
                "INSERT OR IGNORE INTO ticket_branches "
                "(ticket_id, project_id, branch_name, remote, auto_linked, "
                "pr_number, pr_status, pr_url, last_synced) "
                "VALUES (?, ?, ?, 'origin', 1, ?, ?, ?, ?)",
                (matched_tid, project_id, head_ref,
                 pr_number, pr_status, pr_url,
                 datetime.now().isoformat()),
            )
            updated += 1

    return {"updated": updated}


# TODO: remove once parent-promote system workflow is verified in production.
# Phase A migration (tidy-newt) replaced this with the "Parent auto-promote"
# system workflow in workflows_seed.py — the dispatcher fires it on each tick
# against parents whose children all reached terminal status. Kept defined for
# safety in case the workflow engine needs to be temporarily disabled.
def _maybe_promote_parent(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
) -> None:
    """DEPRECATED — see Phase A migration note above. No longer called.

    If this ticket has a parent, check whether all siblings are done/review-ready.

    When every child of a parent ticket has status in
    {'for-review', 'bug-fixed', 'done'}, the parent is auto-promoted
    to the For Review section (keeping its current status badge).
    """
    parent_row = conn.execute(
        "SELECT parent FROM tickets WHERE id = ? AND project_id = ?",
        (ticket_id, project_id),
    ).fetchone()
    if not parent_row or not parent_row["parent"]:
        return

    parent_id = parent_row["parent"]

    # Verify parent exists
    parent_ticket = conn.execute(
        "SELECT * FROM tickets WHERE id = ? AND project_id = ?",
        (parent_id, project_id),
    ).fetchone()
    if not parent_ticket:
        return

    # Already in a terminal section — don't demote
    if parent_ticket["section"] in ("Done", "Won't Do"):
        return

    # Gather all children of this parent
    children = conn.execute(
        "SELECT status FROM tickets WHERE parent = ? AND project_id = ?",
        (parent_id, project_id),
    ).fetchall()
    if not children:
        return

    done_statuses = {"for-review", "bug-fixed", "done"}
    if all(c["status"] in done_statuses for c in children):
        old_parent_section = parent_ticket["section"]
        if old_parent_section == "For Review":
            return  # idempotent — already promoted
        sort_order = _next_sort_order(conn, project_id, "For Review")
        conn.execute(
            "UPDATE tickets SET section = 'For Review', sort_order = ?, updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            (sort_order, datetime.now().isoformat(), parent_id, project_id),
        )
        # Internal-side-effect rule (§9): system-actor event for the cascaded promotion.
        emit_event(conn, project_id, "ticket", parent_id, "section_change",
                   {"before": old_parent_section, "after": "For Review"},
                   ActorContext.system())
