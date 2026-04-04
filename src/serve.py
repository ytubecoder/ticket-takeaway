#!/usr/bin/env python3
"""Ticket Takeaway Dashboard Server — serves the dashboard with editing API.

Usage:
    python3 serve.py [--port PORT] [--project ID]

Starts an HTTP server at http://localhost:PORT (default 8787) that:
  - GET /              → serves the generated HTML dashboard
  - GET /api/tickets   → JSON ticket data
  - PUT /api/tickets/<id>      → update ticket fields
  - POST /api/tickets/<id>/move → move ticket between sections
"""

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Import tickets-cli.py (hyphenated filename requires importlib)
# ---------------------------------------------------------------------------

_CLI_PATH = Path(__file__).parent / "tickets-cli.py"
if not _CLI_PATH.exists():
    _CLI_PATH = Path.home() / ".claude" / "ticket-takeaway" / "tickets-cli.py"

_spec = importlib.util.spec_from_file_location("tickets_cli", str(_CLI_PATH))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

# Import generate.py
_GEN_PATH = Path(__file__).parent / "generate.py"
if not _GEN_PATH.exists():
    _GEN_PATH = Path.home() / ".claude" / "ticket-takeaway" / "generate.py"

_gen_spec = importlib.util.spec_from_file_location("generate", str(_GEN_PATH))
gen = importlib.util.module_from_spec(_gen_spec)
_gen_spec.loader.exec_module(gen)

# ---------------------------------------------------------------------------
# Direct imports from new modules (prefer these over cli.* where available)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))
from constants import (SECTION_SLUGS, DEFAULT_STATUS_BY_SECTION, SECTION_ORDER,
                       SECTION_PREFIX, STATUSES, VALID_STATUSES_BY_SECTION,
                       compute_status_on_move, DASHBOARD_DIR, DB_PATH, REGISTRY_PATH)
from db import get_db, init_db
from actions import move_ticket as _actions_move_ticket, accept_ticket as _actions_accept_ticket, add_ticket as _actions_add_ticket, update_ticket as _actions_update_ticket, capture_commit_hash, auto_generate_id, execute_scheduled_event

import html as _html

# Registry cache — populated at startup, refreshed on /api/projects mutations
_PROJECTS_CACHE: dict[str, dict] = {}
_PROJECTS_CACHE_LOCK = threading.Lock()

# Global route prefixes that must never be captured as project IDs
_GLOBAL_PREFIXES = frozenset({"api", "settings", "static", "health", "favicon.ico", "index.html", ""})

# Reserved project IDs that cannot be registered
_RESERVED_IDS = frozenset({"api", "settings", "static", "health", "favicon.ico", "index.html"})


def _refresh_projects_cache() -> None:
    """Reload registry.json into the module-level cache. Thread-safe."""
    if not REGISTRY_PATH.exists():
        return
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return
    projects = {p["id"]: p for p in data.get("projects", []) if p.get("active", True)}
    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
        _PROJECTS_CACHE.update(projects)


def _resolve_project_from_path(path: str) -> tuple[dict | None, str]:
    """Extract project from URL prefix. Returns (project_dict, remaining_path).

    /goodform/api/tickets  →  (goodform_project, "/api/tickets")
    /api/projects          →  (None, "/api/projects")
    /settings              →  (None, "/settings")
    /                      →  (None, "/")
    """
    parts = path.split("/", 2)  # ["", "segment", "rest..."]
    if len(parts) >= 2:
        candidate = parts[1]
        if candidate in _GLOBAL_PREFIXES:
            return None, path
        with _PROJECTS_CACHE_LOCK:
            proj = _PROJECTS_CACHE.get(candidate)
        if proj is not None:
            remainder = "/" + parts[2] if len(parts) > 2 else "/"
            return proj, remainder
    return None, path


def _safe_attr(s: str) -> str:
    """Escape string for HTML attribute context."""
    return _html.escape(str(s), quote=True)


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

SERVER_PROJECT_ID = None  # Set from --project arg or auto-detect
SERVER_PORT = 8787

# Lock for DB operations (sqlite3 connections aren't thread-safe)
_db_lock = threading.RLock()  # Reentrant — write functions call _get_ticket_json while holding lock


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _get_all_settings() -> dict:
    """Read all settings as a dict."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        try:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        except Exception:
            rows = []
        conn.close()
    return {r["key"]: r["value"] for r in rows}


def _set_settings(updates: dict) -> None:
    """Upsert multiple settings."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        for k, v in updates.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (k, str(v)),
            )
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Feedbacks detection
# ---------------------------------------------------------------------------

_feedbacks_cache: dict = {"result": None, "expires": 0}


def _detect_feedbacks() -> dict:
    import time

    now = time.time()
    if _feedbacks_cache["result"] and now < _feedbacks_cache["expires"]:
        return _feedbacks_cache["result"]

    from constants import FEEDBACKS_DEFAULT_PORT, FEEDBACKS_REPO_URL, FEEDBACKS_DETECTION_CACHE_TTL

    result = {
        "available": False,
        "running": False,
        "installed": False,
        "home": None,
        "output_dir": None,
        "install_url": FEEDBACKS_REPO_URL,
    }

    settings = _get_all_settings()
    feedbacks_home = settings.get("feedbacks.home", "")

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{FEEDBACKS_DEFAULT_PORT}/config")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            result["running"] = True
            result["available"] = True
            result["installed"] = True
            result["output_dir"] = data.get("outputDir")
    except Exception:
        pass

    if feedbacks_home:
        home_path = Path(os.path.expanduser(feedbacks_home))
        if (home_path / "start.sh").exists():
            result["installed"] = True
            result["available"] = True
            result["home"] = str(home_path)
            if not result["output_dir"]:
                sessions_dir = home_path / "sessions"
                if sessions_dir.exists():
                    result["output_dir"] = str(sessions_dir)

    _feedbacks_cache["result"] = result
    _feedbacks_cache["expires"] = now + FEEDBACKS_DETECTION_CACHE_TTL
    return result


# ---------------------------------------------------------------------------
# Attachment CRUD helpers
# ---------------------------------------------------------------------------

def _list_attachments(project_id: str, ticket_id: str) -> list:
    with _db_lock:
        conn = get_db()
        init_db(conn)
        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return []
        tid = row["id"]
        try:
            rows = conn.execute(
                "SELECT * FROM ticket_attachments WHERE ticket_id = ? AND project_id = ? ORDER BY created_at DESC",
                (tid, project_id),
            ).fetchall()
        except Exception:
            rows = []
        conn.close()
    return [dict(r) for r in rows]


def _add_attachment(project_id, ticket_id, attachment_type, name, path="", summary="", metadata="{}"):
    with _db_lock:
        conn = get_db()
        init_db(conn)
        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return None
        tid = row["id"]
        try:
            conn.execute(
                "INSERT INTO ticket_attachments "
                "(ticket_id, project_id, attachment_type, name, path, summary, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tid, project_id, attachment_type, name, path, summary, metadata),
            )
            conn.commit()
            att = conn.execute(
                "SELECT * FROM ticket_attachments "
                "WHERE ticket_id = ? AND project_id = ? AND name = ? AND attachment_type = ?",
                (tid, project_id, name, attachment_type),
            ).fetchone()
            conn.close()
            return dict(att) if att else None
        except sqlite3.IntegrityError:
            conn.close()
            return None


def _delete_attachment(project_id, ticket_id, attachment_id):
    with _db_lock:
        conn = get_db()
        init_db(conn)
        cur = conn.execute(
            "DELETE FROM ticket_attachments WHERE id = ? AND project_id = ?",
            (attachment_id, project_id),
        )
        conn.commit()
        conn.close()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Triage stub (full implementation in Phase 4)
# ---------------------------------------------------------------------------

def _run_triage(project_id, ticket_id, attachment_id):
    """Stub — full implementation in Phase 4."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        try:
            conn.execute(
                "UPDATE ticket_attachments SET triage_status = 'done', triage_result = '[]' WHERE id = ?",
                (attachment_id,),
            )
            conn.commit()
        except Exception:
            pass
        conn.close()


def _get_project() -> dict:
    """Get the target project dict from registry."""
    projects = cli.load_registry()
    if SERVER_PROJECT_ID:
        return cli.find_project(projects, SERVER_PROJECT_ID)
    return projects[0]


def _update_ticket_field(project_id: str, ticket_id: str, field: str, value) -> bool:
    """Update a single field on a ticket. Returns True on success."""
    ALLOWED_FIELDS = {
        "title", "priority", "complexity", "status", "description",
        "parent", "commit_hash", "release_tag", "draft",
    }
    if field not in ALLOWED_FIELDS:
        return False

    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()
        cli.ingest_markdown(conn, proj)

        # Verify ticket exists
        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        conn.execute(
            f"UPDATE tickets SET {field} = ?, updated_at = ? WHERE id = ? AND project_id = ?",
            (value, datetime.now().isoformat(), tid, project_id)
        )
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _move_ticket(project_id: str, ticket_id: str, section_name: str) -> bool:
    """Move a ticket to a different section. Returns True on success.

    Delegates to actions.move_ticket() which uses compute_status_on_move()
    to preserve valid statuses across section moves.
    """
    try:
        section = cli.resolve_section(section_name)
    except (SystemExit, ValueError):
        return False

    if section not in SECTION_SLUGS:
        return False

    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()
        cli.ingest_markdown(conn, proj)

        project_path = os.path.expanduser(proj.get("path", ""))
        try:
            _actions_move_ticket(conn, project_id, ticket_id, section, project_path=project_path)
        except ValueError:
            conn.close()
            return False

        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _toggle_criterion(project_id: str, ticket_id: str, criterion_index: int) -> bool:
    """Toggle a single acceptance criterion's checked state."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()
        cli.ingest_markdown(conn, proj)

        # Find the criterion by ticket + sort_order
        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        criterion = conn.execute(
            "SELECT id, checked FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC LIMIT 1 OFFSET ?",
            (tid, project_id, criterion_index)
        ).fetchone()
        if not criterion:
            conn.close()
            return False

        new_checked = 0 if criterion["checked"] else 1
        conn.execute(
            "UPDATE acceptance_criteria SET checked = ? WHERE id = ?",
            (new_checked, criterion["id"])
        )
        conn.execute(
            "UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
            (datetime.now().isoformat(), tid, project_id)
        )
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _update_criterion_text(project_id: str, ticket_id: str, criterion_index: int, new_text: str) -> bool:
    """Update the text of a criterion at a given index."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()
        cli.ingest_markdown(conn, proj)

        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        criterion = conn.execute(
            "SELECT id FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC LIMIT 1 OFFSET ?",
            (tid, project_id, criterion_index)
        ).fetchone()
        if not criterion:
            conn.close()
            return False

        conn.execute("UPDATE acceptance_criteria SET text = ? WHERE id = ?", (new_text, criterion["id"]))
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
                     (datetime.now().isoformat(), tid, project_id))
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _remove_criterion(project_id: str, ticket_id: str, criterion_index: int) -> bool:
    """Remove a criterion at a given index."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()
        cli.ingest_markdown(conn, proj)

        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        criterion = conn.execute(
            "SELECT id FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC LIMIT 1 OFFSET ?",
            (tid, project_id, criterion_index)
        ).fetchone()
        if not criterion:
            conn.close()
            return False

        conn.execute("DELETE FROM acceptance_criteria WHERE id = ?", (criterion["id"],))
        # Re-number sort_order
        remaining = conn.execute(
            "SELECT id FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
            (tid, project_id)
        ).fetchall()
        for i, r in enumerate(remaining):
            conn.execute("UPDATE acceptance_criteria SET sort_order = ? WHERE id = ?", (i, r["id"]))

        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
                     (datetime.now().isoformat(), tid, project_id))
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _add_criterion(project_id: str, ticket_id: str, text: str) -> bool:
    """Add a new criterion to the end of the list."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()
        cli.ingest_markdown(conn, proj)

        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
            (tid, project_id)
        ).fetchone()["next_order"]

        conn.execute(
            "INSERT INTO acceptance_criteria (ticket_id, project_id, text, checked, sort_order) VALUES (?,?,?,0,?)",
            (tid, project_id, text, max_order)
        )
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
                     (datetime.now().isoformat(), tid, project_id))
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _update_depends(project_id: str, ticket_id: str, depends_list: list) -> bool:
    """Replace all depends for a ticket with a new list."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()
        cli.ingest_markdown(conn, proj)

        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        conn.execute("DELETE FROM depends WHERE ticket_id = ? AND project_id = ?", (tid, project_id))
        for dep_id in depends_list:
            dep_id = dep_id.strip()
            if dep_id:
                conn.execute(
                    "INSERT OR IGNORE INTO depends (ticket_id, project_id, depends_on_id) VALUES (?,?,?)",
                    (tid, project_id, dep_id)
                )
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
                     (datetime.now().isoformat(), tid, project_id))
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _create_ticket(project_id: str, title: str, body: dict) -> dict | None:
    """Create a new ticket. Returns the ticket JSON on success."""
    section_name = body.get("section", "Ideas")
    try:
        section = cli.resolve_section(section_name)
    except (SystemExit, ValueError):
        return None

    priority = body.get("priority", "medium")
    complexity = body.get("complexity", "M")
    description = body.get("description", "")

    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()
        cli.ingest_markdown(conn, proj)

        ticket_id = _actions_add_ticket(
            conn, project_id, title,
            section=section, priority=priority,
            complexity=complexity, description=description,
        )

        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()

    return _get_ticket_json(project_id, ticket_id)


def _delete_ticket(project_id: str, ticket_id: str) -> bool:
    """Delete a ticket. Returns True on success."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()
        cli.ingest_markdown(conn, proj)

        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        conn.execute("DELETE FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?", (tid, project_id))
        conn.execute("DELETE FROM depends WHERE ticket_id = ? AND project_id = ?", (tid, project_id))
        conn.execute("DELETE FROM tickets WHERE id = ? AND project_id = ?", (tid, project_id))
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _accept_ticket(project_id: str, ticket_id: str) -> bool:
    """Accept a ticket — move to Done with status 'done'. Returns True on success."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()
        cli.ingest_markdown(conn, proj)

        project_path = os.path.expanduser(proj.get("path", ""))
        project_name = proj.get("name", proj.get("id", ""))
        try:
            _actions_accept_ticket(conn, project_id, ticket_id, project_path, project_name)
        except ValueError:
            conn.close()
            return False

        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


VALID_READINESS_FLAGS = {"tests", "reviewed", "smoke"}

# Sections that require a gate check before entry
GATED_SECTIONS = {"Ideas", "Backlog", "WIP", "For Review", "Done"}


def _build_gate_prompt(ticket: dict, target_section: str) -> str:
    """Build the analysis prompt for the gate-check agent."""
    criteria_lines = []
    for c in ticket.get("acceptance_criteria", []):
        mark = "[x]" if c["checked"] else "[ ]"
        criteria_lines.append(f"- {mark} {c['text']}")
    criteria_text = "\n".join(criteria_lines) if criteria_lines else "(none)"
    total = len(ticket.get("acceptance_criteria", []))
    checked = sum(1 for c in ticket.get("acceptance_criteria", []) if c["checked"])

    flags = ticket.get("readiness_flags", [])
    deps = ticket.get("depends", [])
    deps_text = ", ".join(deps) if deps else "none"

    return f"""You are a project management assistant analyzing a ticket column move.

TICKET: {ticket['id']} — {ticket['title']}
MOVE: {ticket['section']} → {target_section}
Priority: {ticket['priority']} | Complexity: {ticket['complexity']} | Status: {ticket['status']}

CURRENT STATE:

[D] DESCRIPTION:
{ticket['description'] or '(empty)'}

[C] ACCEPTANCE CRITERIA ({checked}/{total} complete):
{criteria_text}

[T] TESTS: {'SET' if 'tests' in flags else 'NOT SET'}
[R] REVIEWED: {'SET' if 'reviewed' in flags else 'NOT SET'}
[S] SMOKE TESTED: {'SET' if 'smoke' in flags else 'NOT SET'}

DEPENDENCIES: {deps_text}

TASK: Analyze readiness for moving to {target_section}. For each category (D,C,T,R,S), assess completeness and suggest specific improvements if needed.

Respond with ONLY valid JSON (no markdown fences, no explanation) matching this exact schema:
{{
  "verdict": "ready" or "needs-work" or "blocked",
  "summary": "one-line explanation",
  "categories": {{
    "D": {{ "status": "ok" or "needs-work", "current_summary": "brief state", "suggestion": "improvement or null" }},
    "C": {{ "status": "ok" or "needs-work", "current_summary": "brief", "suggestion": "improvement or null", "add_criteria": ["new criterion 1"] }},
    "T": {{ "status": "ok" or "needs-work", "current_summary": "brief", "suggestion": "improvement or null" }},
    "R": {{ "status": "ok" or "needs-work", "current_summary": "brief", "suggestion": "improvement or null" }},
    "S": {{ "status": "ok" or "needs-work", "current_summary": "brief", "suggestion": "improvement or null" }}
  }}
}}"""


def _run_gate_check(project_id: str, ticket_id: str, target_section: str) -> dict:
    """Run the gate-check agent and return structured analysis."""
    import subprocess as _sp

    ticket = _get_ticket_json(project_id, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    prompt = _build_gate_prompt(ticket, target_section)

    try:
        result = _sp.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=90,
            cwd=os.path.expanduser(_get_project().get("path", "."))
        )
        # --output-format json wraps the response in {"type":"result","result":"..."}
        outer = json.loads(result.stdout)
        text = outer.get("result", result.stdout) if isinstance(outer, dict) else result.stdout
        # The agent's text response should be raw JSON
        analysis = json.loads(text) if isinstance(text, str) else text
    except _sp.TimeoutExpired:
        return {"error": "Gate check timed out", "verdict": "needs-work", "summary": "Analysis timed out — review manually."}
    except (json.JSONDecodeError, KeyError):
        return {"error": "Failed to parse agent response", "verdict": "needs-work", "summary": "Could not parse analysis — review manually."}

    # Attach metadata
    analysis["ticket_id"] = ticket_id
    analysis["target_section"] = target_section
    return analysis


# --------------- Per-category assessment ---------------

_CAT_LABELS = {"D": "Description", "C": "Acceptance Criteria", "T": "Tests", "R": "Review", "S": "Smoke Tests"}


def _build_category_prompt(ticket: dict, category: str, action: str) -> str:
    """Build a focused prompt for a single DCTRS category assessment."""
    cat_label = _CAT_LABELS.get(category, category)
    criteria_lines = []
    for c in ticket.get("acceptance_criteria", []):
        mark = "[x]" if c["checked"] else "[ ]"
        criteria_lines.append(f"- {mark} {c['text']}")
    criteria_text = "\n".join(criteria_lines) if criteria_lines else "(none)"
    flags = ticket.get("readiness_flags", [])

    # Build current-content section based on category
    if category == "D":
        current = ticket.get("description") or "(empty)"
    elif category == "C":
        current = criteria_text
    elif category == "T":
        current = (flags.get("tests") if isinstance(flags, dict) else "") or "(empty)"
    elif category == "R":
        current = (flags.get("reviewed") if isinstance(flags, dict) else "") or "(empty)"
    elif category == "S":
        current = (flags.get("smoke") if isinstance(flags, dict) else "") or "(empty)"
    else:
        current = "(unknown category)"

    if action == "create":
        task_instruction = (
            f"Generate high-quality {cat_label.lower()} content for this ticket. "
            f"Return the generated content in the 'content' field as plain text (not JSON, not markdown fences)."
        )
    else:  # review
        task_instruction = (
            f"Review the existing {cat_label.lower()} for completeness, clarity, and quality. "
            f"Provide specific suggestions for improvement."
        )

    add_criteria_field = ""
    if category == "C":
        add_criteria_field = ', "add_criteria": ["suggested new criterion 1", "..."]'

    return f"""You are a project management assistant assessing a single aspect of a ticket.

TICKET: {ticket['id']} — {ticket['title']}
Priority: {ticket['priority']} | Complexity: {ticket['complexity']} | Status: {ticket['status']}

DESCRIPTION:
{ticket.get('description') or '(empty)'}

ACCEPTANCE CRITERIA:
{criteria_text}

CURRENT {cat_label.upper()} CONTENT:
{current}

TASK: {task_instruction}

Respond with ONLY valid JSON (no markdown fences, no explanation) matching this exact schema:
{{
  "status": "ok" or "needs-work",
  "current_summary": "brief assessment of current state",
  "suggestion": "specific improvement suggestion or null",
  "content": "generated content text (for create action) or null"{add_criteria_field}
}}"""


def _run_category_assess(project_id: str, ticket_id: str, category: str, action: str) -> dict:
    """Run a focused single-category assessment and return structured result."""
    import subprocess as _sp

    ticket = _get_ticket_json(project_id, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    prompt = _build_category_prompt(ticket, category, action)

    try:
        result = _sp.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=45,
            cwd=os.path.expanduser(_get_project().get("path", "."))
        )
        outer = json.loads(result.stdout)
        text = outer.get("result", result.stdout) if isinstance(outer, dict) else result.stdout
        analysis = json.loads(text) if isinstance(text, str) else text
    except _sp.TimeoutExpired:
        return {"error": "Assessment timed out", "status": "needs-work", "current_summary": "Timed out", "suggestion": "Try again."}
    except (json.JSONDecodeError, KeyError):
        return {"error": "Failed to parse response", "status": "needs-work", "current_summary": "Parse error", "suggestion": "Try again."}

    analysis["ticket_id"] = ticket_id
    analysis["category"] = category
    analysis["action"] = action
    return analysis


def _build_enrich_prompt(ticket: dict, field: str, content: str, action: str) -> str:
    """Build a prompt for Claude CLI to enrich/review a single field's content."""
    field_label = {
        "description": "Description",
        "criteria": "Acceptance Criteria",
        "tests": "Tests",
        "reviewed": "Review Notes",
        "smoke": "Smoke Test Plan",
    }.get(field, field)

    criteria_lines = []
    for c in ticket.get("acceptance_criteria", []):
        mark = "[x]" if c["checked"] else "[ ]"
        criteria_lines.append(f"- {mark} {c['text']}")
    criteria_text = "\n".join(criteria_lines) if criteria_lines else "(none)"

    if action == "create":
        task = (
            f"Generate high-quality {field_label.lower()} content for this ticket. "
            f"Return the complete text in the 'suggested' field. Be thorough and specific."
        )
    else:
        task = (
            f"Review and improve the existing {field_label.lower()} content for this ticket. "
            f"Return an improved version in the 'suggested' field. Preserve what is correct, fix gaps and clarity issues."
        )

    return f"""You are a project management assistant improving ticket content.

TICKET: {ticket['id']} — {ticket['title']}
Priority: {ticket['priority']} | Complexity: {ticket['complexity']} | Status: {ticket['status']}

DESCRIPTION:
{ticket.get('description') or '(empty)'}

ACCEPTANCE CRITERIA:
{criteria_text}

CURRENT {field_label.upper()} CONTENT:
{content or '(empty)'}

TASK: {task}

Respond with ONLY valid JSON (no markdown fences, no explanation):
{{
  "original": {json.dumps(content or "")},
  "suggested": "your improved/generated content here as a plain text string"
}}"""


def _compute_diff_hunks(original: str, suggested: str) -> list:
    """Compute line-by-line diff hunks between original and suggested text."""
    import difflib

    orig_lines = original.splitlines() if original else []
    sugg_lines = suggested.splitlines() if suggested else []

    matcher = difflib.SequenceMatcher(None, orig_lines, sugg_lines, autojunk=False)
    hunks = []
    idx = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        elif tag == "replace":
            # Pair up lines where possible, then handle extras
            orig_chunk = orig_lines[i1:i2]
            sugg_chunk = sugg_lines[j1:j2]
            max_len = max(len(orig_chunk), len(sugg_chunk))
            for k in range(max_len):
                o = orig_chunk[k] if k < len(orig_chunk) else None
                s = sugg_chunk[k] if k < len(sugg_chunk) else None
                if o is None:
                    hunks.append({"type": "add", "original": "", "suggested": s, "index": idx})
                elif s is None:
                    hunks.append({"type": "remove", "original": o, "suggested": "", "index": idx})
                else:
                    hunks.append({"type": "modify", "original": o, "suggested": s, "index": idx})
                idx += 1
        elif tag == "delete":
            for line in orig_lines[i1:i2]:
                hunks.append({"type": "remove", "original": line, "suggested": "", "index": idx})
                idx += 1
        elif tag == "insert":
            for line in sugg_lines[j1:j2]:
                hunks.append({"type": "add", "original": "", "suggested": line, "index": idx})
                idx += 1

    return hunks


def _run_enrich(project_id: str, ticket_id: str, field: str, content: str, action: str) -> dict:
    """Run Claude CLI to enrich a single field and return diff hunks."""
    import subprocess as _sp

    ticket = _get_ticket_json(project_id, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    prompt = _build_enrich_prompt(ticket, field, content, action)

    try:
        result = _sp.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=90,
            cwd=os.path.expanduser(_get_project().get("path", "."))
        )
        outer = json.loads(result.stdout)
        text = outer.get("result", result.stdout) if isinstance(outer, dict) else result.stdout
        data = json.loads(text) if isinstance(text, str) else text
    except _sp.TimeoutExpired:
        return {"error": "Enrich timed out — try again."}
    except (json.JSONDecodeError, KeyError) as e:
        return {"error": f"Failed to parse response: {e}"}

    original = data.get("original", content or "")
    suggested = data.get("suggested", "")
    hunks = _compute_diff_hunks(original, suggested)

    return {
        "original": original,
        "suggested": suggested,
        "hunks": hunks,
        "ticket_id": ticket_id,
        "field": field,
        "action": action,
    }


def _toggle_readiness(project_id: str, ticket_id: str, flag: str) -> bool:
    """Toggle a readiness flag. If set, clear it; if unset, set it."""
    if flag not in VALID_READINESS_FLAGS:
        return False

    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()

        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        existing = conn.execute(
            "SELECT flag FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
            (tid, project_id, flag)
        ).fetchone()

        if existing:
            conn.execute(
                "DELETE FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
                (tid, project_id, flag)
            )
        else:
            conn.execute(
                "INSERT INTO readiness_flags (ticket_id, project_id, flag, set_by) VALUES (?, ?, ?, 'dashboard')",
                (tid, project_id, flag)
            )

        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _update_readiness_content(project_id: str, ticket_id: str, flag: str, content: str) -> bool:
    """Update readiness flag content. Non-empty content upserts (auto-fills dot), empty deletes (auto-empties)."""
    if flag not in VALID_READINESS_FLAGS:
        return False

    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()

        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        content = content.strip()

        if content:
            conn.execute("""
                INSERT INTO readiness_flags (ticket_id, project_id, flag, content, set_by)
                VALUES (?, ?, ?, ?, 'dashboard')
                ON CONFLICT (ticket_id, project_id, flag)
                DO UPDATE SET content = excluded.content
            """, (tid, project_id, flag, content))
        else:
            conn.execute(
                "DELETE FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
                (tid, project_id, flag)
            )

        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _get_ticket_json(project_id: str, ticket_id: str) -> dict | None:
    """Get a single ticket as a JSON-serializable dict. Thread-safe."""
    with _db_lock:
        return _get_ticket_json_inner(project_id, ticket_id)


def _get_ticket_json_inner(project_id: str, ticket_id: str) -> dict | None:
    """Inner implementation — caller must hold _db_lock."""
    conn = get_db()
    init_db(conn)
    row = conn.execute(
        "SELECT * FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
        (ticket_id, project_id)
    ).fetchone()
    if not row:
        conn.close()
        return None

    criteria = conn.execute(
        "SELECT text, checked FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
        (row["id"], project_id)
    ).fetchall()
    deps = conn.execute(
        "SELECT depends_on_id FROM depends WHERE ticket_id = ? AND project_id = ?",
        (row["id"], project_id)
    ).fetchall()
    try:
        flags = conn.execute(
            "SELECT flag, content FROM readiness_flags WHERE ticket_id = ? AND project_id = ?",
            (row["id"], project_id)
        ).fetchall()
        readiness_flags = {f["flag"]: f["content"] for f in flags}
    except Exception:
        readiness_flags = {}

    # Attachment count
    try:
        att_count_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM ticket_attachments WHERE ticket_id = ? AND project_id = ?",
            (row["id"], project_id),
        ).fetchone()
        attachment_count = att_count_row["cnt"] if att_count_row else 0
    except Exception:
        attachment_count = 0

    conn.close()

    # Build criteria text for clipboard prompts
    criteria_list = [{"text": c["text"], "checked": bool(c["checked"])} for c in criteria]
    criteria_text = "\n".join(f"- [{'x' if c['checked'] else ' '}] {c['text']}" for c in criteria_list)

    return {
        "id": row["id"],
        "title": row["title"],
        "priority": row["priority"],
        "complexity": row["complexity"],
        "status": row["status"],
        "section": row["section"],
        "description": row["description"],
        "parent": row["parent"],
        "commit_hash": row["commit_hash"] if "commit_hash" in row.keys() else "",
        "release_tag": row["release_tag"] if "release_tag" in row.keys() else "",
        "draft": bool(row["draft"]) if "draft" in row.keys() else False,
        "acceptance_criteria": criteria_list,
        "criteria_text": criteria_text,
        "depends": [d["depends_on_id"] for d in deps],
        "readiness_flags": readiness_flags,
        "attachment_count": attachment_count,
    }


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class DashboardHandler(BaseHTTPRequestHandler):
    """Handle dashboard HTTP requests."""

    def log_message(self, format, *args):
        """Quieter logging — only errors."""
        if args and str(args[0]).startswith(("4", "5")):
            super().log_message(format, *args)

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", f"http://localhost:{SERVER_PORT}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = min(int(self.headers.get("Content-Length", 0)), 1_048_576)  # 1 MB cap
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", f"http://localhost:{SERVER_PORT}")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        # Serve dashboard HTML
        if path == "/" or path == "/index.html":
            proj = _get_project()
            html_path = Path(os.path.expanduser(proj.get("path", ""))) / "docs" / "sdlc-dashboard.html"
            if html_path.exists():
                html = html_path.read_text(encoding="utf-8")
                # Inject edit-api meta tag if not present
                if '<meta name="edit-api"' not in html:
                    # Only inject in <head>, not inside JS strings — replace first occurrence only
                    idx = html.find('<meta name="gen-ts"')
                    if idx != -1:
                        html = html[:idx] + f'<meta name="edit-api" content="http://localhost:{SERVER_PORT}/api">\n' + html[idx:]
                self._send_html(html)
            else:
                self._send_json({"error": "Dashboard not generated yet. Run generate.py first."}, 404)
            return

        # JSON tickets API
        if path == "/api/tickets":
            proj = _get_project()
            project_id = proj["id"]
            conn = get_db()
            init_db(conn)
            rows = conn.execute(
                "SELECT id FROM tickets WHERE project_id = ? ORDER BY sort_order ASC",
                (project_id,)
            ).fetchall()
            tickets = []
            for r in rows:
                t = _get_ticket_json(project_id, r["id"])
                if t:
                    tickets.append(t)
            conn.close()
            self._send_json({"project_id": project_id, "tickets": tickets})
            return

        # Single ticket
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)$", path)
        if m:
            proj = _get_project()
            t = _get_ticket_json(proj["id"], m.group(1))
            if t:
                self._send_json(t)
            else:
                self._send_json({"error": "Ticket not found"}, 404)
            return

        # Settings
        if path == "/api/settings":
            self._send_json(_get_all_settings())
            return

        # Feedbacks status
        if path == "/api/feedbacks/status":
            self._send_json(_detect_feedbacks())
            return

        # Ticket attachments list
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/attachments$", path)
        if m:
            proj = _get_project()
            atts = _list_attachments(proj["id"], m.group(1))
            self._send_json(atts)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path

        # Settings update
        if path == "/api/settings":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            if not isinstance(body, dict):
                self._send_json({"error": "Body must be a JSON object"}, 400)
                return
            _set_settings(body)
            # Invalidate feedbacks cache on settings change
            _feedbacks_cache["result"] = None
            self._send_json({"ok": True})
            return

        # Update readiness flag content
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/readiness/([a-z]+)$", path)
        if m:
            ticket_id = m.group(1)
            flag = m.group(2)
            proj = _get_project()
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            content = body.get("content", "")
            if _update_readiness_content(proj["id"], ticket_id, flag, content):
                t = _get_ticket_json(proj["id"], ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Invalid flag or ticket"}, 400)
            return

        # Update ticket fields
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)$", path)
        if not m:
            self._send_json({"error": "Not found"}, 404)
            return

        ticket_id = m.group(1)
        proj = _get_project()
        project_id = proj["id"]

        try:
            body = self._read_body()
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if not body:
            self._send_json({"error": "Empty body"}, 400)
            return

        # Handle adding a new acceptance criterion (from gate-check panel)
        if "add_criteria" in body:
            text = body["add_criteria"]
            if isinstance(text, str) and text.strip():
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    proj2 = _get_project()
                    cli.ingest_markdown(conn, proj2)
                    row = conn.execute(
                        "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
                        (ticket_id, project_id)
                    ).fetchone()
                    if row:
                        tid = row["id"]
                        sort_row = conn.execute(
                            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
                            (tid, project_id)
                        ).fetchone()
                        conn.execute(
                            "INSERT INTO acceptance_criteria (ticket_id, project_id, text, checked, sort_order) VALUES (?, ?, ?, 0, ?)",
                            (tid, project_id, text.strip(), sort_row["next_order"])
                        )
                        conn.commit()
                        cli.sync_to_markdown(conn, proj2)
                        cli.regenerate_dashboard(proj2)
                    conn.close()
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
                return

        # Handle criterion toggle specially
        if "toggle_criterion" in body:
            idx = body["toggle_criterion"]
            if isinstance(idx, int) and _toggle_criterion(project_id, ticket_id, idx):
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to toggle criterion"}, 400)
            return

        # Handle criterion text update
        if "criterion_index" in body and "criterion_text" in body:
            idx = body["criterion_index"]
            text = body["criterion_text"]
            if isinstance(idx, int) and isinstance(text, str) and _update_criterion_text(project_id, ticket_id, idx, text):
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to update criterion text"}, 400)
            return

        # Handle criterion removal
        if "remove_criterion" in body:
            idx = body["remove_criterion"]
            if isinstance(idx, int) and _remove_criterion(project_id, ticket_id, idx):
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to remove criterion"}, 400)
            return

        # Handle add criterion
        if "add_criteria" in body:
            text = body["add_criteria"]
            if isinstance(text, str) and text.strip() and _add_criterion(project_id, ticket_id, text.strip()):
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to add criterion"}, 400)
            return

        # Handle depends list update
        if "depends" in body:
            deps = body["depends"]
            if isinstance(deps, list) and _update_depends(project_id, ticket_id, deps):
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to update depends"}, 400)
            return

        # Update individual fields
        for field, value in body.items():
            if not _update_ticket_field(project_id, ticket_id, field, value):
                self._send_json({"error": f"Failed to update field: {field}"}, 400)
                return

        # Return updated ticket
        t = _get_ticket_json(project_id, ticket_id)
        self._send_json(t or {"ok": True})

    def do_POST(self):
        path = urlparse(self.path).path

        # Move ticket
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/move$", path)
        if m:
            ticket_id = m.group(1)
            proj = _get_project()
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            section = body.get("section", "")
            if not section:
                self._send_json({"error": "Missing 'section' field"}, 400)
                return

            if _move_ticket(proj["id"], ticket_id, section):
                t = _get_ticket_json(proj["id"], ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to move ticket"}, 400)
            return

        # AI-powered field enrichment with diff hunks
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/enrich$", path)
        if m:
            ticket_id = m.group(1)
            proj = _get_project()
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            field = body.get("field", "")
            content = body.get("content", "")
            action = body.get("action", "review")

            valid_fields = {"description", "criteria", "tests", "reviewed", "smoke"}
            if field not in valid_fields:
                self._send_json({"error": f"field must be one of: {', '.join(sorted(valid_fields))}"}, 400)
                return
            if action not in ("create", "review"):
                self._send_json({"error": "action must be 'create' or 'review'"}, 400)
                return

            result = _run_enrich(proj["id"], ticket_id, field, content, action)
            if "error" in result and "hunks" not in result:
                self._send_json(result, 400)
            else:
                self._send_json(result)
            return

        # Gate check before column move
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/gate-check$", path)
        if m:
            ticket_id = m.group(1)
            proj = _get_project()
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            section = body.get("section", "")
            if not section:
                self._send_json({"error": "Missing 'section' field"}, 400)
                return

            result = _run_gate_check(proj["id"], ticket_id, section)
            if "error" in result and "verdict" not in result:
                self._send_json(result, 400)
            else:
                self._send_json(result)
            return

        # Per-category assessment
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/assess/([DCTRS])$", path)
        if m:
            ticket_id = m.group(1)
            category = m.group(2)
            proj = _get_project()
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            action = body.get("action", "review")
            if action not in ("create", "review"):
                self._send_json({"error": "action must be 'create' or 'review'"}, 400)
                return

            result = _run_category_assess(proj["id"], ticket_id, category, action)
            if "error" in result and "status" not in result:
                self._send_json(result, 400)
            else:
                self._send_json(result)
            return

        # Toggle readiness flag
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/readiness/([a-z]+)$", path)
        if m:
            ticket_id = m.group(1)
            flag = m.group(2)
            proj = _get_project()
            if _toggle_readiness(proj["id"], ticket_id, flag):
                t = _get_ticket_json(proj["id"], ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Invalid flag or ticket"}, 400)
            return

        # Accept ticket (move to Done + append to PRODUCT_SPECIFICATION.md)
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/accept$", path)
        if m:
            ticket_id = m.group(1)
            proj = _get_project()
            if _accept_ticket(proj["id"], ticket_id):
                t = _get_ticket_json(proj["id"], ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to accept ticket"}, 400)
            return

        # Create ticket
        if path == "/api/tickets":
            proj = _get_project()
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            title = body.get("title", "").strip()
            if not title:
                self._send_json({"error": "Missing 'title' field"}, 400)
                return

            result = _create_ticket(proj["id"], title, body)
            if result:
                self._send_json(result, 201)
            else:
                self._send_json({"error": "Failed to create ticket"}, 400)
            return

        # Add attachment to ticket
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/attachments$", path)
        if m:
            ticket_id = m.group(1)
            proj = _get_project()
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            att = _add_attachment(
                proj["id"],
                ticket_id,
                attachment_type=body.get("attachment_type", "feedbacks"),
                name=body.get("name", ""),
                path=body.get("path", ""),
                summary=body.get("summary", ""),
                metadata=body.get("metadata", "{}"),
            )
            if att:
                self._send_json(att, 201)
            else:
                self._send_json({"error": "Failed to add attachment"}, 400)
            return

        # Feedbacks callback — receive session, create attachment, start triage
        if path == "/api/feedbacks/callback":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            ticket_id = body.get("ticket_id", "")
            session_name = body.get("session_name", "")
            session_path = body.get("session_path", "")
            if not ticket_id or not session_name:
                self._send_json({"error": "ticket_id and session_name required"}, 400)
                return
            proj = _get_project()
            att = _add_attachment(
                proj["id"],
                ticket_id,
                attachment_type="feedbacks",
                name=session_name,
                path=session_path,
                summary=body.get("summary", ""),
                metadata=json.dumps({k: v for k, v in body.items() if k not in ("ticket_id",)}),
            )
            if att:
                att_id = att["id"]
                t = threading.Thread(
                    target=_run_triage,
                    args=(proj["id"], ticket_id, att_id),
                    daemon=True,
                )
                t.start()
                self._send_json({"ok": True, "attachment_id": att_id})
            else:
                self._send_json({"error": "Failed to record session"}, 400)
            return

        # Record — returns URL to open feedbacks recorder for a ticket
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/record$", path)
        if m:
            ticket_id = m.group(1)
            from constants import FEEDBACKS_DEFAULT_PORT
            callback_url = f"http://localhost:{SERVER_PORT}/api/feedbacks/callback"
            record_url = (
                f"http://localhost:{FEEDBACKS_DEFAULT_PORT}/"
                f"?ticket={ticket_id}&callback={callback_url}&mode=recorder"
            )
            self._send_json({"url": record_url})
            return

        # Start feedbacks server
        if path == "/api/settings/feedbacks/start":
            status = _detect_feedbacks()
            home = status.get("home")
            if not home:
                self._send_json({"error": "feedbacks.home not configured or start.sh not found"}, 400)
                return
            start_sh = Path(home) / "start.sh"
            try:
                subprocess.Popen(
                    ["bash", str(start_sh)],
                    cwd=home,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Invalidate cache so next detection re-checks
                _feedbacks_cache["result"] = None
                self._send_json({"ok": True, "message": "feedbacks start.sh launched"})
            except Exception as e:
                self._send_json({"error": f"Failed to start feedbacks: {e}"}, 500)
            return

        # Install feedbacks via git clone
        if path == "/api/settings/feedbacks/install":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                body = {}
            from constants import FEEDBACKS_REPO_URL
            install_dir = body.get("install_dir", str(Path.home() / "projects" / "feedbacks"))
            repo_url = body.get("repo_url", FEEDBACKS_REPO_URL)
            try:
                subprocess.Popen(
                    ["git", "clone", repo_url, install_dir],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._send_json({"ok": True, "message": f"git clone started → {install_dir}"})
            except Exception as e:
                self._send_json({"error": f"Failed to clone feedbacks: {e}"}, 500)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path

        # Delete attachment
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/attachments/(\d+)$", path)
        if m:
            ticket_id = m.group(1)
            attachment_id = int(m.group(2))
            proj = _get_project()
            if _delete_attachment(proj["id"], ticket_id, attachment_id):
                self._send_json({"ok": True, "deleted": attachment_id})
            else:
                self._send_json({"error": "Attachment not found"}, 404)
            return

        # Delete ticket
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)$", path)
        if not m:
            self._send_json({"error": "Not found"}, 404)
            return

        ticket_id = m.group(1)
        proj = _get_project()
        if _delete_ticket(proj["id"], ticket_id):
            self._send_json({"ok": True, "deleted": ticket_id})
        else:
            self._send_json({"error": "Ticket not found"}, 404)


# ---------------------------------------------------------------------------
# Background watcher for external markdown edits
# ---------------------------------------------------------------------------

def _start_external_edit_watcher(project: dict, interval: float = 5.0):
    """Start a daemon thread that polls for external PRODUCT_BACKLOG.md edits.

    Every *interval* seconds it computes the file hash and compares with the
    stored hash in ``_sync_state``.  Only when the hash differs does it parse
    the markdown and merge deltas — keeping the hot-path cheap.
    """
    import time

    def _poll():
        while True:
            try:
                time.sleep(interval)
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    changed = cli.detect_external_edits(conn, project)
                    if changed:
                        cli.regenerate_dashboard(project)
                        print(f"[watcher] External edits absorbed for {project.get('id', '?')}")
                    conn.close()
            except Exception as exc:
                # Don't crash the watcher on transient errors
                print(f"[watcher] Error: {exc}")

    t = threading.Thread(target=_poll, daemon=True, name="md-edit-watcher")
    t.start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _start_scheduled_event_poller(project: dict, interval: float = 30.0):
    """Start a daemon thread that polls scheduled_events every *interval* seconds.

    Picks up unfired events whose fire_at <= now, executes the action via
    execute_scheduled_event(), marks them fired, and syncs markdown/dashboard.
    Each event is wrapped in its own try/except so one bad event cannot kill
    the poller.
    """
    import time

    def _poll():
        while True:
            try:
                time.sleep(interval)
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    now = datetime.now().isoformat()
                    due = conn.execute(
                        "SELECT * FROM scheduled_events WHERE fired = 0 AND fire_at <= ? "
                        "ORDER BY fire_at ASC",
                        (now,),
                    ).fetchall()
                    executed_any = False
                    for event in due:
                        try:
                            execute_scheduled_event(conn, event)
                            conn.execute(
                                "UPDATE scheduled_events SET fired = 1 WHERE id = ?",
                                (event["id"],),
                            )
                            executed_any = True
                        except Exception as exc:
                            # Mark as fired to avoid retry-looping on permanently broken events
                            conn.execute(
                                "UPDATE scheduled_events SET fired = 1 WHERE id = ?",
                                (event["id"],),
                            )
                            import traceback
                            traceback.print_exc()
                    if executed_any:
                        conn.commit()
                        cli.sync_to_markdown(conn, project)
                        cli.regenerate_dashboard(project)
                    conn.close()
            except Exception:
                import traceback
                traceback.print_exc()

    t = threading.Thread(target=_poll, daemon=True, name="scheduled-event-poller")
    t.start()


def main():
    global SERVER_PROJECT_ID, SERVER_PORT

    args = sys.argv[1:]
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            SERVER_PORT = int(args[idx + 1])
    if "--project" in args:
        idx = args.index("--project")
        if idx + 1 < len(args):
            SERVER_PROJECT_ID = args[idx + 1]

    # Auto-detect project from cwd
    if not SERVER_PROJECT_ID:
        cwd = os.path.realpath(os.getcwd())
        try:
            for proj in cli.load_registry():
                proj_path = os.path.realpath(os.path.expanduser(proj.get("path", "")))
                if cwd == proj_path or cwd.startswith(proj_path + os.sep):
                    SERVER_PROJECT_ID = proj["id"]
                    break
        except SystemExit:
            pass

    # Regenerate dashboard before starting
    try:
        proj = _get_project()
        print(f"Serving: {proj.get('name', proj.get('id', 'unknown'))}")
    except (SystemExit, IndexError):
        print("No project found. Run from a registered project directory or use --project ID.", file=sys.stderr)
        sys.exit(1)

    # Start background threads
    _start_external_edit_watcher(proj)
    _start_scheduled_event_poller(proj)

    server = ThreadingHTTPServer(("127.0.0.1", SERVER_PORT), DashboardHandler)
    url = f"http://localhost:{SERVER_PORT}"
    print(f"Dashboard server: {url}")
    print("Press Ctrl+C to stop.\n")

    # Open in browser
    import platform
    import subprocess
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", url])
        elif system == "Linux":
            subprocess.Popen(["xdg-open", url], stderr=subprocess.DEVNULL)
        elif system == "Windows":
            os.startfile(url)
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
