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

_LEGACY_PROJECT_ID = None  # Set from --project arg for backward compat
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

    settings = _get_all_settings()
    feedbacks_home = settings.get("feedbacks.home", "")
    feedbacks_enabled = settings.get("feedbacks.enabled", "").lower() in ("true", "1", "yes")

    result = {
        "available": False,
        "running": False,
        "installed": False,
        "enabled": feedbacks_enabled,
        "home": None,
        "output_dir": None,
        "install_url": FEEDBACKS_REPO_URL,
    }

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

    # Only cache positive running state; when not running, re-check each time
    # so polling during startup gets a fresh answer
    if result["running"]:
        _feedbacks_cache["result"] = result
        _feedbacks_cache["expires"] = now + FEEDBACKS_DETECTION_CACHE_TTL
    else:
        _feedbacks_cache["result"] = None
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
    result = [dict(r) for r in rows]
    # Enrich feedbacks attachments with player/thumbnail URLs
    from constants import FEEDBACKS_DEFAULT_PORT
    for att in result:
        if att.get("attachment_type") == "feedbacks" and att.get("name"):
            base = f"http://localhost:{FEEDBACKS_DEFAULT_PORT}/sessions/{att['name']}"
            att["player_url"] = f"{base}/player.html"
            att["thumbnail_url"] = f"{base}/images/001.png"
    return result


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

def _update_ticket_field(proj: dict, ticket_id: str, field: str, value) -> bool:
    """Update a single field on a ticket. Returns True on success."""
    project_id = proj["id"]
    ALLOWED_FIELDS = {
        "title", "priority", "complexity", "status", "description",
        "parent", "commit_hash", "release_tag", "draft",
    }
    if field not in ALLOWED_FIELDS:
        return False

    with _db_lock:
        conn = get_db()
        init_db(conn)
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


def _move_ticket(proj: dict, ticket_id: str, section_name: str) -> bool:
    """Move a ticket to a different section. Returns True on success.

    Delegates to actions.move_ticket() which uses compute_status_on_move()
    to preserve valid statuses across section moves.
    """
    project_id = proj["id"]
    try:
        section = cli.resolve_section(section_name)
    except (SystemExit, ValueError):
        return False

    if section not in SECTION_SLUGS:
        return False

    with _db_lock:
        conn = get_db()
        init_db(conn)
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


def _toggle_criterion(proj: dict, ticket_id: str, criterion_index: int) -> bool:
    """Toggle a single acceptance criterion's checked state."""
    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
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


def _update_criterion_text(proj: dict, ticket_id: str, criterion_index: int, new_text: str) -> bool:
    """Update the text of a criterion at a given index."""
    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
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


def _remove_criterion(proj: dict, ticket_id: str, criterion_index: int) -> bool:
    """Remove a criterion at a given index."""
    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
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


def _add_criterion(proj: dict, ticket_id: str, text: str) -> bool:
    """Add a new criterion to the end of the list."""
    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
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


def _update_depends(proj: dict, ticket_id: str, depends_list: list) -> bool:
    """Replace all depends for a ticket with a new list."""
    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
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


def _create_ticket(proj: dict, title: str, body: dict) -> dict | None:
    """Create a new ticket. Returns the ticket JSON on success."""
    project_id = proj["id"]
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


def _delete_ticket(proj: dict, ticket_id: str) -> bool:
    """Delete a ticket. Returns True on success."""
    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
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


def _accept_ticket(proj: dict, ticket_id: str) -> bool:
    """Accept a ticket — move to Done with status 'done'. Returns True on success."""
    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
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


def _run_gate_check(proj: dict, ticket_id: str, target_section: str) -> dict:
    """Run the gate-check agent and return structured analysis."""
    import subprocess as _sp

    project_id = proj["id"]
    ticket = _get_ticket_json(project_id, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    prompt = _build_gate_prompt(ticket, target_section)

    try:
        result = _sp.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=90,
            cwd=os.path.expanduser(proj.get("path", "."))
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


def _run_category_assess(proj: dict, ticket_id: str, category: str, action: str) -> dict:
    """Run a focused single-category assessment and return structured result."""
    import subprocess as _sp

    project_id = proj["id"]
    ticket = _get_ticket_json(project_id, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    prompt = _build_category_prompt(ticket, category, action)

    try:
        result = _sp.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=45,
            cwd=os.path.expanduser(proj.get("path", "."))
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


def _run_enrich(proj: dict, ticket_id: str, field: str, content: str, action: str) -> dict:
    """Run Claude CLI to enrich a single field and return diff hunks."""
    import subprocess as _sp

    project_id = proj["id"]
    ticket = _get_ticket_json(project_id, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    prompt = _build_enrich_prompt(ticket, field, content, action)

    try:
        result = _sp.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=90,
            cwd=os.path.expanduser(proj.get("path", "."))
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


def _toggle_readiness(proj: dict, ticket_id: str, flag: str) -> bool:
    """Toggle a readiness flag. If set, clear it; if unset, set it."""
    project_id = proj["id"]
    if flag not in VALID_READINESS_FLAGS:
        return False

    with _db_lock:
        conn = get_db()
        init_db(conn)

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


def _update_readiness_content(proj: dict, ticket_id: str, flag: str, content: str) -> bool:
    """Update readiness flag content. Non-empty content upserts (auto-fills dot), empty deletes (auto-empties)."""
    project_id = proj["id"]
    if flag not in VALID_READINESS_FLAGS:
        return False

    with _db_lock:
        conn = get_db()
        init_db(conn)

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
# Project picker renderer
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$')


def _validate_project_registration(body: dict) -> str | None:
    """Validate project registration fields. Returns error string or None."""
    pid = body.get("id", "")
    if not _SLUG_RE.match(pid):
        return "id must be 2-40 chars, lowercase alphanumeric and hyphens"
    if pid in _RESERVED_IDS:
        return f"'{pid}' is a reserved name and cannot be used as a project ID"
    path = body.get("path", "")
    if not path:
        return "path is required"
    resolved = Path(os.path.realpath(os.path.expanduser(path)))
    if not resolved.is_dir():
        return "path does not exist or is not a directory"
    home = Path.home().resolve()
    try:
        resolved.relative_to(home)
    except ValueError:
        return "path must be within the user's home directory"
    if resolved == home:
        return "path cannot be the home directory itself"
    claude_dir = (home / ".claude").resolve()
    if resolved == claude_dir or str(resolved).startswith(str(claude_dir) + os.sep):
        return "path cannot be inside ~/.claude"
    with _PROJECTS_CACHE_LOCK:
        if pid in _PROJECTS_CACHE:
            return f"project '{pid}' already exists"
    return None


def _render_project_settings(proj: dict, port: int) -> str:
    """Render the settings page for a single project."""
    pid = _safe_attr(proj["id"])
    name = _safe_attr(proj.get("name", proj["id"]))
    path = _safe_attr(proj.get("path", ""))
    description = _safe_attr(proj.get("description", ""))
    active = proj.get("active", True)

    return f'''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} — Settings</title>
<script>
(function(){{
  var s=localStorage.getItem('tt-theme');
  if(s==='light')document.documentElement.setAttribute('data-theme','light');
  else if(s==='dark')document.documentElement.setAttribute('data-theme','dark');
  else document.documentElement.setAttribute('data-theme',
    window.matchMedia('(prefers-color-scheme:light)').matches?'light':'dark');
}})();
</script>
<style>
:root, [data-theme="dark"] {{
  --bg-page: #0c0c0e; --bg-surface: #151518; --bg-card: #1b1b20; --bg-hover: #232329;
  --border-subtle: #1f1f26; --border-default: #2c2c35; --border-strong: #3c3c47;
  --text-primary: #eaeaed; --text-secondary: #9e9eab; --text-tertiary: #6a6a76;
  --accent: #3b82f6;
}}
[data-theme="light"] {{
  --bg-page: #f8f9fa; --bg-surface: #ffffff; --bg-card: #ffffff; --bg-hover: #f3f4f6;
  --border-subtle: #e5e7eb; --border-default: #d1d5db; --border-strong: #9ca3af;
  --text-primary: #111827; --text-secondary: #6b7280; --text-tertiary: #9ca3af;
  --accent: #2563eb;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: var(--bg-page); color: var(--text-primary); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 32px; max-width: 600px; }}
.back {{ color: var(--text-tertiary); text-decoration: none; font-size: 13px; }}
.back:hover {{ color: var(--text-secondary); }}
h1 {{ font-size: 18px; font-weight: 600; margin: 16px 0 24px; }}
label {{ display: block; color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; margin-top: 20px; }}
input, textarea {{ width: 100%; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 6px; padding: 8px 12px; color: var(--text-primary); font-size: 14px; font-family: inherit; }}
input:focus, textarea:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }}
input[readonly] {{ background: var(--bg-page); color: var(--text-tertiary); border-color: var(--border-subtle); }}
textarea {{ min-height: 60px; resize: vertical; }}
.toggle-wrap {{ display: flex; align-items: center; gap: 10px; margin-top: 20px; }}
.toggle {{ width: 36px; height: 20px; border-radius: 10px; cursor: pointer; position: relative; transition: background 0.15s; border: none; }}
.toggle.on {{ background: #22c55e; }}
.toggle.off {{ background: var(--border-default); }}
.toggle::after {{ content: ''; position: absolute; width: 16px; height: 16px; background: white; border-radius: 50%; top: 2px; transition: left 0.15s; }}
.toggle.on::after {{ left: 18px; }}
.toggle.off::after {{ left: 2px; }}
.btn {{ display: inline-block; margin-top: 24px; padding: 8px 20px; background: rgba(59,130,246,0.15); border: 1px solid rgba(59,130,246,0.3); color: var(--accent); border-radius: 6px; cursor: pointer; font-size: 13px; }}
.btn:hover {{ background: rgba(59,130,246,0.25); }}
.danger {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--border-default); }}
.danger h3 {{ color: #ef4444; font-size: 12px; font-weight: 600; margin-bottom: 12px; }}
.danger .btn {{ background: rgba(239,68,68,0.15); border-color: rgba(239,68,68,0.3); color: #ef4444; margin-top: 0; }}
.danger .btn:hover {{ background: rgba(239,68,68,0.25); }}
.danger p {{ color: var(--text-tertiary); font-size: 11px; margin-top: 6px; }}
.msg {{ font-size: 12px; margin-top: 8px; display: none; }}
.msg.ok {{ color: #22c55e; }}
.msg.err {{ color: #ef4444; }}
</style>
</head>
<body>
<a href="/{pid}" class="back" data-testid="settings-back">&larr; Back to board</a>
<h1>{name} Settings</h1>
<form id="settings-form" data-testid="project-settings-form">
  <label>Project Name</label>
  <input name="name" value="{name}" data-testid="settings-name">
  <label>Project Path</label>
  <input name="path" value="{path}" style="font-family:monospace" data-testid="settings-path">
  <label>Description</label>
  <textarea name="description" data-testid="settings-description">{description}</textarea>
  <label>Project ID <span style="color:var(--text-tertiary)">(read-only)</span></label>
  <input name="id" value="{pid}" readonly data-testid="settings-id">
  <div class="toggle-wrap">
    <button type="button" class="toggle {'on' if active else 'off'}" id="active-toggle" data-testid="settings-active-toggle"></button>
    <span style="font-size:13px">Active</span>
  </div>
  <button type="submit" class="btn" data-testid="settings-save">Save Changes</button>
  <div class="msg" id="save-msg"></div>
</form>
<div class="danger">
  <h3>Danger Zone</h3>
  <button class="btn" id="remove-btn" data-testid="settings-remove">Remove Project</button>
  <p>Removes from registry only. Does not delete files, tickets, or database entries.</p>
</div>
<script>
(function() {{
  var activeOn = {'true' if active else 'false'};
  var toggle = document.getElementById('active-toggle');
  toggle.addEventListener('click', function() {{
    activeOn = !activeOn;
    toggle.className = 'toggle ' + (activeOn ? 'on' : 'off');
  }});
  var form = document.getElementById('settings-form');
  var msg = document.getElementById('save-msg');
  form.addEventListener('submit', function(e) {{
    e.preventDefault();
    msg.style.display = 'none';
    fetch('/api/projects/{pid}', {{
      method: 'PUT',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        name: form.elements.name.value,
        path: form.elements.path.value,
        description: form.elements.description.value,
        active: activeOn
      }})
    }}).then(function(r) {{ return r.json().then(function(j) {{ return {{ok: r.ok, data: j}}; }}); }})
    .then(function(res) {{
      if (res.ok) {{ msg.textContent = 'Saved!'; msg.className = 'msg ok'; }}
      else {{ msg.textContent = res.data.error || 'Failed'; msg.className = 'msg err'; }}
      msg.style.display = 'block';
    }});
  }});
  var modal = document.getElementById('confirm-modal');
  var modalCancel = document.getElementById('modal-cancel');
  var modalConfirm = document.getElementById('modal-confirm');
  document.getElementById('remove-btn').addEventListener('click', function() {{
    modal.style.display = 'flex';
  }});
  modalCancel.addEventListener('click', function() {{ modal.style.display = 'none'; }});
  modal.addEventListener('click', function(e) {{ if (e.target === modal) modal.style.display = 'none'; }});
  modalConfirm.addEventListener('click', function() {{
    modal.style.display = 'none';
    fetch('/api/projects/{pid}', {{ method: 'DELETE' }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.ok) window.location.href = '/';
      else {{ msg.textContent = data.error || 'Failed to remove'; msg.className = 'msg err'; msg.style.display = 'block'; }}
    }});
  }});
}})();
</script>
<div id="confirm-modal" style="display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px);align-items:center;justify-content:center;">
  <div style="background:var(--bg-card);border:1px solid var(--border-default);border-radius:12px;padding:24px;max-width:400px;width:90vw;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
    <h3 style="font-size:14px;font-weight:600;margin-bottom:8px;">Remove Project</h3>
    <p style="font-size:13px;color:var(--text-secondary);margin-bottom:20px;">Remove this project from the registry? Tickets and files will not be deleted.</p>
    <div style="display:flex;gap:8px;justify-content:flex-end;">
      <button id="modal-cancel" style="font-size:12px;padding:6px 16px;border-radius:6px;border:1px solid var(--border-default);background:none;color:var(--text-secondary);cursor:pointer;font-family:inherit;">Cancel</button>
      <button id="modal-confirm" style="font-size:12px;padding:6px 16px;border-radius:6px;border:none;background:rgba(239,68,68,0.15);color:#ef4444;cursor:pointer;font-weight:600;font-family:inherit;">Remove</button>
    </div>
  </div>
</div>
</body>
</html>'''


def _render_project_picker(port: int) -> str:
    """Render the project picker page as self-contained HTML."""
    conn = get_db()
    init_db(conn)
    counts_by_project = {}
    rows = conn.execute(
        "SELECT project_id, section, COUNT(*) as cnt FROM tickets GROUP BY project_id, section"
    ).fetchall()
    for r in rows:
        pid = r["project_id"]
        if pid not in counts_by_project:
            counts_by_project[pid] = {}
        counts_by_project[pid][r["section"]] = r["cnt"]
    conn.close()

    with _PROJECTS_CACHE_LOCK:
        projects = list(_PROJECTS_CACHE.values())

    cards_html = ""
    for proj in projects:
        pid = proj["id"]
        name = _safe_attr(proj.get("name", pid))
        raw_path = proj.get("path", "")
        display_path = _safe_attr(raw_path.replace(str(Path.home()), "~"))
        counts = counts_by_project.get(pid, {})
        wip = counts.get("WIP", 0)
        backlog = counts.get("Backlog", 0)
        review = counts.get("For Review", 0)
        path_exists = Path(os.path.expanduser(raw_path)).is_dir() if raw_path else False
        opacity = "1" if path_exists else "0.5"

        cards_html += f'''
        <a href="/{_safe_attr(pid)}" class="proj-card" style="opacity:{opacity}" data-testid="proj-card-{_safe_attr(pid)}">
          <div class="proj-card-name">{name}</div>
          <div class="proj-card-path">{display_path}</div>
          <div class="proj-card-counts">
            <span class="count-wip">{wip} WIP</span>
            <span class="count-backlog">{backlog} Backlog</span>
            <span class="count-review">{review} Review</span>
          </div>
          {'' if path_exists else '<div class="proj-card-warn">Path not found</div>'}
        </a>'''

    return f'''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ticket Takeaway</title>
<script>
(function(){{
  var s=localStorage.getItem('tt-theme');
  if(s==='light')document.documentElement.setAttribute('data-theme','light');
  else if(s==='dark')document.documentElement.setAttribute('data-theme','dark');
  else document.documentElement.setAttribute('data-theme',
    window.matchMedia('(prefers-color-scheme:light)').matches?'light':'dark');
}})();
</script>
<style>
:root, [data-theme="dark"] {{
  --bg-page: #0c0c0e; --bg-surface: #151518; --bg-card: #1b1b20; --bg-hover: #232329;
  --border-subtle: #1f1f26; --border-default: #2c2c35; --border-strong: #3c3c47;
  --text-primary: #eaeaed; --text-secondary: #9e9eab; --text-tertiary: #6a6a76;
  --accent: #3b82f6;
}}
[data-theme="light"] {{
  --bg-page: #f8f9fa; --bg-surface: #ffffff; --bg-card: #ffffff; --bg-hover: #f3f4f6;
  --border-subtle: #e5e7eb; --border-default: #d1d5db; --border-strong: #9ca3af;
  --text-primary: #111827; --text-secondary: #6b7280; --text-tertiary: #9ca3af;
  --accent: #2563eb;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: var(--bg-page); color: var(--text-primary); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 32px; }}
.header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 32px; padding-bottom: 16px; border-bottom: 1px solid var(--border-default); }}
.header h1 {{ font-size: 20px; font-weight: 600; }}
.header .count {{ color: var(--text-tertiary); font-size: 13px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; max-width: 900px; }}
.proj-card {{ display: block; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 10px; padding: 20px; text-decoration: none; color: inherit; transition: border-color 0.15s, background 0.15s; }}
.proj-card:hover {{ border-color: var(--accent); background: var(--bg-hover); }}
.proj-card-name {{ font-size: 16px; font-weight: 600; color: var(--text-primary); margin-bottom: 6px; }}
.proj-card-path {{ font-size: 12px; color: var(--text-tertiary); font-family: monospace; margin-bottom: 12px; }}
.proj-card-counts {{ display: flex; gap: 12px; font-size: 12px; }}
.count-wip {{ color: #f59e0b; }} .count-backlog {{ color: #3b82f6; }} .count-review {{ color: #ec4899; }}
.proj-card-warn {{ color: #ef4444; font-size: 11px; margin-top: 8px; }}
.add-card {{ display: flex; align-items: center; justify-content: center; background: transparent; border: 2px dashed var(--border-default); border-radius: 10px; padding: 20px; min-height: 110px; cursor: pointer; transition: border-color 0.15s; color: var(--text-tertiary); }}
.add-card:hover {{ border-color: var(--accent); }}
.add-card-inner {{ text-align: center; }}
.add-card-plus {{ font-size: 24px; color: var(--text-tertiary); }}
.add-card-label {{ font-size: 13px; margin-top: 4px; }}
.add-form {{ display: none; background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 10px; padding: 20px; margin-top: 16px; max-width: 500px; }}
.add-form.visible {{ display: block; }}
.add-form label {{ display: block; color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; margin-top: 14px; }}
.add-form label:first-child {{ margin-top: 0; }}
.add-form input {{ width: 100%; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 6px; padding: 8px 12px; color: var(--text-primary); font-size: 14px; }}
.add-form input:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }}
.add-form .btn {{ display: inline-block; margin-top: 16px; padding: 8px 20px; background: rgba(59,130,246,0.15); border: 1px solid rgba(59,130,246,0.3); color: var(--accent); border-radius: 6px; cursor: pointer; font-size: 13px; }}
.add-form .btn:hover {{ background: rgba(59,130,246,0.25); }}
.add-form .error {{ color: #ef4444; font-size: 12px; margin-top: 8px; display: none; }}
</style>
</head>
<body>
<div class="header">
  <h1>Ticket Takeaway</h1>
  <span class="count">{len(projects)} project{"s" if len(projects) != 1 else ""} registered</span>
</div>
<div class="grid">
  {cards_html}
  <div class="add-card" onclick="document.getElementById('add-form').classList.toggle('visible')" data-testid="add-project-card">
    <div class="add-card-inner">
      <div class="add-card-plus">+</div>
      <div class="add-card-label">Add Project</div>
    </div>
  </div>
</div>
<form id="add-form" class="add-form" data-testid="add-project-form">
  <label>Project Name</label>
  <input name="name" placeholder="My Project" required data-testid="add-project-name">
  <label>Project Path</label>
  <input name="path" placeholder="~/projects/my-project" required data-testid="add-project-path">
  <label>Project ID <span style="color:var(--text-tertiary)">(auto-generated from name)</span></label>
  <input name="id" placeholder="my-project" data-testid="add-project-id">
  <label>Description <span style="color:var(--text-tertiary)">(optional)</span></label>
  <input name="description" placeholder="Brief description">
  <button type="submit" class="btn">Add Project</button>
  <div class="error" id="add-error"></div>
</form>
<script>
(function() {{
  var nameInput = document.querySelector('[name="name"]');
  var idInput = document.querySelector('[name="id"]');
  if (nameInput && idInput) {{
    nameInput.addEventListener('input', function() {{
      if (!idInput.dataset.manual) {{
        idInput.value = nameInput.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      }}
    }});
    idInput.addEventListener('input', function() {{ idInput.dataset.manual = '1'; }});
  }}
  var form = document.getElementById('add-form');
  var errorDiv = document.getElementById('add-error');
  if (form) {{
    form.addEventListener('submit', function(e) {{
      e.preventDefault();
      errorDiv.style.display = 'none';
      var data = {{
        id: form.elements.id.value || form.elements.name.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''),
        name: form.elements.name.value,
        path: form.elements.path.value,
        description: form.elements.description.value
      }};
      fetch('/api/projects', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(data)
      }}).then(function(r) {{ return r.json().then(function(j) {{ return {{ok: r.ok, data: j}}; }}); }})
      .then(function(res) {{
        if (res.ok) {{ window.location.href = '/' + res.data.id; }}
        else {{ errorDiv.textContent = res.data.error || 'Failed'; errorDiv.style.display = 'block'; }}
      }}).catch(function(err) {{ errorDiv.textContent = err.message; errorDiv.style.display = 'block'; }});
    }});
  }}
}})();
</script>
</body>
</html>'''


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
        proj, remainder = _resolve_project_from_path(path)

        # ── Global routes (proj is None) ────────────────────────────
        if proj is None:
            # Legacy backward compat: --project flag redirects bare /api/ routes
            if _LEGACY_PROJECT_ID and remainder.startswith("/api/"):
                self.send_response(301)
                self.send_header("Location", f"/{_LEGACY_PROJECT_ID}{remainder}")
                self.end_headers()
                return

            # Root: project picker page
            if remainder == "/" or remainder == "":
                html = _render_project_picker(SERVER_PORT)
                self._send_html(html)
                return

            # GET /api/projects — list all projects with ticket counts
            if remainder == "/api/projects":
                with _PROJECTS_CACHE_LOCK:
                    projects_list = list(_PROJECTS_CACHE.values())
                conn = get_db()
                init_db(conn)
                counts_rows = conn.execute(
                    "SELECT project_id, section, COUNT(*) as cnt FROM tickets GROUP BY project_id, section"
                ).fetchall()
                conn.close()
                counts_map = {}
                for r in counts_rows:
                    counts_map.setdefault(r["project_id"], {})[r["section"]] = r["cnt"]
                result = []
                for p in projects_list:
                    c = counts_map.get(p["id"], {})
                    result.append({
                        "id": p["id"], "name": p.get("name", p["id"]),
                        "path": p.get("path", ""), "description": p.get("description", ""),
                        "active": p.get("active", True),
                        "ticket_counts": {"wip": c.get("WIP", 0), "backlog": c.get("Backlog", 0), "review": c.get("For Review", 0)}
                    })
                self._send_json({"projects": result})
                return

            self._send_json({"error": "Not found"}, 404)
            return

        # ── Project-scoped routes ────────────────────────────────────

        # Project settings page
        if remainder == "/settings":
            html = _render_project_settings(proj, SERVER_PORT)
            self._send_html(html)
            return

        # Serve dashboard HTML
        if remainder == "/" or remainder == "/index.html":
            html_path = Path(os.path.expanduser(proj.get("path", ""))) / "docs" / "sdlc-dashboard.html"
            if html_path.exists():
                html = html_path.read_text(encoding="utf-8")
                # Inject edit-api meta tag if not present
                if '<meta name="edit-api"' not in html:
                    idx = html.find('<meta name="gen-ts"')
                    if idx != -1:
                        with _PROJECTS_CACHE_LOCK:
                            proj_list = [{"id": p["id"], "name": p.get("name", p["id"])} for p in _PROJECTS_CACHE.values()]
                        projects_json = json.dumps(proj_list)
                        injection = (
                            f'<meta name="edit-api" content="http://localhost:{SERVER_PORT}/{_safe_attr(proj["id"])}/api">\n'
                            f'<meta name="current-project" content="{_safe_attr(proj["id"])}">\n'
                            f"<meta name=\"projects-list\" content='{_safe_attr(projects_json)}'>\n"
                        )
                        html = html[:idx] + injection + html[idx:]
                self._send_html(html)
            else:
                self._send_json({"error": "Dashboard not generated yet. Run generate.py first."}, 404)
            return

        # JSON tickets API
        if remainder == "/api/tickets":
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
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)$", remainder)
        if m:
            t = _get_ticket_json(proj["id"], m.group(1))
            if t:
                self._send_json(t)
            else:
                self._send_json({"error": "Ticket not found"}, 404)
            return

        # Settings
        if remainder == "/api/settings":
            self._send_json(_get_all_settings())
            return

        # Feedbacks status
        if remainder == "/api/feedbacks/status":
            self._send_json(_detect_feedbacks())
            return

        # Ticket attachments list
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/attachments$", remainder)
        if m:
            atts = _list_attachments(proj["id"], m.group(1))
            self._send_json(atts)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path
        proj, remainder = _resolve_project_from_path(path)

        # ── Global routes ───────────────────────────────────────────
        if proj is None:
            m = re.match(r"^/api/projects/([a-z0-9][a-z0-9-]*[a-z0-9])$", remainder)
            if m:
                pid = m.group(1)
                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError):
                    self._send_json({"error": "Invalid JSON"}, 400)
                    return
                try:
                    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                        registry = json.load(f)
                except (json.JSONDecodeError, IOError):
                    self._send_json({"error": "Registry not found"}, 500)
                    return
                found = False
                updated_entry = None
                for entry in registry["projects"]:
                    if entry["id"] == pid:
                        for field in ("name", "path", "description", "active"):
                            if field in body:
                                entry[field] = body[field]
                        found = True
                        updated_entry = entry
                        break
                if not found:
                    self._send_json({"error": "Project not found"}, 404)
                    return
                with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                    json.dump(registry, f, indent=2)
                _refresh_projects_cache()
                self._send_json(updated_entry)
                return

            if _LEGACY_PROJECT_ID and remainder.startswith("/api/"):
                self.send_response(301)
                self.send_header("Location", f"/{_LEGACY_PROJECT_ID}{remainder}")
                self.end_headers()
                return
            self._send_json({"error": "Not found"}, 404)
            return

        # ── Project-scoped routes ────────────────────────────────────

        # Settings update
        if remainder == "/api/settings":
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
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/readiness/([a-z]+)$", remainder)
        if m:
            ticket_id = m.group(1)
            flag = m.group(2)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            content = body.get("content", "")
            if _update_readiness_content(proj, ticket_id, flag, content):
                t = _get_ticket_json(proj["id"], ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Invalid flag or ticket"}, 400)
            return

        # Update ticket fields
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)$", remainder)
        if not m:
            self._send_json({"error": "Not found"}, 404)
            return

        ticket_id = m.group(1)
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
                    cli.ingest_markdown(conn, proj)
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
                        cli.sync_to_markdown(conn, proj)
                        cli.regenerate_dashboard(proj)
                    conn.close()
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
                return

        # Handle criterion toggle specially
        if "toggle_criterion" in body:
            idx = body["toggle_criterion"]
            if isinstance(idx, int) and _toggle_criterion(proj, ticket_id, idx):
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to toggle criterion"}, 400)
            return

        # Handle criterion text update
        if "criterion_index" in body and "criterion_text" in body:
            idx = body["criterion_index"]
            text = body["criterion_text"]
            if isinstance(idx, int) and isinstance(text, str) and _update_criterion_text(proj, ticket_id, idx, text):
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to update criterion text"}, 400)
            return

        # Handle criterion removal
        if "remove_criterion" in body:
            idx = body["remove_criterion"]
            if isinstance(idx, int) and _remove_criterion(proj, ticket_id, idx):
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to remove criterion"}, 400)
            return

        # Handle add criterion
        if "add_criteria" in body:
            text = body["add_criteria"]
            if isinstance(text, str) and text.strip() and _add_criterion(proj, ticket_id, text.strip()):
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to add criterion"}, 400)
            return

        # Handle depends list update
        if "depends" in body:
            deps = body["depends"]
            if isinstance(deps, list) and _update_depends(proj, ticket_id, deps):
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to update depends"}, 400)
            return

        # Update individual fields
        for field, value in body.items():
            if not _update_ticket_field(proj, ticket_id, field, value):
                self._send_json({"error": f"Failed to update field: {field}"}, 400)
                return

        # Return updated ticket
        t = _get_ticket_json(project_id, ticket_id)
        self._send_json(t or {"ok": True})

    def do_POST(self):
        path = urlparse(self.path).path
        proj, remainder = _resolve_project_from_path(path)

        # ── Global routes ───────────────────────────────────────────
        if proj is None:
            if remainder == "/api/projects":
                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError):
                    self._send_json({"error": "Invalid JSON"}, 400)
                    return
                error = _validate_project_registration(body)
                if error:
                    self._send_json({"error": error}, 400)
                    return
                new_project = {
                    "id": body["id"],
                    "name": body.get("name", body["id"]),
                    "path": body["path"],
                    "description": body.get("description", ""),
                    "active": True,
                }
                try:
                    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                        registry = json.load(f)
                except (json.JSONDecodeError, IOError):
                    registry = {"projects": []}
                registry["projects"].append(new_project)
                with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                    json.dump(registry, f, indent=2)
                _refresh_projects_cache()
                conn = get_db()
                init_db(conn)
                conn.close()
                self._send_json(new_project, 201)
                return

            if _LEGACY_PROJECT_ID and remainder.startswith("/api/"):
                self.send_response(301)
                self.send_header("Location", f"/{_LEGACY_PROJECT_ID}{remainder}")
                self.end_headers()
                return
            self._send_json({"error": "Not found"}, 404)
            return

        # ── Project-scoped routes ────────────────────────────────────

        # Move ticket
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/move$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            section = body.get("section", "")
            if not section:
                self._send_json({"error": "Missing 'section' field"}, 400)
                return

            if _move_ticket(proj, ticket_id, section):
                t = _get_ticket_json(proj["id"], ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to move ticket"}, 400)
            return

        # AI-powered field enrichment with diff hunks
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/enrich$", remainder)
        if m:
            ticket_id = m.group(1)
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

            result = _run_enrich(proj, ticket_id, field, content, action)
            if "error" in result and "hunks" not in result:
                self._send_json(result, 400)
            else:
                self._send_json(result)
            return

        # Gate check before column move
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/gate-check$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            section = body.get("section", "")
            if not section:
                self._send_json({"error": "Missing 'section' field"}, 400)
                return

            result = _run_gate_check(proj, ticket_id, section)
            if "error" in result and "verdict" not in result:
                self._send_json(result, 400)
            else:
                self._send_json(result)
            return

        # Per-category assessment
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/assess/([DCTRS])$", remainder)
        if m:
            ticket_id = m.group(1)
            category = m.group(2)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            action = body.get("action", "review")
            if action not in ("create", "review"):
                self._send_json({"error": "action must be 'create' or 'review'"}, 400)
                return

            result = _run_category_assess(proj, ticket_id, category, action)
            if "error" in result and "status" not in result:
                self._send_json(result, 400)
            else:
                self._send_json(result)
            return

        # Toggle readiness flag
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/readiness/([a-z]+)$", remainder)
        if m:
            ticket_id = m.group(1)
            flag = m.group(2)
            if _toggle_readiness(proj, ticket_id, flag):
                t = _get_ticket_json(proj["id"], ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Invalid flag or ticket"}, 400)
            return

        # Accept ticket (move to Done + append to PRODUCT_SPECIFICATION.md)
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/accept$", remainder)
        if m:
            ticket_id = m.group(1)
            if _accept_ticket(proj, ticket_id):
                t = _get_ticket_json(proj["id"], ticket_id)
                self._send_json(t or {"ok": True})
            else:
                self._send_json({"error": "Failed to accept ticket"}, 400)
            return

        # Create ticket
        if remainder == "/api/tickets":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            title = body.get("title", "").strip()
            if not title:
                self._send_json({"error": "Missing 'title' field"}, 400)
                return

            result = _create_ticket(proj, title, body)
            if result:
                self._send_json(result, 201)
            else:
                self._send_json({"error": "Failed to create ticket"}, 400)
            return

        # Add attachment to ticket
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/attachments$", remainder)
        if m:
            ticket_id = m.group(1)
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
        if remainder == "/api/feedbacks/callback":
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
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/record$", remainder)
        if m:
            ticket_id = m.group(1)
            from constants import FEEDBACKS_DEFAULT_PORT
            callback_url = f"http://localhost:{SERVER_PORT}/{proj['id']}/api/feedbacks/callback"
            record_url = (
                f"http://localhost:{FEEDBACKS_DEFAULT_PORT}/"
                f"?ticket={ticket_id}&callback={callback_url}&mode=recorder"
            )
            self._send_json({"url": record_url})
            return

        # Start feedbacks server
        if remainder == "/api/settings/feedbacks/start":
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
        if remainder == "/api/settings/feedbacks/install":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                body = {}
            from constants import FEEDBACKS_REPO_URL
            install_dir = body.get("install_dir", str(Path.home() / "projects" / "feedbacks"))
            repo_url = body.get("repo_url", FEEDBACKS_REPO_URL)
            # Validate install_dir is within home directory
            resolved_dir = Path(os.path.realpath(os.path.expanduser(install_dir)))
            home = Path.home().resolve()
            try:
                resolved_dir.relative_to(home)
            except ValueError:
                self._send_json({"error": "install_dir must be within home directory"}, 400)
                return
            # Validate repo_url is a trusted HTTPS source
            ALLOWED_REPO_PREFIXES = ("https://github.com/", "https://gitlab.com/")
            if not any(repo_url.startswith(p) for p in ALLOWED_REPO_PREFIXES):
                self._send_json({"error": "repo_url must be a GitHub or GitLab HTTPS URL"}, 400)
                return
            try:
                subprocess.Popen(
                    ["git", "clone", repo_url, str(resolved_dir)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Save the install path to settings so detection finds it
                _set_settings({"feedbacks.home": install_dir})
                _feedbacks_cache["result"] = None
                self._send_json({"ok": True, "message": f"git clone started → {install_dir}", "install_dir": install_dir})
            except Exception as e:
                self._send_json({"error": f"Failed to clone feedbacks: {e}"}, 500)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        proj, remainder = _resolve_project_from_path(path)

        # ── Global routes ───────────────────────────────────────────
        if proj is None:
            m = re.match(r"^/api/projects/([a-z0-9][a-z0-9-]*[a-z0-9])$", remainder)
            if m:
                pid = m.group(1)
                try:
                    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                        registry = json.load(f)
                except (json.JSONDecodeError, IOError):
                    self._send_json({"error": "Registry not found"}, 500)
                    return
                found = False
                for entry in registry["projects"]:
                    if entry["id"] == pid:
                        entry["active"] = False
                        found = True
                        break
                if not found:
                    self._send_json({"error": "Project not found"}, 404)
                    return
                with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                    json.dump(registry, f, indent=2)
                _refresh_projects_cache()
                self._send_json({"ok": True, "deactivated": pid})
                return

            if _LEGACY_PROJECT_ID and remainder.startswith("/api/"):
                self.send_response(301)
                self.send_header("Location", f"/{_LEGACY_PROJECT_ID}{remainder}")
                self.end_headers()
                return
            self._send_json({"error": "Not found"}, 404)
            return

        # ── Project-scoped routes ────────────────────────────────────

        # Delete attachment
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/attachments/(\d+)$", remainder)
        if m:
            ticket_id = m.group(1)
            attachment_id = int(m.group(2))
            if _delete_attachment(proj["id"], ticket_id, attachment_id):
                self._send_json({"ok": True, "deleted": attachment_id})
            else:
                self._send_json({"error": "Attachment not found"}, 404)
            return

        # Delete ticket
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)$", remainder)
        if not m:
            self._send_json({"error": "Not found"}, 404)
            return

        ticket_id = m.group(1)
        if _delete_ticket(proj, ticket_id):
            self._send_json({"ok": True, "deleted": ticket_id})
        else:
            self._send_json({"error": "Ticket not found"}, 404)


# ---------------------------------------------------------------------------
# Background watcher for external markdown edits
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Feedbacks session watcher — polls output dir for new sessions
# ---------------------------------------------------------------------------

_session_watcher_known: set = set()  # session dir names already processed


def _start_feedbacks_session_watcher(interval: float = 3.0):
    """Daemon thread watching feedbacks output dir for new completed sessions."""
    import time

    def _poll():
        global _session_watcher_known
        # Initial snapshot: populate known sessions so we don't import old ones
        _seed_known_sessions()

        while True:
            try:
                time.sleep(interval)
                status = _detect_feedbacks()
                if not status.get("enabled") or not status.get("output_dir"):
                    continue

                output_dir = Path(status["output_dir"])
                if not output_dir.is_dir():
                    continue

                # Scan for new session directories
                for entry in output_dir.iterdir():
                    if not entry.is_dir():
                        continue
                    if entry.name in _session_watcher_known:
                        continue
                    if not entry.name.startswith("feedbacks-"):
                        continue

                    # Check for meta.json (written last in save sequence)
                    meta_file = entry / "meta.json"
                    if not meta_file.exists():
                        continue

                    # New completed session found
                    _session_watcher_known.add(entry.name)
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        continue

                    ticket_id = meta.get("ticketId", "").strip()
                    if not ticket_id:
                        continue

                    # Find which project this ticket belongs to
                    project_id = _find_project_for_ticket(ticket_id)
                    if not project_id:
                        continue

                    # Check if attachment already exists (idempotent)
                    existing = _list_attachments(project_id, ticket_id)
                    if any(a.get("name") == entry.name for a in existing):
                        continue

                    # Create attachment
                    summary = f"Feedback session: {meta.get('duration', '?')}, {meta.get('imageCount', 0)} screenshots, {meta.get('sttCount', 0)} transcripts"
                    att = _add_attachment(
                        project_id=project_id,
                        ticket_id=ticket_id,
                        attachment_type="feedbacks",
                        name=entry.name,
                        path=str(entry.resolve()),
                        summary=summary,
                        metadata=json.dumps(meta),
                    )
                    if att:
                        print(f"[feedbacks-watcher] Linked session {entry.name} → {ticket_id}")

            except Exception:
                import traceback
                traceback.print_exc()

    t = threading.Thread(target=_poll, daemon=True, name="feedbacks-session-watcher")
    t.start()


def _seed_known_sessions():
    """Populate known sessions from existing attachments + output dir scan."""
    global _session_watcher_known
    # Add all existing attachment names
    try:
        with _db_lock:
            conn = get_db()
            init_db(conn)
            rows = conn.execute(
                "SELECT name FROM ticket_attachments WHERE attachment_type = 'feedbacks'"
            ).fetchall()
            conn.close()
        _session_watcher_known = {r["name"] for r in rows}
    except Exception:
        _session_watcher_known = set()

    # Also add everything currently in the output dir so we don't re-import old sessions
    status = _detect_feedbacks()
    output_dir = status.get("output_dir")
    if output_dir:
        p = Path(output_dir)
        if p.is_dir():
            for entry in p.iterdir():
                if entry.is_dir() and entry.name.startswith("feedbacks-"):
                    _session_watcher_known.add(entry.name)


def _find_project_for_ticket(ticket_id: str) -> str | None:
    """Find which project a ticket belongs to."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        row = conn.execute(
            "SELECT project_id FROM tickets WHERE UPPER(id) = UPPER(?)",
            (ticket_id,),
        ).fetchone()
        conn.close()
    return row["project_id"] if row else None


def _start_external_edit_watcher(interval: float = 5.0):
    """Daemon thread polling for external PRODUCT_BACKLOG.md edits across all projects."""
    import time

    def _poll():
        while True:
            try:
                time.sleep(interval)
                with _PROJECTS_CACHE_LOCK:
                    snapshot = list(_PROJECTS_CACHE.values())
                for project in snapshot:
                    try:
                        with _db_lock:
                            conn = get_db()
                            init_db(conn)
                            changed = cli.detect_external_edits(conn, project)
                            if changed:
                                cli.regenerate_dashboard(project)
                                print(f"[watcher] External edits absorbed for {project.get('id', '?')}")
                            conn.close()
                    except Exception as exc:
                        print(f"[watcher] Error for {project.get('id', '?')}: {exc}")
            except Exception:
                import traceback
                traceback.print_exc()

    t = threading.Thread(target=_poll, daemon=True, name="md-edit-watcher")
    t.start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _start_scheduled_event_poller(interval: float = 30.0):
    """Daemon thread executing scheduled events across all projects."""
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
                    projects_to_sync = set()
                    for event in due:
                        try:
                            execute_scheduled_event(conn, event)
                            conn.execute(
                                "UPDATE scheduled_events SET fired = 1 WHERE id = ?",
                                (event["id"],),
                            )
                            projects_to_sync.add(event["project_id"])
                        except Exception:
                            conn.execute(
                                "UPDATE scheduled_events SET fired = 1 WHERE id = ?",
                                (event["id"],),
                            )
                            import traceback
                            traceback.print_exc()
                    if projects_to_sync:
                        conn.commit()
                        with _PROJECTS_CACHE_LOCK:
                            cache_snap = dict(_PROJECTS_CACHE)
                        for pid in projects_to_sync:
                            proj = cache_snap.get(pid)
                            if proj:
                                cli.sync_to_markdown(conn, proj)
                                cli.regenerate_dashboard(proj)
                    conn.close()
            except Exception:
                import traceback
                traceback.print_exc()

    t = threading.Thread(target=_poll, daemon=True, name="scheduled-event-poller")
    t.start()


def main():
    global _LEGACY_PROJECT_ID, SERVER_PORT

    args = sys.argv[1:]
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            SERVER_PORT = int(args[idx + 1])
    if "--project" in args:
        idx = args.index("--project")
        if idx + 1 < len(args):
            _LEGACY_PROJECT_ID = args[idx + 1]

    # Populate registry cache
    _refresh_projects_cache()

    if not _PROJECTS_CACHE:
        print("No active projects in registry. Register a project first.", file=sys.stderr)
        sys.exit(1)

    project_names = [p.get("name", p["id"]) for p in _PROJECTS_CACHE.values()]
    print(f"Serving {len(project_names)} project(s): {', '.join(project_names)}")

    # Start background threads
    _start_external_edit_watcher()
    _start_scheduled_event_poller()
    _start_feedbacks_session_watcher()

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
