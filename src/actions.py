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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from constants import (
    DEFAULT_STATUS_BY_SECTION,
    SECTION_ORDER,
    SECTION_PREFIX,
    SECTION_TO_COLUMN,
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


def _find_ticket(conn: sqlite3.Connection, project_id: str, ticket_id: str) -> sqlite3.Row:
    """Locate a ticket by ID (case-insensitive).  Raises ValueError if not found."""
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
        (ticket_id, project_id),
    ).fetchone()
    if not ticket:
        raise ValueError(f"Ticket '{ticket_id}' not found in project '{project_id}'.")
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


def execute_scheduled_event(conn: sqlite3.Connection, event: sqlite3.Row) -> None:
    """Execute a single scheduled event.  Called by the poller in serve.py.

    Dispatches on event['event_type'].  Currently supported:
      - 'auto-accept': accept the ticket (move to Done + spec entry)
    """
    event_type = event["event_type"]
    ticket_id = event["ticket_id"]
    project_id = event["project_id"]

    if event_type == "auto-accept":
        # Re-check preconditions: ticket still in For Review, status done, no open bugs
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE id = ? AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()
        if not ticket:
            return
        if ticket["section"] != "For Review" or ticket["status"] != "done":
            return
        if _has_open_bugs(conn, project_id, ticket_id):
            return

        # Move to Done
        sort_order = _next_sort_order(conn, project_id, "Done")
        commit_hash_val = ""
        try:
            from db import REGISTRY_PATH
            import json as _json
            registry = _json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
            for p in registry.get("projects", []):
                if p["id"] == project_id:
                    project_path = os.path.expanduser(p.get("path", ""))
                    commit_hash_val = capture_commit_hash(project_path)
                    break
        except Exception:
            pass

        now = datetime.now().isoformat()
        update_fields = "section = 'Done', sort_order = ?, updated_at = ?"
        params: list = [sort_order, now]
        if commit_hash_val and not ticket["commit_hash"]:
            update_fields += ", commit_hash = ?"
            params.append(commit_hash_val)
        params.extend([ticket_id, project_id])
        conn.execute(
            f"UPDATE tickets SET {update_fields} WHERE id = ? AND project_id = ?",
            params,
        )
    # else: unknown event type — silently skip


# ---------------------------------------------------------------------------
# Core ticket operations
# ---------------------------------------------------------------------------

def move_ticket(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    target_section: str,
    project_path: str = "",
) -> str:
    """Move a ticket to *target_section*.

    Uses compute_status_on_move() to decide the new status (preserves the
    current status when it is valid in the target section).  Captures the
    HEAD commit hash when moving to Done.

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

    # Post-change hooks
    _after_section_change(conn, project_id, tid, old_section, target_section)
    _after_status_change(conn, project_id, tid, old_status, new_status)

    return tid


def accept_ticket(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    project_path: str,
    project_name: str,
) -> str:
    """Accept a ticket: move to Done with status 'done' and append to PRODUCT_SPECIFICATION.md.

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

    # Append to PRODUCT_SPECIFICATION.md
    spec_path = Path(project_path) / "PRODUCT_SPECIFICATION.md"
    today = datetime.now().strftime("%Y-%m-%d")
    entry = f"\n### {tid}: {ticket['title']}\n"
    entry += f"Priority: {ticket['priority']} | Complexity: {ticket['complexity']} | Status: released\n"
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
    complexity: str = "M",
    description: str = "",
    parent: Optional[str] = None,
    rationale: str = "",
) -> str:
    """Add a new ticket.  Auto-generates the ID from *section* prefix.

    Returns the new ticket ID.
    """
    status = DEFAULT_STATUS_BY_SECTION[section]
    ticket_id = auto_generate_id(conn, project_id, section)
    sort_order = _next_sort_order(conn, project_id, section)

    conn.execute(
        "INSERT INTO tickets (id, project_id, title, priority, complexity, status, "
        "section, description, parent, rationale, sort_order) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, project_id, title, priority, complexity, status,
         section, description, parent, rationale, sort_order),
    )

    return ticket_id


def update_ticket(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    *,
    title: Optional[str] = None,
    priority: Optional[str] = None,
    complexity: Optional[str] = None,
    status: Optional[str] = None,
    description: Optional[str] = None,
    rationale: Optional[str] = None,
    parent: Optional[str] = ...,  # sentinel — None means "clear parent"
    summary: Optional[str] = None,
    add_criteria: Optional[list[str]] = None,
    check_criteria: Optional[int] = None,
    uncheck_criteria: Optional[int] = None,
    remove_criteria: Optional[int] = None,
    add_depends: Optional[list[str]] = None,
    remove_depends: Optional[list[str]] = None,
) -> str:
    """Partial update of a ticket.  Only fields that are not None/sentinel are changed.

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
    if complexity is not None:
        updates["complexity"] = complexity.upper()
    if status is not None:
        updates["status"] = status.lower()
    if description is not None:
        updates["description"] = description
    if rationale is not None:
        updates["rationale"] = rationale
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
        _update_criterion(conn, tid, project_id, check_criteria, checked=1)

    if uncheck_criteria is not None:
        _update_criterion(conn, tid, project_id, uncheck_criteria, checked=0)

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

    # Post-change hooks (status only — section unchanged by update)
    new_status = updates.get("status", old_status)
    if new_status != old_status:
        _after_status_change(conn, project_id, tid, old_status, new_status)

    return tid


# ---------------------------------------------------------------------------
# Criteria helpers (internal)
# ---------------------------------------------------------------------------

def _update_criterion(
    conn: sqlite3.Connection, tid: str, project_id: str, index: int, checked: int
):
    """Update the checked state of the Nth criterion (1-indexed)."""
    criteria = conn.execute(
        "SELECT id FROM acceptance_criteria "
        "WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
        (tid, project_id),
    ).fetchall()
    if 1 <= index <= len(criteria):
        conn.execute(
            "UPDATE acceptance_criteria SET checked = ? WHERE id = ?",
            (checked, criteria[index - 1]["id"]),
        )
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

def schedule_event(
    conn: sqlite3.Connection,
    event_type: str,
    ticket_id: str,
    project_id: str,
    payload: dict | None = None,
    delay_seconds: int = 300,
) -> int:
    """Insert a scheduled event to fire after *delay_seconds*.

    Returns the new event row id.
    """
    fire_at = (datetime.now() + timedelta(seconds=delay_seconds)).isoformat()
    cur = conn.execute(
        "INSERT INTO scheduled_events (event_type, ticket_id, project_id, payload, fire_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_type, ticket_id, project_id, json.dumps(payload or {}), fire_at),
    )
    return cur.lastrowid


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
    """Run post-status-change side effects."""
    # Promote parent when a child reaches a done-like status
    if new_status in {"done", "for-review", "bug-fixed"}:
        _maybe_promote_parent(conn, project_id, ticket_id)

    # Auto-accept rule: when status becomes "done" while in "For Review"
    # and there are no open bugs, schedule an auto-accept with 5 min delay.
    if new_status == "done":
        ticket = conn.execute(
            "SELECT section FROM tickets WHERE id = ? AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()
        if ticket and ticket["section"] == "For Review":
            if not _has_open_bugs(conn, project_id, ticket_id):
                # Cancel any existing unfired auto-accept for this ticket
                conn.execute(
                    "UPDATE scheduled_events SET fired = 1 "
                    "WHERE ticket_id = ? AND project_id = ? AND event_type = 'auto-accept' AND fired = 0",
                    (ticket_id, project_id),
                )
                schedule_event(
                    conn,
                    event_type="auto-accept",
                    ticket_id=ticket_id,
                    project_id=project_id,
                    delay_seconds=300,  # 5 minutes
                )


def _after_section_change(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    old_section: str,
    new_section: str,
) -> None:
    """Run post-move logic after a ticket changes section."""
    _maybe_promote_parent(conn, project_id, ticket_id)

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


def _maybe_promote_parent(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
) -> None:
    """If this ticket has a parent, check whether all siblings are done/review-ready.

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
        sort_order = _next_sort_order(conn, project_id, "For Review")
        conn.execute(
            "UPDATE tickets SET section = 'For Review', sort_order = ?, updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            (sort_order, datetime.now().isoformat(), parent_id, project_id),
        )
