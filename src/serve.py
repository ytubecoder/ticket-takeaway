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
import time
import urllib.request
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs

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
                       compute_status_on_move, DASHBOARD_DIR, DB_PATH, REGISTRY_PATH,
                       WORKFLOW_AGENT_TIMEOUT, WORKFLOW_RUN_STATUSES)
from db import get_db, init_db
from actions import (
    move_ticket as _actions_move_ticket,
    accept_ticket as _actions_accept_ticket,
    add_ticket as _actions_add_ticket,
    update_ticket as _actions_update_ticket,
    capture_commit_hash,
    auto_generate_id,
    execute_scheduled_event,
    # Kitchen (M1a)
    ActorContext,
    eligibility as _kitchen_eligibility,
    set_automation_mode as _kitchen_set_mode,
    set_no_test_required as _kitchen_set_ntr,
)
from scenarios import discover_scenarios
from journeys import (
    add_journey, update_journey, delete_journey, list_journeys, get_journey,
    add_step, update_step, delete_step, reorder_steps,
    compile_to_manifest, store_run_results,
    link_ticket, unlink_ticket, infer_journeys,
    export_to_json, import_from_manifest,
)
from page_scraper import scan_all_screens, scans_to_json
from scenario_drafting import DraftRequest, DraftContext, generate_drafts, KNOWN_TESTIDS

import html as _html


def _auto_export_journey(*args, **kwargs):
    """Stub — full implementation on journeys branch."""
    pass


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

# Scenario run tracking
_scenario_runs: dict[str, dict] = {}  # run_id -> {status, scenario_id, process, output_dir, started_at}
_scenario_runs_lock = threading.Lock()

# Workflow bounce tracking
_workflow_runs: dict[str, dict] = {}
_workflow_runs_lock = threading.Lock()


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
# Managed files
# ---------------------------------------------------------------------------

_MANAGED_FILES = [
    ("PRODUCT_BACKLOG.md", "Ticket backlog \u2014 auto-regenerated from DB on every write", False),
    ("PRODUCT_SPECIFICATION.md", "Accepted feature specs \u2014 append-only on /accept", False),
    ("docs/sdlc-dashboard.html", "Visual dashboard snapshot", True),
    ("docs/features/", "Per-feature working files (ephemeral)", True),
    (".feedbacks/", "Feedbacks session recordings", True),
]


def _get_managed_files(project: dict) -> list[dict]:
    """Return list of files/dirs managed by Ticket Takeaway in a project."""
    project_path = os.path.expanduser(project.get("path", ""))
    result = []
    for rel_path, description, gitignored in _MANAGED_FILES:
        full = os.path.join(project_path, rel_path)
        result.append({
            "path": rel_path,
            "description": description,
            "exists": os.path.exists(full),
            "gitignored": gitignored,
        })
    return result


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
            att = conn.execute(
                "SELECT * FROM ticket_attachments "
                "WHERE ticket_id = ? AND project_id = ? AND name = ? AND attachment_type = ?",
                (tid, project_id, name, attachment_type),
            ).fetchone()
            # M1b: attachment_added event in same tx as INSERT
            from actions import emit_event as _emit, ActorContext as _AC
            _emit(conn, project_id, "ticket", tid, "attachment_added",
                  {"attachment_id": att["id"] if att else None,
                   "kind": attachment_type, "label": name},
                  _AC.human())
            conn.commit()
            conn.close()
            return dict(att) if att else None
        except sqlite3.IntegrityError:
            conn.close()
            return None


def _delete_attachment(project_id, ticket_id, attachment_id):
    with _db_lock:
        conn = get_db()
        init_db(conn)
        # Snapshot for the audit event before we delete.
        att = conn.execute(
            "SELECT ticket_id, attachment_type, name FROM ticket_attachments "
            "WHERE id = ? AND project_id = ?",
            (attachment_id, project_id),
        ).fetchone()
        cur = conn.execute(
            "DELETE FROM ticket_attachments WHERE id = ? AND project_id = ?",
            (attachment_id, project_id),
        )
        if cur.rowcount > 0 and att:
            from actions import emit_event as _emit, ActorContext as _AC
            _emit(conn, project_id, "ticket", att["ticket_id"], "attachment_removed",
                  {"attachment_id": attachment_id,
                   "kind": att["attachment_type"], "label": att["name"]},
                  _AC.human())
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

        # Capture the before-value so the audit event is invertable.
        row = conn.execute(
            f"SELECT id, {field} FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        before_val = row[field]
        if before_val == value:
            # No-op write — skip the SQL and the event so we don't pollute history.
            conn.close()
            return True

        conn.execute(
            f"UPDATE tickets SET {field} = ?, updated_at = ? WHERE id = ? AND project_id = ?",
            (value, datetime.now().isoformat(), tid, project_id)
        )
        # M1b: emit_event in same tx. status changes stay on the M1a status_change
        # event; everything else is field_changed.
        from actions import emit_event as _emit, ActorContext as _AC
        if field == "status":
            _emit(conn, project_id, "ticket", tid, "status_change",
                  {"before": before_val, "after": value}, _AC.human())
        else:
            _emit(conn, project_id, "ticket", tid, "field_changed",
                  {"field": field, "before": before_val, "after": value}, _AC.human())
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
            "SELECT id, text FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC LIMIT 1 OFFSET ?",
            (tid, project_id, criterion_index)
        ).fetchone()
        if not criterion:
            conn.close()
            return False

        before_text = criterion["text"]
        conn.execute("UPDATE acceptance_criteria SET text = ? WHERE id = ?", (new_text, criterion["id"]))
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
                     (datetime.now().isoformat(), tid, project_id))
        # M1b: criteria_changed event with {before, after}
        if before_text != new_text:
            from actions import emit_event as _emit, ActorContext as _AC
            _emit(conn, project_id, "ticket", tid, "criteria_changed",
                  {"criterion_id": criterion["id"], "before": before_text, "after": new_text},
                  _AC.human())
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
            "SELECT id, text FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC LIMIT 1 OFFSET ?",
            (tid, project_id, criterion_index)
        ).fetchone()
        if not criterion:
            conn.close()
            return False

        removed_id = criterion["id"]
        removed_text = criterion["text"]
        conn.execute("DELETE FROM acceptance_criteria WHERE id = ?", (removed_id,))
        # Re-number sort_order
        remaining = conn.execute(
            "SELECT id FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
            (tid, project_id)
        ).fetchall()
        for i, r in enumerate(remaining):
            conn.execute("UPDATE acceptance_criteria SET sort_order = ? WHERE id = ?", (i, r["id"]))

        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
                     (datetime.now().isoformat(), tid, project_id))
        # M1b: criteria_removed event carries the removed text for restoration.
        from actions import emit_event as _emit, ActorContext as _AC
        _emit(conn, project_id, "ticket", tid, "criteria_removed",
              {"criterion_id": removed_id, "text": removed_text},
              _AC.human())
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

        cur = conn.execute(
            "INSERT INTO acceptance_criteria (ticket_id, project_id, text, checked, sort_order) VALUES (?,?,?,0,?)",
            (tid, project_id, text, max_order)
        )
        new_crit_id = cur.lastrowid
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
                     (datetime.now().isoformat(), tid, project_id))
        # M1b: criteria_added event
        from actions import emit_event as _emit, ActorContext as _AC
        _emit(conn, project_id, "ticket", tid, "criteria_added",
              {"criterion_id": new_crit_id, "text": text},
              _AC.human())
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
        before = [r[0] for r in conn.execute(
            "SELECT depends_on_id FROM depends WHERE ticket_id = ? AND project_id = ? ORDER BY depends_on_id",
            (tid, project_id),
        ).fetchall()]
        conn.execute("DELETE FROM depends WHERE ticket_id = ? AND project_id = ?", (tid, project_id))
        cleaned: list[str] = []
        for dep_id in depends_list:
            dep_id = dep_id.strip()
            if dep_id:
                conn.execute(
                    "INSERT OR IGNORE INTO depends (ticket_id, project_id, depends_on_id) VALUES (?,?,?)",
                    (tid, project_id, dep_id)
                )
                cleaned.append(dep_id)
        after = sorted(set(cleaned))
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
                     (datetime.now().isoformat(), tid, project_id))
        # M1b: dependency_changed event with sorted-list before/after for clean diffs.
        if sorted(before) != after:
            from actions import emit_event as _emit, ActorContext as _AC
            _emit(conn, project_id, "ticket", tid, "dependency_changed",
                  {"before": before, "after": after},
                  _AC.human())
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

        draft = bool(body.get("draft", False))
        ticket_id = _actions_add_ticket(
            conn, project_id, title,
            section=section, priority=priority,
            complexity=complexity, description=description,
            draft=draft,
        )
        # M1b: ticket_created event
        from actions import emit_event as _emit, ActorContext as _AC
        _emit(conn, project_id, "ticket", ticket_id, "ticket_created",
              {"id": ticket_id, "title": title, "section": section},
              _AC.human())

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

        # Snapshot the row so the ticket_deleted event carries enough state
        # for a future "undelete" / restore path.
        row = conn.execute(
            "SELECT * FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id)
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        snapshot = {k: row[k] for k in row.keys()}
        conn.execute("DELETE FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?", (tid, project_id))
        conn.execute("DELETE FROM depends WHERE ticket_id = ? AND project_id = ?", (tid, project_id))
        conn.execute("DELETE FROM tickets WHERE id = ? AND project_id = ?", (tid, project_id))
        # M1b: ticket_deleted event with snapshot. Activity row references a
        # subject that no longer exists in tickets — by design, the audit log
        # outlives the row.
        from actions import emit_event as _emit, ActorContext as _AC
        _emit(conn, project_id, "ticket", tid, "ticket_deleted",
              {"snapshot": snapshot},
              _AC.human())
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


# ---------------------------------------------------------------------------
# Workflow Bounce — validation helpers
# ---------------------------------------------------------------------------

def _normalize_json_array(value, field_name: str) -> str:
    """Accept a string or list, validate it's a JSON array, return canonical JSON string."""
    if isinstance(value, list):
        return json.dumps(value)
    if isinstance(value, str):
        value = value.strip()
        if not value or value == "[]":
            return "[]"
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError(f"{field_name}: invalid JSON")
        if not isinstance(parsed, list):
            raise ValueError(f"{field_name}: must be a JSON array")
        return json.dumps(parsed)
    raise ValueError(f"{field_name}: must be a string or list")


def _normalize_workflow_steps(steps_value, validate_agents: bool = True) -> str:
    """Validate a steps list. Returns canonical JSON string.

    Rejects steps that reference ``_project_*`` agents (discovered agents are
    read-only and cannot be used directly in workflow definitions).  When
    *validate_agents* is True, also verifies that each referenced agent exists
    in the ``workflow_agents`` table.
    """
    raw = _normalize_json_array(steps_value, "steps")
    steps = json.loads(raw)
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"steps[{i}]: must be an object")
        agent_id = step.get("agent_id", "")
        if not agent_id:
            raise ValueError(f"steps[{i}]: missing agent_id")
        if agent_id.startswith("_project_"):
            raise ValueError(
                f"steps[{i}]: discovered agents (_project_*) cannot be used in workflows — "
                f"create a custom agent instead"
            )
        if validate_agents and not _get_workflow_agent(agent_id):
            raise ValueError(f"steps[{i}]: agent '{agent_id}' not found")
    return raw


# ---------------------------------------------------------------------------
# Workflow Bounce — CRUD helpers + execution engine
# ---------------------------------------------------------------------------

def _list_workflow_agents() -> list[dict]:
    """Return all custom workflow agents."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        rows = conn.execute("SELECT * FROM workflow_agents ORDER BY name").fetchall()
        conn.close()
    return [dict(r) for r in rows]


def _get_workflow_agent(agent_id: str) -> dict | None:
    """Return a single workflow agent by ID."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        row = conn.execute("SELECT * FROM workflow_agents WHERE id = ?", (agent_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def _create_workflow_agent(agent_id: str, name: str, command: str, args: str, system_prompt: str) -> dict | None:
    """Insert a new workflow agent. Returns None on duplicate ID."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        try:
            conn.execute(
                "INSERT INTO workflow_agents (id, name, command, args, system_prompt) VALUES (?, ?, ?, ?, ?)",
                (agent_id, name, command, args, system_prompt),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM workflow_agents WHERE id = ?", (agent_id,)).fetchone()
            conn.close()
            return dict(row) if row else None
        except sqlite3.IntegrityError:
            conn.close()
            return None


def _update_workflow_agent(agent_id: str, updates: dict) -> dict | None:
    """Update an existing workflow agent. Returns updated record or None."""
    allowed = {"name", "command", "args", "system_prompt"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return _get_workflow_agent(agent_id)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [agent_id]
    with _db_lock:
        conn = get_db()
        init_db(conn)
        conn.execute(f"UPDATE workflow_agents SET {set_clause} WHERE id = ?", values)
        conn.commit()
        row = conn.execute("SELECT * FROM workflow_agents WHERE id = ?", (agent_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def _delete_workflow_agent(agent_id: str) -> bool:
    """Delete a workflow agent. Returns True if deleted."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        cur = conn.execute("DELETE FROM workflow_agents WHERE id = ?", (agent_id,))
        conn.commit()
        conn.close()
    return cur.rowcount > 0


def _discover_project_agents(proj: dict) -> list[dict]:
    """Scan a project's .claude/agents/ directory for agent .md files."""
    project_path = os.path.expanduser(proj.get("path", ""))
    agents_dir = os.path.join(project_path, ".claude", "agents")
    if not os.path.isdir(agents_dir):
        return []
    results = []
    for fname in sorted(os.listdir(agents_dir)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(agents_dir, fname)
        slug = fname[:-3]  # strip .md
        name = slug.replace("-", " ").replace("_", " ").title()
        # Try to parse frontmatter for a name
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read(2048)
            if content.startswith("---"):
                end = content.find("---", 3)
                if end > 0:
                    fm = content[3:end]
                    for line in fm.splitlines():
                        if line.strip().lower().startswith("name:"):
                            parsed = line.split(":", 1)[1].strip().strip("\"'")
                            if parsed:
                                name = parsed
                            break
        except Exception:
            pass
        results.append({
            "id": f"_project_{slug}",
            "name": name,
            "command": "claude",
            "args": "[]",
            "system_prompt": "",
            "source": "project",
            "editable": False,
        })
    return results


def _list_workflows() -> list[dict]:
    """Return all workflows."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        rows = conn.execute("SELECT * FROM workflows ORDER BY name").fetchall()
        conn.close()
    return [dict(r) for r in rows]


def _get_workflow(workflow_id: str) -> dict | None:
    """Return a single workflow by ID."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def _create_workflow(workflow_id: str, name: str, description: str, steps: str) -> dict | None:
    """Insert a new workflow. steps should be a JSON string."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        try:
            conn.execute(
                "INSERT INTO workflows (id, name, description, steps) VALUES (?, ?, ?, ?)",
                (workflow_id, name, description, steps),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
            conn.close()
            return dict(row) if row else None
        except sqlite3.IntegrityError:
            conn.close()
            return None


def _update_workflow(workflow_id: str, updates: dict) -> dict | None:
    """Update an existing workflow. Returns updated record or None."""
    allowed = {"name", "description", "steps"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return _get_workflow(workflow_id)
    fields["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [workflow_id]
    with _db_lock:
        conn = get_db()
        init_db(conn)
        conn.execute(f"UPDATE workflows SET {set_clause} WHERE id = ?", values)
        conn.commit()
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def _delete_workflow(workflow_id: str) -> bool:
    """Delete a workflow. Returns True if deleted."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        cur = conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        conn.commit()
        conn.close()
    return cur.rowcount > 0


def _list_workflow_runs(project_id: str, ticket_id: str) -> list[dict]:
    """Return all workflow runs for a ticket, with parsed conversation."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        rows = conn.execute(
            "SELECT * FROM workflow_runs WHERE project_id = ? AND ticket_id = ? ORDER BY started_at DESC",
            (project_id, ticket_id),
        ).fetchall()
        conn.close()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["conversation"] = json.loads(d.get("conversation", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["conversation"] = []
        results.append(d)
    return results


def _get_workflow_run(run_id: str) -> dict | None:
    """Return a single workflow run with parsed conversation."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
        conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["conversation"] = json.loads(d.get("conversation", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["conversation"] = []
    return d


def _update_workflow_run(run_id: str, **kwargs) -> dict | None:
    """Update a workflow run. Auto-serializes conversation to JSON."""
    allowed = {"status", "current_step", "conversation", "completed_at"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return _get_workflow_run(run_id)
    if "conversation" in fields and isinstance(fields["conversation"], (list, dict)):
        fields["conversation"] = json.dumps(fields["conversation"])
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [run_id]
    with _db_lock:
        conn = get_db()
        init_db(conn)
        conn.execute(f"UPDATE workflow_runs SET {set_clause} WHERE id = ?", values)
        conn.commit()
        conn.close()
    return _get_workflow_run(run_id)


def _run_workflow_thread(run_id: str, project_id: str, ticket_id: str, workflow: dict, proj: dict) -> None:
    """Background thread that executes a workflow bounce.

    For each step: loads the agent, builds a prompt with ticket context and
    conversation history, runs the agent CLI, parses the response.  After
    step > 0, asks the primary agent whether agents agree.  Pauses on
    disagreement; creates an attachment on completion.
    """
    try:
        # Load ticket context
        ticket = _get_ticket_json(project_id, ticket_id)
        if not ticket:
            _update_workflow_run(run_id, status="failed",
                                conversation=[{"role": "system", "content": "Ticket not found"}],
                                completed_at=datetime.utcnow().isoformat())
            with _workflow_runs_lock:
                if run_id in _workflow_runs:
                    _workflow_runs[run_id]["status"] = "failed"
            return

        # Build initial context from ticket
        context_parts = [f"Ticket: {ticket.get('id', '')} — {ticket.get('title', '')}"]
        if ticket.get("description"):
            context_parts.append(f"Description: {ticket['description']}")
        criteria = ticket.get("acceptance_criteria", [])
        if criteria:
            criteria_text = "\n".join(f"- {'[x]' if c.get('checked') else '[ ]'} {c.get('text', '')}" for c in criteria)
            context_parts.append(f"Acceptance Criteria:\n{criteria_text}")
        ticket_context = "\n\n".join(context_parts)

        steps = []
        try:
            steps = json.loads(workflow.get("steps", "[]")) if isinstance(workflow.get("steps"), str) else workflow.get("steps", [])
        except (json.JSONDecodeError, TypeError):
            steps = []

        if not steps:
            _update_workflow_run(run_id, status="failed",
                                conversation=[{"role": "system", "content": "Workflow has no steps"}],
                                completed_at=datetime.utcnow().isoformat())
            with _workflow_runs_lock:
                if run_id in _workflow_runs:
                    _workflow_runs[run_id]["status"] = "failed"
            return

        conversation = []
        total_steps = len(steps)

        for step_idx, step in enumerate(steps):
            # Check for cancellation
            with _workflow_runs_lock:
                run_state = _workflow_runs.get(run_id, {})
                if run_state.get("status") == "cancelled":
                    _update_workflow_run(run_id, status="cancelled",
                                        conversation=conversation,
                                        completed_at=datetime.utcnow().isoformat())
                    return

            # Check for pause (from disagreement)
            while True:
                with _workflow_runs_lock:
                    run_state = _workflow_runs.get(run_id, {})
                    st = run_state.get("status", "running")
                if st == "cancelled":
                    _update_workflow_run(run_id, status="cancelled",
                                        conversation=conversation,
                                        completed_at=datetime.utcnow().isoformat())
                    return
                if st != "paused":
                    break
                time.sleep(1)

            agent_id = step.get("agent_id", "")
            prompt_modifier = step.get("prompt_modifier", step.get("prompt", ""))

            # Load agent config
            agent = _get_workflow_agent(agent_id)
            if not agent:
                conversation.append({
                    "role": "system",
                    "step": step_idx,
                    "content": f"Agent '{agent_id}' not found — skipping step",
                })
                _update_workflow_run(run_id, current_step=step_idx, conversation=conversation)
                continue

            # Build prompt
            prompt_parts = []
            if agent.get("system_prompt"):
                prompt_parts.append(agent["system_prompt"])

            # Include last 3 conversation turns for context
            recent = conversation[-3:] if len(conversation) > 3 else conversation
            if recent:
                history = "\n\n".join(f"[{t.get('agent', 'system')}]: {t.get('content', '')}" for t in recent)
                prompt_parts.append(f"Previous conversation:\n{history}")

            prompt_parts.append(f"Ticket context:\n{ticket_context}")

            if prompt_modifier:
                prompt_parts.append(prompt_modifier)

            prompt = "\n\n---\n\n".join(prompt_parts)

            # Run agent CLI
            try:
                agent_args = json.loads(agent.get("args", "[]")) if isinstance(agent.get("args"), str) else agent.get("args", [])
            except (json.JSONDecodeError, TypeError):
                agent_args = []

            cmd = [agent.get("command", "claude")] + agent_args + ["-p", prompt, "--output-format", "json"]

            _update_workflow_run(run_id, current_step=step_idx, conversation=conversation, status="running")
            with _workflow_runs_lock:
                if run_id in _workflow_runs:
                    _workflow_runs[run_id]["status"] = "running"
                    _workflow_runs[run_id]["current_step"] = step_idx

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=WORKFLOW_AGENT_TIMEOUT,
                    cwd=os.path.expanduser(proj.get("path", ".")),
                )
                # Parse response — same pattern as gate-check
                response_text = result.stdout.strip()
                try:
                    response_json = json.loads(response_text)
                    if isinstance(response_json, dict) and "result" in response_json:
                        response_content = response_json["result"]
                    elif isinstance(response_json, dict) and "content" in response_json:
                        response_content = response_json["content"]
                    else:
                        response_content = response_text
                except json.JSONDecodeError:
                    response_content = response_text

                conversation.append({
                    "role": "agent",
                    "agent": agent.get("name", agent_id),
                    "agent_id": agent_id,
                    "step": step_idx,
                    "content": response_content,
                })
            except subprocess.TimeoutExpired:
                conversation.append({
                    "role": "system",
                    "step": step_idx,
                    "content": f"Agent '{agent.get('name', agent_id)}' timed out after {WORKFLOW_AGENT_TIMEOUT}s",
                })
            except Exception as e:
                conversation.append({
                    "role": "system",
                    "step": step_idx,
                    "content": f"Agent '{agent.get('name', agent_id)}' error: {e}",
                })

            _update_workflow_run(run_id, current_step=step_idx, conversation=conversation)

            # After step > 0, check if agents agree
            if step_idx > 0 and len(steps) > 1:
                primary_agent_id = steps[0].get("agent_id", "")
                primary_agent = _get_workflow_agent(primary_agent_id)
                if primary_agent:
                    agree_prompt = (
                        f"You are reviewing a multi-agent workflow discussion about ticket: {ticket.get('title', '')}.\n\n"
                        f"The conversation so far:\n"
                    )
                    for turn in conversation:
                        agree_prompt += f"\n[{turn.get('agent', 'system')}]: {turn.get('content', '')[:500]}\n"
                    agree_prompt += (
                        "\nDo the agents agree on the approach? Respond with JSON: "
                        '{"agreed": true/false, "summary": "brief summary", "contention": "point of disagreement or null"}'
                    )

                    try:
                        agree_args = json.loads(primary_agent.get("args", "[]")) if isinstance(primary_agent.get("args"), str) else primary_agent.get("args", [])
                    except (json.JSONDecodeError, TypeError):
                        agree_args = []

                    agree_cmd = [primary_agent.get("command", "claude")] + agree_args + ["-p", agree_prompt, "--output-format", "json"]
                    try:
                        agree_result = subprocess.run(
                            agree_cmd, capture_output=True, text=True,
                            timeout=WORKFLOW_AGENT_TIMEOUT,
                            cwd=os.path.expanduser(proj.get("path", ".")),
                        )
                        agree_text = agree_result.stdout.strip()
                        try:
                            agree_json = json.loads(agree_text)
                            if isinstance(agree_json, dict) and "result" in agree_json:
                                try:
                                    agree_json = json.loads(agree_json["result"])
                                except (json.JSONDecodeError, TypeError):
                                    agree_json = {"agreed": True, "summary": agree_json["result"]}
                        except json.JSONDecodeError:
                            agree_json = {"agreed": True, "summary": agree_text}

                        conversation.append({
                            "role": "arbiter",
                            "agent": primary_agent.get("name", primary_agent_id),
                            "step": step_idx,
                            "agreed": agree_json.get("agreed", True),
                            "summary": agree_json.get("summary", ""),
                            "contention": agree_json.get("contention"),
                            "content": agree_json.get("summary", ""),
                        })

                        if not agree_json.get("agreed", True):
                            _update_workflow_run(run_id, status="paused",
                                                current_step=step_idx, conversation=conversation)
                            with _workflow_runs_lock:
                                if run_id in _workflow_runs:
                                    _workflow_runs[run_id]["status"] = "paused"
                    except (subprocess.TimeoutExpired, Exception):
                        pass  # agreement check failed, continue anyway

        # Completed — create attachment
        summary_parts = []
        for turn in conversation:
            if turn.get("role") == "agent":
                summary_parts.append(f"**{turn.get('agent', 'Agent')}**: {turn.get('content', '')[:200]}")
        summary_text = "\n\n".join(summary_parts) if summary_parts else "Workflow completed"

        _add_attachment(
            project_id, ticket_id,
            attachment_type="workflow_bounce",
            name=f"workflow-{run_id[:8]}",
            path="",
            summary=summary_text[:1000],
            metadata=json.dumps({"run_id": run_id, "workflow_id": workflow.get("id", "")}),
        )

        _update_workflow_run(run_id, status="completed", conversation=conversation,
                            current_step=len(steps) - 1,
                            completed_at=datetime.utcnow().isoformat())
        with _workflow_runs_lock:
            if run_id in _workflow_runs:
                _workflow_runs[run_id]["status"] = "completed"

    except Exception as e:
        _update_workflow_run(run_id, status="failed",
                            conversation=[{"role": "system", "content": f"Workflow error: {e}"}],
                            completed_at=datetime.utcnow().isoformat())
        with _workflow_runs_lock:
            if run_id in _workflow_runs:
                _workflow_runs[run_id]["status"] = "failed"


# Sections that require a gate check before entry
GATED_SECTIONS = {"Ideas", "Backlog", "WIP", "For Review", "Done"}


def _clean_ai_text(text: str) -> str:
    """Strip leading markdown headers and blank lines from AI-generated text."""
    if not isinstance(text, str) or not text.strip():
        return text or ""
    lines = text.strip().splitlines()
    # Remove leading header line (# ..., ## ..., **...**:)
    while lines and (
        re.match(r"^#{1,4}\s", lines[0])
        or re.match(r"^\*\*.*\*\*:?\s*$", lines[0])
    ):
        lines.pop(0)
    # Remove leading blank lines
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()


def _clean_criteria_item(text: str) -> str:
    """Strip bullet prefixes and markdown formatting from a criteria item."""
    if not isinstance(text, str):
        return text or ""
    text = text.strip()
    # Remove leading bullet/number prefixes: - [ ] , - , * , 1.
    text = re.sub(r"^-\s*\[[ xX]?\]\s*", "", text)
    text = re.sub(r"^[-*]\s+", "", text)
    text = re.sub(r"^\d+\.\s+", "", text)
    return text.strip()


def _clean_analysis(analysis: dict) -> dict:
    """Clean AI response fields to remove header artifacts."""
    if not isinstance(analysis, dict):
        return analysis
    for key in ("suggestion", "current_summary", "content", "summary"):
        if key in analysis and isinstance(analysis[key], str):
            analysis[key] = _clean_ai_text(analysis[key])
    if "add_criteria" in analysis and isinstance(analysis["add_criteria"], list):
        analysis["add_criteria"] = [_clean_criteria_item(c) for c in analysis["add_criteria"] if c and c.strip()]
    if "categories" in analysis and isinstance(analysis["categories"], dict):
        for cat in analysis["categories"].values():
            if isinstance(cat, dict):
                for key in ("suggestion", "current_summary", "content"):
                    if key in cat and isinstance(cat[key], str):
                        cat[key] = _clean_ai_text(cat[key])
                if "add_criteria" in cat and isinstance(cat["add_criteria"], list):
                    cat["add_criteria"] = [_clean_criteria_item(c) for c in cat["add_criteria"] if c and c.strip()]
    return analysis


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
    _clean_analysis(analysis)
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

    _clean_analysis(analysis)
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
    suggested = _clean_ai_text(data.get("suggested", ""))
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
            "SELECT flag, content FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
            (tid, project_id, flag)
        ).fetchone()

        if existing:
            before = {"present": True, "content": existing["content"] or ""}
            conn.execute(
                "DELETE FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
                (tid, project_id, flag)
            )
            after = {"present": False, "content": ""}
        else:
            before = {"present": False, "content": ""}
            conn.execute(
                "INSERT INTO readiness_flags (ticket_id, project_id, flag, set_by) VALUES (?, ?, ?, 'dashboard')",
                (tid, project_id, flag)
            )
            after = {"present": True, "content": ""}

        # M1b: readiness_changed event with before/after presence + content
        from actions import emit_event as _emit, ActorContext as _AC
        _emit(conn, project_id, "ticket", tid, "readiness_changed",
              {"flag": flag, "before": before, "after": after}, _AC.human())
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

        # Capture before
        existing = conn.execute(
            "SELECT content FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
            (tid, project_id, flag)
        ).fetchone()
        before = {"present": existing is not None,
                  "content": (existing["content"] if existing else "") or ""}

        if content:
            conn.execute("""
                INSERT INTO readiness_flags (ticket_id, project_id, flag, content, set_by)
                VALUES (?, ?, ?, ?, 'dashboard')
                ON CONFLICT (ticket_id, project_id, flag)
                DO UPDATE SET content = excluded.content
            """, (tid, project_id, flag, content))
            after = {"present": True, "content": content}
        else:
            conn.execute(
                "DELETE FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
                (tid, project_id, flag)
            )
            after = {"present": False, "content": ""}

        # M1b: readiness_changed event (no-op writes are also skipped from emit).
        if before != after:
            from actions import emit_event as _emit, ActorContext as _AC
            _emit(conn, project_id, "ticket", tid, "readiness_changed",
                  {"flag": flag, "before": before, "after": after}, _AC.human())
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

    # Kitchen state (M1a) — automation intent + computed eligibility + latest run.
    # Errors here are non-fatal: pre-migration DBs or transient failures fall
    # back to default 'manual' / no run / not eligible.
    automation_mode = "manual"
    hold_reason = None
    no_test_required = False
    no_test_required_note = ""
    latest_run_status = None
    try:
        am_row = conn.execute(
            "SELECT automation_mode, hold_reason FROM automation_subjects "
            "WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ?",
            (project_id, row["id"]),
        ).fetchone()
        if am_row:
            automation_mode = am_row["automation_mode"]
            hold_reason = am_row["hold_reason"]
        if "no_test_required" in row.keys():
            no_test_required = bool(row["no_test_required"])
            no_test_required_note = row["no_test_required_note"] or ""
        latest = conn.execute(
            "SELECT status FROM runs WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (project_id, row["id"]),
        ).fetchone()
        if latest:
            latest_run_status = latest["status"]
    except Exception:
        pass

    try:
        elig = _kitchen_eligibility(conn, project_id, "ticket", row["id"])
        eligible = elig.eligible
        eligibility_reasons = list(elig.reasons)
    except Exception:
        eligible = False
        eligibility_reasons = ["eligibility check failed"]

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
        # Kitchen state (M1a)
        "automation_mode": automation_mode,
        "hold_reason": hold_reason,
        "no_test_required": no_test_required,
        "no_test_required_note": no_test_required_note,
        "latest_run_status": latest_run_status,
        "automation_eligible": eligible,
        "automation_eligibility_reasons": eligibility_reasons,
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


def _render_journeys_page(proj: dict, port: int, open_journey_id: str = "") -> str:
    """Render the journeys page for a single project.

    Note: innerHTML usage is safe here — all dynamic values pass through the
    esc() function which uses textContent-based escaping to prevent XSS.
    Server-injected values use _safe_attr() for the same purpose.
    """
    pid = _safe_attr(proj["id"])
    name = _safe_attr(proj.get("name", proj["id"]))
    api_base = f"http://localhost:{port}/{pid}/api"

    return f'''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} — Journeys</title>
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
  --accent: #3b82f6; --green: #22c55e; --red: #ef4444; --yellow: #eab308;
}}
[data-theme="light"] {{
  --bg-page: #f8f9fa; --bg-surface: #ffffff; --bg-card: #ffffff; --bg-hover: #f3f4f6;
  --border-subtle: #e5e7eb; --border-default: #d1d5db; --border-strong: #9ca3af;
  --text-primary: #111827; --text-secondary: #6b7280; --text-tertiary: #9ca3af;
  --accent: #2563eb; --green: #16a34a; --red: #dc2626; --yellow: #ca8a04;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: var(--bg-page); color: var(--text-primary); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
.header {{ display: flex; align-items: center; gap: 16px; padding: 16px 24px; border-bottom: 1px solid var(--border-default); }}
.header .back {{ color: var(--text-tertiary); text-decoration: none; font-size: 13px; }}
.header .back:hover {{ color: var(--text-secondary); }}
.header h1 {{ font-size: 16px; font-weight: 600; flex: 1; }}
.btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 500; border: 1px solid; transition: background 0.15s; }}
.btn-primary {{ background: rgba(59,130,246,0.15); border-color: rgba(59,130,246,0.3); color: var(--accent); }}
.btn-primary:hover {{ background: rgba(59,130,246,0.25); }}
.btn-success {{ background: rgba(34,197,94,0.15); border-color: rgba(34,197,94,0.3); color: var(--green); }}
.btn-success:hover {{ background: rgba(34,197,94,0.25); }}
.btn-danger {{ background: rgba(239,68,68,0.1); border-color: rgba(239,68,68,0.2); color: var(--red); }}
.btn-danger:hover {{ background: rgba(239,68,68,0.2); }}
.btn-ghost {{ background: transparent; border-color: var(--border-default); color: var(--text-secondary); }}
.btn-ghost:hover {{ background: var(--bg-hover); color: var(--text-primary); }}
.btn-sm {{ padding: 4px 10px; font-size: 11px; }}
.btn-icon {{ width: 28px; height: 28px; padding: 0; display: inline-flex; align-items: center; justify-content: center; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }}
.badge-draft {{ background: rgba(156,163,175,0.15); color: var(--text-tertiary); }}
.badge-active {{ background: rgba(59,130,246,0.15); color: var(--accent); }}
.badge-validated {{ background: rgba(34,197,94,0.15); color: var(--green); }}
.badge-archived {{ background: rgba(156,163,175,0.1); color: var(--text-tertiary); }}
.status-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
.status-dot.passed {{ background: var(--green); }}
.status-dot.failed {{ background: var(--red); }}
.status-dot.skipped {{ background: var(--text-tertiary); }}
.status-dot.pending {{ background: var(--border-strong); }}
.content {{ padding: 24px; max-width: 1200px; }}
.journey-list {{ display: flex; flex-direction: column; gap: 8px; }}
.journey-card {{ background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 8px; padding: 16px; cursor: pointer; transition: border-color 0.15s, background 0.15s; }}
.journey-card:hover {{ border-color: var(--border-strong); background: var(--bg-hover); }}
.journey-card .top-row {{ display: flex; align-items: center; gap: 10px; }}
.journey-card .title {{ font-size: 14px; font-weight: 600; flex: 1; }}
.journey-card .meta {{ display: flex; align-items: center; gap: 12px; margin-top: 8px; color: var(--text-tertiary); font-size: 11px; }}
.journey-card .persona {{ color: var(--text-secondary); font-size: 11px; font-style: italic; }}
.empty-state {{ text-align: center; padding: 60px 24px; color: var(--text-tertiary); font-size: 13px; }}
.journey-detail {{ display: none; }}
.journey-detail.active {{ display: block; }}
.detail-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }}
.detail-header .title-input {{ flex: 1; background: transparent; border: 1px solid transparent; padding: 4px 8px; font-size: 18px; font-weight: 600; color: var(--text-primary); border-radius: 4px; font-family: inherit; }}
.detail-header .title-input:focus {{ border-color: var(--accent); outline: none; background: var(--bg-card); }}
.steps-table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
.steps-table th {{ text-align: left; font-size: 10px; font-weight: 600; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.5px; padding: 6px 10px; border-bottom: 1px solid var(--border-default); }}
.steps-table td {{ padding: 8px 10px; border-bottom: 1px solid var(--border-subtle); font-size: 13px; vertical-align: middle; }}
.steps-table tr:hover td {{ background: var(--bg-hover); }}
.steps-table .step-num {{ color: var(--text-tertiary); font-size: 11px; width: 30px; text-align: center; }}
.steps-table .action-cell {{ font-family: "SF Mono", Monaco, monospace; font-size: 12px; color: var(--accent); }}
.steps-table .target-cell {{ font-family: "SF Mono", Monaco, monospace; font-size: 11px; color: var(--text-secondary); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.steps-table .label-cell {{ color: var(--text-primary); }}
.steps-table .actions-cell {{ width: 80px; text-align: right; }}
.steps-table .capture-icon {{ color: var(--yellow); font-size: 11px; }}
.step-expand {{ display: none; }}
.step-expand.active {{ display: table-row; }}
.step-expand td {{ padding: 12px 10px; background: var(--bg-surface); }}
.step-expand .field-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
.step-expand label {{ font-size: 10px; color: var(--text-tertiary); text-transform: uppercase; margin-bottom: 2px; display: block; }}
.step-expand input, .step-expand select {{ width: 100%; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 4px; padding: 6px 8px; color: var(--text-primary); font-size: 12px; font-family: "SF Mono", Monaco, monospace; }}
.step-expand input:focus, .step-expand select:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }}
.run-results {{ margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border-default); }}
.run-summary {{ display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }}
.run-status {{ font-size: 14px; font-weight: 600; }}
.run-status.passed {{ color: var(--green); }}
.run-status.failed {{ color: var(--red); }}
.run-meta {{ font-size: 11px; color: var(--text-tertiary); }}
.step-timeline {{ display: flex; gap: 4px; align-items: center; flex-wrap: wrap; }}
.tl-step {{ display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 6px 8px; border-radius: 6px; cursor: pointer; min-width: 40px; transition: background 0.15s; }}
.tl-step:hover {{ background: var(--bg-hover); }}
.tl-step .tl-num {{ font-size: 10px; color: var(--text-tertiary); }}
.tl-step .tl-label {{ font-size: 9px; color: var(--text-tertiary); max-width: 60px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center; }}
.step-result-detail {{ margin-top: 12px; padding: 12px; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 6px; font-size: 12px; }}
.step-result-detail .error-msg {{ color: var(--red); font-family: "SF Mono", Monaco, monospace; white-space: pre-wrap; }}
.step-result-detail img {{ max-width: 100%; border-radius: 4px; margin-top: 8px; border: 1px solid var(--border-default); }}
.run-history {{ margin-top: 16px; }}
.run-history h3 {{ font-size: 12px; font-weight: 600; color: var(--text-secondary); margin-bottom: 8px; }}
.run-row {{ display: flex; align-items: center; gap: 10px; padding: 6px 8px; border-radius: 4px; font-size: 11px; cursor: pointer; color: var(--text-secondary); }}
.run-row:hover {{ background: var(--bg-hover); }}
.run-row .run-id {{ font-family: "SF Mono", Monaco, monospace; color: var(--text-tertiary); }}
.form-row {{ display: flex; gap: 12px; align-items: center; margin-bottom: 12px; }}
.form-row label {{ font-size: 11px; color: var(--text-secondary); min-width: 80px; }}
.form-row input {{ flex: 1; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 4px; padding: 6px 8px; color: var(--text-primary); font-size: 13px; font-family: inherit; }}
.form-row input:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }}
.toast {{ position: fixed; bottom: 20px; right: 20px; padding: 10px 16px; border-radius: 6px; font-size: 12px; z-index: 1000; transition: opacity 0.3s; }}
.toast.success {{ background: rgba(34,197,94,0.15); border: 1px solid rgba(34,197,94,0.3); color: var(--green); }}
.toast.error {{ background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.3); color: var(--red); }}

/* Timeline — unified vertical view */
.tl-container {{ position: relative; padding: 0; }}
.tl-container::before {{
  content: ''; position: absolute; left: 50%; top: 0; bottom: 0;
  width: 2px; background: var(--border-default); transform: translateX(-50%);
}}
.tl-row {{ display: flex; align-items: flex-start; position: relative; min-height: 40px; margin-bottom: 4px; }}
.tl-left {{ width: calc(50% - 20px); display: flex; justify-content: flex-end; padding-right: 16px; }}
.tl-dot-col {{ width: 40px; display: flex; justify-content: center; flex-shrink: 0; padding-top: 6px; position: relative; z-index: 1; }}
.tl-right {{ width: calc(50% - 20px); padding-left: 16px; }}
.tl-dot {{ width: 10px; height: 10px; border-radius: 50%; background: var(--border-default); border: 2px solid var(--bg-page, var(--bg-surface)); }}
.tl-dot.passed {{ background: var(--green); }}
.tl-dot.failed {{ background: var(--red); }}
.tl-dot.skipped {{ background: var(--yellow); }}
.tl-dot.capture {{ width: 14px; height: 14px; background: var(--accent); }}
.tl-thumb {{
  width: 200px; height: 120px; border: 1px solid var(--border-default); border-radius: 6px;
  overflow: hidden; cursor: pointer; position: relative;
  transition: border-color 0.15s, box-shadow 0.15s;
}}
.tl-thumb:hover {{ border-color: var(--accent); box-shadow: 0 0 0 2px rgba(59,130,246,0.15); }}
.tl-thumb img {{ width: 100%; height: 100%; object-fit: cover; }}
.tl-thumb.failed {{ border-color: var(--red); border-width: 2px; }}
.tl-thumb.failed::after {{
  content: "Step failed"; position: absolute; inset: 0;
  background: rgba(0,0,0,0.72); display: flex; align-items: center;
  justify-content: center; color: var(--red); font-size: 11px; font-weight: 600;
}}
.tl-thumb-empty {{
  width: 200px; height: 50px; border: 1px dashed var(--border-default); border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  color: var(--text-tertiary); font-size: 10px;
}}
.tl-detail {{
  background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 6px;
  padding: 8px 12px; font-size: 11px; max-width: 280px;
}}
.tl-detail.failed {{ border-color: var(--red); background: rgba(239,68,68,0.05); }}
.tl-detail.skipped {{ opacity: 0.5; }}
.tl-detail-label {{ font-weight: 600; color: var(--text-primary); margin-bottom: 2px; }}
.tl-detail-action {{ font-family: "SF Mono", Monaco, monospace; font-size: 10px; color: var(--text-tertiary); }}
.tl-detail-target {{ font-size: 10px; color: var(--text-tertiary); margin-top: 2px; word-break: break-all; }}
.tl-detail-error {{ font-size: 10px; color: var(--red); margin-top: 4px; font-family: "SF Mono", Monaco, monospace; }}
.tl-detail-actions {{ display: flex; gap: 4px; margin-top: 6px; }}
.tl-detail-actions button {{
  font-size: 9px; padding: 2px 6px; border: 1px solid var(--border-default); border-radius: 3px;
  background: none; color: var(--text-tertiary); cursor: pointer;
}}
.tl-detail-actions button:hover {{ color: var(--text-primary); background: var(--bg-hover); }}
.tl-row.action-only {{ min-height: 28px; }}
.tl-row.action-only .tl-detail {{ padding: 4px 10px; }}
.tl-add-step {{
  display: flex; align-items: center; justify-content: center;
  padding: 6px 14px; border: 1px dashed var(--border-default); border-radius: 6px;
  background: none; color: var(--text-tertiary); cursor: pointer; font-size: 11px;
  margin-top: 4px; width: 120px; margin-left: calc(50% - 60px); position: relative; z-index: 1;
}}
.tl-add-step:hover {{ color: var(--accent); border-color: var(--accent); }}

/* Lightbox */
.flow-lightbox {{
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.85); z-index: 2000;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
}}
.flow-lightbox-img {{
  max-width: 90vw; max-height: 90vh; border-radius: 8px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.5);
}}
</style>
</head>
<body>
<div class="header">
  <a href="/{pid}" class="back" data-testid="journeys-back">&larr; Board</a>
  <h1>Journeys</h1>
  <div style="display:flex;gap:8px;">
    <button class="btn btn-ghost" onclick="inferJourneys()" data-testid="infer-btn">Infer from Tickets</button>
    <button class="btn btn-primary" onclick="createJourney()" data-testid="new-journey-btn">+ New Journey</button>
  </div>
</div>
<div class="content">
  <div id="list-view">
    <div id="journey-list" class="journey-list" data-testid="journey-list"></div>
  </div>
  <div id="detail-view" class="journey-detail" data-testid="journey-detail">
    <div class="detail-header">
      <button class="btn btn-ghost btn-sm" onclick="showList()" data-testid="detail-back">&larr;</button>
      <input class="title-input" id="detail-title" data-testid="detail-title" placeholder="Journey title...">
      <span id="detail-badge" class="badge badge-draft" data-testid="detail-badge">draft</span>
      <div style="margin-left:auto;display:flex;gap:6px;">
        <button class="btn btn-ghost btn-sm" onclick="validateJourney()" data-testid="validate-btn">Validate</button>
        <button class="btn btn-success btn-sm" onclick="runJourney()" data-testid="run-btn">&#9654; Run</button>
        <button class="btn btn-danger btn-sm" onclick="deleteJourney()" data-testid="delete-btn">Delete</button>
      </div>
    </div>
    <div class="form-row">
      <label>Persona</label>
      <input id="detail-persona" data-testid="detail-persona" placeholder="Who is this journey for?">
    </div>
    <div class="form-row">
      <label>Description</label>
      <input id="detail-description" data-testid="detail-description" placeholder="What does this journey validate?">
    </div>
    <h3 style="font-size:13px;font-weight:600;margin:20px 0 8px;">Steps</h3>
    <div id="timeline-container" class="tl-container" data-testid="timeline-container"></div>
    <div id="flow-lightbox" class="flow-lightbox" style="display:none;" onclick="closeLightbox()">
      <img id="flow-lightbox-img" class="flow-lightbox-img" />
    </div>
    <div style="margin-top:20px;padding-top:16px;border-top:1px solid var(--border-default);">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
        <h3 style="font-size:13px;font-weight:600;">Linked Tickets</h3>
        <button class="btn btn-ghost btn-sm" onclick="linkTicket()" data-testid="link-ticket-btn">+ Link Ticket</button>
      </div>
      <div id="linked-tickets" data-testid="linked-tickets" style="font-size:12px;color:var(--text-secondary);"></div>
    </div>
    <div id="run-results" class="run-results" style="display:none;" data-testid="run-results">
      <div class="run-summary">
        <span id="run-status-label" class="run-status" data-testid="run-status"></span>
        <span id="run-meta" class="run-meta" data-testid="run-meta"></span>
      </div>
      <div id="step-timeline" class="step-timeline" data-testid="step-timeline"></div>
      <div id="step-result-detail" class="step-result-detail" style="display:none;" data-testid="step-result-detail"></div>
    </div>
    <div id="run-history" class="run-history" style="display:none;" data-testid="run-history">
      <h3>Run History</h3>
      <div id="run-history-list" data-testid="run-history-list"></div>
    </div>
  </div>
</div>
<script>
(function() {{
  var API = '{api_base}';
  var API_PREFIX = API.replace(/\/api$/, '');
  var currentJourney = null;
  var currentSteps = [];
  var lastRunResults = null;

  /* ── Helpers ─────────────────────────────────────────── */
  function toast(msg, type) {{
    var el = document.createElement('div');
    el.className = 'toast ' + (type || 'success');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function() {{ el.style.opacity = '0'; setTimeout(function() {{ el.remove(); }}, 300); }}, 2500);
  }}
  function esc(s) {{ var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }}
  function timeAgo(ts) {{
    if (!ts) return 'never';
    var s = String(ts);
    if (s.indexOf('Z') === -1 && s.indexOf('+') === -1 && s.indexOf('T') > 0) s += 'Z';
    var d = new Date(s), diff = (Date.now() - d.getTime()) / 1000;
    if (isNaN(diff)) return 'unknown';
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
  }}

  /* ── View tab switching ──────────────────────────────── */
  /* ── Timeline renderer (unified view) ──────────────── */
  function extractTarget(step) {{
    try {{
      var t = typeof step.target_json === 'string' ? JSON.parse(step.target_json) : (step.target_json || {{}});
      return t.testid || t.css || t.text || t.title || '';
    }} catch(e) {{ return ''; }}
  }}

  var DOT_TIPS = {{ passed: 'Step passed', failed: 'Step failed', skipped: 'Step skipped', capture: 'Screenshot capture' }};
  var ACTION_LABELS = {{
    open: 'Navigate to page', reload: 'Reload page', click: 'Click element',
    double_click: 'Double-click element', fill: 'Fill in field', select: 'Select option',
    press: 'Press key', wait_for: 'Wait for element', assert_visible: 'Assert element visible',
    assert_text: 'Assert text content', capture: 'Capture screenshot'
  }};

  function renderTimeline() {{
    var container = document.getElementById('timeline-container');
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);

    if (!currentSteps || currentSteps.length === 0) {{
      var empty = document.createElement('div');
      empty.style.cssText = 'font-size:12px;color:var(--text-tertiary);padding:16px 0;text-align:center;';
      empty.textContent = 'No steps yet.';
      container.appendChild(empty);
    }}

    var runResults = lastRunResults || [];

    // Track current URL for grouping
    var currentUrl = '/' + (currentJourney ? currentJourney.id : '');
    var lastUrl = null;

    currentSteps.forEach(function(step, idx) {{
      var sr = runResults.find(function(r) {{ return r.sort_order === idx; }}) ||
               runResults.find(function(r) {{ return r.step_id === step.id; }});
      var status = sr ? sr.status : '';
      var isCapture = step.action === 'capture';

      // Track URL — open steps change the current URL
      if (step.action === 'open') {{
        currentUrl = step.value || extractTarget(step) || '/';
      }}

      // URL group header when URL changes
      if (currentUrl !== lastUrl) {{
        lastUrl = currentUrl;
        var urlRow = document.createElement('div');
        urlRow.className = 'tl-row';
        urlRow.style.cssText = 'min-height:24px;margin-bottom:0;';
        var urlLeft = document.createElement('div');
        urlLeft.className = 'tl-left';
        urlRow.appendChild(urlLeft);
        var urlDotCol = document.createElement('div');
        urlDotCol.className = 'tl-dot-col';
        urlDotCol.style.paddingTop = '2px';
        var urlDot = document.createElement('div');
        urlDot.style.cssText = 'width:6px;height:6px;border-radius:50%;background:var(--accent);';
        urlDotCol.appendChild(urlDot);
        urlRow.appendChild(urlDotCol);
        var urlRight = document.createElement('div');
        urlRight.className = 'tl-right';
        var urlLabel = document.createElement('div');
        urlLabel.style.cssText = 'font-size:10px;font-family:"SF Mono",Monaco,monospace;color:var(--accent);padding:2px 0;display:flex;align-items:center;gap:6px;';
        urlLabel.textContent = currentUrl;
        urlRight.appendChild(urlLabel);
        urlRow.appendChild(urlRight);
        container.appendChild(urlRow);
      }}

      var row = document.createElement('div');
      row.className = 'tl-row' + (isCapture ? '' : ' action-only');
      row.id = 'tl-row-' + (step.id || idx);

      // ── Left side: screenshot thumbnail (capture steps only) ──
      var left = document.createElement('div');
      left.className = 'tl-left';
      if (isCapture) {{
        if (sr && sr.screenshot_path) {{
          var thumb = document.createElement('div');
          thumb.className = 'tl-thumb' + (status === 'failed' ? ' failed' : '');
          var img = document.createElement('img');
          img.src = API_PREFIX + sr.screenshot_path;
          img.alt = step.label || 'Screenshot';
          img.loading = 'lazy';
          thumb.appendChild(img);
          thumb.addEventListener('click', function() {{ openLightbox(API_PREFIX + sr.screenshot_path); }});
          left.appendChild(thumb);
        }} else {{
          var ph = document.createElement('div');
          ph.className = 'tl-thumb-empty';
          ph.textContent = status === 'failed' ? '\u2717 failed' : '\u25a3 no capture yet';
          left.appendChild(ph);
        }}
      }}
      row.appendChild(left);

      // ── Center: dot on spine with tooltip ──
      var dotCol = document.createElement('div');
      dotCol.className = 'tl-dot-col';
      var dot = document.createElement('div');
      dot.className = 'tl-dot' + (status ? ' ' + status : '') + (isCapture ? ' capture' : '');
      dot.title = (isCapture ? DOT_TIPS.capture : (DOT_TIPS[status] || 'Not run yet'));
      dotCol.appendChild(dot);
      row.appendChild(dotCol);

      // ── Right side: step detail card ──
      var right = document.createElement('div');
      right.className = 'tl-right';
      var detail = document.createElement('div');
      detail.className = 'tl-detail' + (status === 'failed' ? ' failed' : '') + (status === 'skipped' ? ' skipped' : '');
      detail.id = 'tl-detail-' + (step.id || idx);

      var lbl = document.createElement('div');
      lbl.className = 'tl-detail-label';
      lbl.textContent = (idx + 1) + '. ' + (step.label || step.action || 'Step');
      detail.appendChild(lbl);

      // Human-readable description
      var desc = document.createElement('div');
      desc.style.cssText = 'font-size:10px;color:var(--text-secondary);margin-bottom:2px;';
      var descText = ACTION_LABELS[step.action] || step.action || '';
      var target = extractTarget(step);
      if (target) descText += ': ' + target;
      if (step.value && step.action !== 'open') descText += ' = "' + step.value + '"';
      if (step.key) descText += ' [' + step.key + ']';
      desc.textContent = descText;
      detail.appendChild(desc);

      if (status === 'failed' && sr && sr.error_message) {{
        var err = document.createElement('div');
        err.className = 'tl-detail-error';
        err.textContent = sr.error_message.substring(0, 120);
        detail.appendChild(err);
      }}

      // ── Inline edit form (hidden by default) ──
      var editForm = document.createElement('div');
      editForm.id = 'tl-edit-' + (step.id || idx);
      editForm.style.cssText = 'display:none;margin-top:6px;';
      var fields = [
        ['Label', 'label', step.label || '', 'Human-readable name, e.g. "Click login button"'],
        ['Action', 'action', step.action || '', 'open, click, fill, press, wait_for, capture, assert_visible, assert_text'],
        ['Value', 'value', step.value || '', 'URL for open, text for fill, key name for press'],
        ['Key', 'key', step.key || '', 'Keyboard key, e.g. Escape, Enter, Tab'],
        ['Target (testid)', '_target_testid', (function() {{ try {{ return JSON.parse(step.target_json || '{{}}').testid || ''; }} catch(e) {{ return ''; }} }})(), 'data-testid attribute, e.g. login-btn'],
        ['Target (css)', '_target_css', (function() {{ try {{ return JSON.parse(step.target_json || '{{}}').css || ''; }} catch(e) {{ return ''; }} }})(), 'CSS selector, e.g. .submit-btn >> nth=0']
      ];
      fields.forEach(function(f) {{
        var fRow = document.createElement('div');
        fRow.style.cssText = 'display:flex;align-items:center;gap:6px;margin-bottom:3px;';
        var fLabel = document.createElement('span');
        fLabel.style.cssText = 'font-size:9px;color:var(--text-tertiary);min-width:70px;';
        fLabel.textContent = f[0];
        fRow.appendChild(fLabel);
        var fInput = document.createElement('input');
        fInput.style.cssText = 'flex:1;font-size:10px;padding:2px 6px;border:1px solid var(--border-default);border-radius:3px;background:var(--bg-surface);color:var(--text-primary);font-family:inherit;';
        fInput.value = f[2];
        fInput.placeholder = f[3] || '';
        fInput.title = f[3] || '';
        fInput.dataset.field = f[1];
        fInput.dataset.stepId = step.id;
        fInput.addEventListener('blur', function() {{
          var field = fInput.dataset.field;
          var val = fInput.value;
          if (field.startsWith('_target_')) {{
            updateTarget(parseInt(fInput.dataset.stepId), field.replace('_target_', ''), val);
          }} else {{
            updateField(parseInt(fInput.dataset.stepId), field, val);
          }}
        }});
        fRow.appendChild(fInput);
        editForm.appendChild(fRow);
      }});
      detail.appendChild(editForm);

      var actions = document.createElement('div');
      actions.className = 'tl-detail-actions';
      var editBtn = document.createElement('button');
      editBtn.textContent = '\u270e';
      editBtn.title = 'Edit step';
      editBtn.addEventListener('click', function() {{
        var ef = document.getElementById('tl-edit-' + (step.id || idx));
        if (ef) ef.style.display = ef.style.display === 'none' ? '' : 'none';
      }});
      actions.appendChild(editBtn);
      var delBtn = document.createElement('button');
      delBtn.textContent = '\u00d7';
      delBtn.title = 'Delete step';
      delBtn.addEventListener('click', function() {{ deleteStep(step.id); }});
      actions.appendChild(delBtn);
      detail.appendChild(actions);

      right.appendChild(detail);
      row.appendChild(right);

      container.appendChild(row);
    }});

    // ── Add Step button at the end of the timeline ──
    var addRow = document.createElement('button');
    addRow.className = 'tl-add-step';
    addRow.textContent = '+ Add Step';
    addRow.onclick = function() {{ addStep(); }};
    container.appendChild(addRow);
  }}

  /* ── Lightbox ────────────────────────────────────────── */
  function openLightbox(src) {{
    var lb = document.getElementById('flow-lightbox');
    var img = document.getElementById('flow-lightbox-img');
    if (!lb || !img) return;
    img.src = src;
    lb.style.display = 'flex';
  }}
  window.closeLightbox = function() {{
    var lb = document.getElementById('flow-lightbox');
    if (lb) lb.style.display = 'none';
  }};
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') window.closeLightbox();
  }});

  /* ── API ─────────────────────────────────────────────── */
  function apiGet(path) {{ return fetch(API + path).then(function(r) {{ return r.json(); }}); }}
  function apiPost(path, body) {{ return fetch(API + path, {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body||{{}}) }}).then(function(r) {{ return r.json().then(function(d) {{ return {{status:r.status,data:d}}; }}); }}); }}
  function apiPut(path, body) {{ return fetch(API + path, {{ method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body) }}).then(function(r) {{ return r.json(); }}); }}
  function apiDel(path) {{ return fetch(API + path, {{ method:'DELETE' }}).then(function(r) {{ return r.json(); }}); }}

  /* ── Journey List ────────────────────────────────────── */
  function loadList() {{
    apiGet('/journeys').then(function(data) {{
      var list = document.getElementById('journey-list');
      var journeys = data.journeys || [];
      list.textContent = '';
      if (journeys.length === 0) {{
        var empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.setAttribute('data-testid', 'journeys-empty');
        var p1 = document.createElement('p');
        p1.style.cssText = 'font-size:24px;margin-bottom:8px';
        p1.textContent = 'No journeys yet';
        var p2 = document.createElement('p');
        p2.textContent = 'Define user flows to validate your features work end-to-end.';
        empty.appendChild(p1);
        empty.appendChild(p2);
        list.appendChild(empty);
        return;
      }}
      journeys.forEach(function(j) {{
        var card = document.createElement('div');
        card.className = 'journey-card';
        card.setAttribute('data-testid', 'journey-card-' + j.id);
        card.onclick = function() {{ openJourney(j.id); }};
        // Top row
        var topRow = document.createElement('div');
        topRow.className = 'top-row';
        var dot = document.createElement('span');
        dot.className = 'status-dot ' + (j.last_run_status === 'passed' ? 'passed' : j.last_run_status === 'failed' ? 'failed' : 'pending');
        var titleSpan = document.createElement('span');
        titleSpan.className = 'title';
        titleSpan.textContent = j.title;
        var badge = document.createElement('span');
        badge.className = 'badge badge-' + j.status;
        badge.textContent = j.status;
        topRow.appendChild(dot);
        topRow.appendChild(titleSpan);
        topRow.appendChild(badge);
        card.appendChild(topRow);
        // Meta
        var meta = document.createElement('div');
        meta.className = 'meta';
        var stepCount = document.createElement('span');
        stepCount.textContent = (j.step_count || 0) + ' steps';
        meta.appendChild(stepCount);
        if (j.persona) {{
          var persona = document.createElement('span');
          persona.className = 'persona';
          persona.textContent = j.persona;
          meta.appendChild(persona);
        }}
        if (j.last_run_at) {{
          var runAt = document.createElement('span');
          runAt.textContent = 'Last run: ' + timeAgo(j.last_run_at);
          meta.appendChild(runAt);
        }}
        card.appendChild(meta);
        list.appendChild(card);
      }});
    }});
  }}

  /* ── Create ──────────────────────────────────────────── */
  window.createJourney = function() {{
    var title = prompt('Journey title:');
    if (!title) return;
    apiPost('/journeys', {{title:title}}).then(function(r) {{
      if (r.status === 201) {{ toast('Journey created'); openJourney(r.data.id); }}
      else toast(r.data.error || 'Failed', 'error');
    }});
  }};

  /* ── Open Detail ─────────────────────────────────────── */
  function openJourney(id) {{
    apiGet('/journeys/' + id).then(function(j) {{
      currentJourney = j;
      currentSteps = j.steps || [];
      document.getElementById('detail-title').value = j.title;
      document.getElementById('detail-persona').value = j.persona || '';
      document.getElementById('detail-description').value = j.description || '';
      var badge = document.getElementById('detail-badge');
      badge.textContent = j.status;
      badge.className = 'badge badge-' + j.status;
      renderTimeline();
      renderLinkedTickets();
      loadRunResults(j);
      renderTimeline();
      document.getElementById('list-view').style.display = 'none';
      var dv = document.getElementById('detail-view');
      dv.style.display = 'block';
      dv.className = 'journey-detail active';
      history.pushState({{ journeyId: id }}, '', '/{pid}/journeys/' + id);
      document.getElementById('detail-title').onblur = saveJourneyMeta;
      document.getElementById('detail-persona').onblur = saveJourneyMeta;
      document.getElementById('detail-description').onblur = saveJourneyMeta;
    }});
  }}
  function saveJourneyMeta() {{
    if (!currentJourney) return;
    apiPut('/journeys/' + currentJourney.id, {{
      title: document.getElementById('detail-title').value,
      persona: document.getElementById('detail-persona').value,
      description: document.getElementById('detail-description').value,
    }});
  }}
  window.showList = function() {{
    currentJourney = null;
    document.getElementById('list-view').style.display = 'block';
    var dv = document.getElementById('detail-view');
    dv.style.display = 'none';
    dv.className = 'journey-detail';
    history.pushState({{}}, '', '/{pid}/journeys');
    loadList();
  }};

  /* ── Steps ───────────────────────────────────────────── */
  var ACTIONS = ['open','reload','click','double_click','fill','select','press','wait_for','assert_visible','assert_text','capture'];

  function _renderStepsTable() {{
    var tbody = document.getElementById('steps-body');
    if (!tbody) return;
    tbody.textContent = '';
    currentSteps.forEach(function(step, idx) {{
      var tr = document.createElement('tr');
      tr.setAttribute('data-testid', 'step-row-' + step.id);
      var target = '';
      try {{ var t = JSON.parse(step.target_json || '{{}}'); target = t.testid || t.css || t.text || t.title || t.role || ''; }} catch(e) {{}}
      var hasCapture = step.capture_json && step.capture_json !== '';
      var dotClass = 'pending';
      if (lastRunResults && lastRunResults[idx]) dotClass = lastRunResults[idx].status;

      // Build cells with DOM methods
      var numTd = document.createElement('td'); numTd.className = 'step-num'; numTd.textContent = idx + 1;
      var dotTd = document.createElement('td'); var dotEl = document.createElement('span'); dotEl.className = 'status-dot ' + dotClass; dotTd.appendChild(dotEl);
      var labelTd = document.createElement('td'); labelTd.className = 'label-cell'; labelTd.textContent = step.label || '(no label)';
      var actionTd = document.createElement('td'); actionTd.className = 'action-cell'; actionTd.textContent = step.action;
      var targetTd = document.createElement('td'); targetTd.className = 'target-cell'; targetTd.textContent = target || '\u2014'; targetTd.title = step.target_json || '';
      var capTd = document.createElement('td');
      if (hasCapture) {{ var capSpan = document.createElement('span'); capSpan.className = 'capture-icon'; capSpan.textContent = '[capture]'; capTd.appendChild(capSpan); }}
      var actTd = document.createElement('td'); actTd.className = 'actions-cell';
      var editBtn = document.createElement('button'); editBtn.className = 'btn btn-ghost btn-sm btn-icon'; editBtn.textContent = '\u270E'; editBtn.title = 'Edit';
      (function(sid) {{ editBtn.onclick = function() {{ toggleExpand(sid); }}; }})(step.id);
      var delBtn = document.createElement('button'); delBtn.className = 'btn btn-danger btn-sm btn-icon'; delBtn.textContent = '\u00D7'; delBtn.title = 'Remove';
      (function(sid) {{ delBtn.onclick = function() {{ removeStep(sid); }}; }})(step.id);
      actTd.appendChild(editBtn); actTd.appendChild(delBtn);

      tr.appendChild(numTd); tr.appendChild(dotTd); tr.appendChild(labelTd); tr.appendChild(actionTd); tr.appendChild(targetTd); tr.appendChild(capTd); tr.appendChild(actTd);
      tbody.appendChild(tr);

      // Expand row
      var expandTr = document.createElement('tr');
      expandTr.className = 'step-expand';
      expandTr.id = 'expand-' + step.id;
      var expandTd = document.createElement('td'); expandTd.colSpan = 7;
      var grid = document.createElement('div'); grid.className = 'field-grid';
      var targetObj = {{}};
      try {{ targetObj = JSON.parse(step.target_json || '{{}}'); }} catch(e) {{}}

      function makeField(labelText, val, onChange) {{
        var wrap = document.createElement('div');
        var lbl = document.createElement('label'); lbl.textContent = labelText; wrap.appendChild(lbl);
        var inp = document.createElement('input'); inp.value = val || ''; inp.onblur = function() {{ onChange(inp.value); }};
        wrap.appendChild(inp);
        return wrap;
      }}
      function makeSelect(labelText, options, current, onChange) {{
        var wrap = document.createElement('div');
        var lbl = document.createElement('label'); lbl.textContent = labelText; wrap.appendChild(lbl);
        var sel = document.createElement('select');
        options.forEach(function(o) {{
          var opt = document.createElement('option'); opt.value = o; opt.textContent = o;
          if (o === current) opt.selected = true;
          sel.appendChild(opt);
        }});
        sel.onchange = function() {{ onChange(sel.value); }};
        wrap.appendChild(sel);
        return wrap;
      }}

      (function(sid, stp) {{
        grid.appendChild(makeField('Label', stp.label, function(v) {{ updateField(sid, 'label', v); }}));
        grid.appendChild(makeSelect('Action', ACTIONS, stp.action, function(v) {{ updateField(sid, 'action', v); }}));
        grid.appendChild(makeField('Actor', stp.actor, function(v) {{ updateField(sid, 'actor', v); }}));
        grid.appendChild(makeField('Value', stp.value, function(v) {{ updateField(sid, 'value', v); }}));
        grid.appendChild(makeField('Target (testid)', targetObj.testid || '', function(v) {{ updateTarget(sid, 'testid', v); }}));
        grid.appendChild(makeField('Target (css)', targetObj.css || '', function(v) {{ updateTarget(sid, 'css', v); }}));
        grid.appendChild(makeField('Target (text)', targetObj.text || '', function(v) {{ updateTarget(sid, 'text', v); }}));
        grid.appendChild(makeField('Key (for press)', stp.key || '', function(v) {{ updateField(sid, 'key', v); }}));
      }})(step.id, step);

      expandTd.appendChild(grid);
      expandTr.appendChild(expandTd);
      tbody.appendChild(expandTr);
    }});
  }}

  function toggleExpand(stepId) {{
    var row = document.getElementById('expand-' + stepId);
    if (row) row.classList.toggle('active');
  }}

  function updateField(stepId, field, value) {{
    var body = {{}}; body[field] = value;
    apiPut('/journeys/' + currentJourney.id + '/steps/' + stepId, body).then(function(updated) {{
      var idx = currentSteps.findIndex(function(s) {{ return s.id === stepId; }});
      if (idx >= 0) currentSteps[idx] = updated;
    }});
  }}

  function updateTarget(stepId, key, value) {{
    var idx = currentSteps.findIndex(function(s) {{ return s.id === stepId; }});
    if (idx < 0) return;
    var target = {{}};
    try {{ target = JSON.parse(currentSteps[idx].target_json || '{{}}'); }} catch(e) {{}}
    if (value) target[key] = value; else delete target[key];
    apiPut('/journeys/' + currentJourney.id + '/steps/' + stepId, {{target: target}});
    currentSteps[idx].target_json = JSON.stringify(target);
  }}

  window.addStep = function() {{
    apiPost('/journeys/' + currentJourney.id + '/steps', {{action:'click',label:'New step'}}).then(function(r) {{
      if (r.status === 201) {{
        currentSteps.push(r.data);
        renderTimeline();
        var ex = document.getElementById('expand-' + r.data.id);
        if (ex) ex.classList.add('active');
      }}
    }});
  }};

  function removeStep(stepId) {{
    apiDel('/journeys/' + currentJourney.id + '/steps/' + stepId).then(function() {{
      currentSteps = currentSteps.filter(function(s) {{ return s.id !== stepId; }});
      renderTimeline();
    }});
  }}

  /* ── Validate & Run ──────────────────────────────────── */
  window.validateJourney = function() {{
    apiPost('/journeys/' + currentJourney.id + '/validate', {{}}).then(function(r) {{
      if (r.data.ok) toast('Validation passed');
      else toast(r.data.error || 'Validation failed', 'error');
    }});
  }};
  window.runJourney = function() {{
    toast('Starting run...');
    apiPost('/journeys/' + currentJourney.id + '/run', {{}}).then(function(r) {{
      if (r.data.error) {{ toast(r.data.error, 'error'); return; }}
      toast('Run started: ' + r.data.run_id);
      setTimeout(function() {{ openJourney(currentJourney.id); }}, 2000);
    }});
  }};

  /* ── Run Results ─────────────────────────────────────── */
  function loadRunResults(journey) {{
    var resultsDiv = document.getElementById('run-results');
    var historyDiv = document.getElementById('run-history');
    lastRunResults = null;
    var runs = journey.runs || [];
    if (runs.length === 0) {{ resultsDiv.style.display = 'none'; historyDiv.style.display = 'none'; return; }}

    var latest = runs[0];
    apiGet('/journeys/' + journey.id + '/runs/' + latest.id).then(function(data) {{
      var run = data.run, stepResults = data.step_results || [];
      lastRunResults = stepResults;
      renderTimeline();
      var statusEl = document.getElementById('run-status-label');
      statusEl.textContent = run.status === 'passed' ? '\\u2713 Passed' : run.status === 'failed' ? '\\u2717 Failed' : run.status;
      statusEl.className = 'run-status ' + run.status;
      document.getElementById('run-meta').textContent = (run.duration_ms ? run.duration_ms + 'ms' : '') + (run.started_at ? ' \\u2022 ' + timeAgo(run.started_at) : '');

      var timeline = document.getElementById('step-timeline');
      timeline.textContent = '';
      stepResults.forEach(function(sr, i) {{
        var step = document.createElement('div');
        step.className = 'tl-step';
        step.onclick = function() {{ showStepDetail(sr); }};
        var d = document.createElement('span'); d.className = 'status-dot ' + sr.status; step.appendChild(d);
        var n = document.createElement('span'); n.className = 'tl-num'; n.textContent = i + 1; step.appendChild(n);
        var l = document.createElement('span'); l.className = 'tl-label'; l.textContent = sr.label || sr.action; step.appendChild(l);
        timeline.appendChild(step);
      }});
      resultsDiv.style.display = 'block';
    }});

    if (runs.length >= 1) {{
      historyDiv.style.display = 'block';
      var histList = document.getElementById('run-history-list');
      histList.textContent = '';
      runs.forEach(function(run) {{
        var row = document.createElement('div');
        row.className = 'run-row';
        var d = document.createElement('span'); d.className = 'status-dot ' + run.status; row.appendChild(d);
        var rid = document.createElement('span'); rid.className = 'run-id'; rid.textContent = run.id; row.appendChild(rid);
        var t = document.createElement('span'); t.textContent = timeAgo(run.started_at); row.appendChild(t);
        var dur = document.createElement('span'); dur.textContent = run.duration_ms ? run.duration_ms + 'ms' : ''; row.appendChild(dur);
        row.onclick = function() {{ loadRunDetail(journey.id, run.id); }};
        histList.appendChild(row);
      }});
    }} else {{ historyDiv.style.display = 'none'; }}
  }}

  function showStepDetail(sr) {{
    var detail = document.getElementById('step-result-detail');
    detail.textContent = '';
    if (sr.status === 'failed' && sr.error_message) {{
      var errDiv = document.createElement('div'); errDiv.className = 'error-msg'; errDiv.textContent = sr.error_message;
      detail.appendChild(errDiv);
    }} else if (sr.screenshot_path) {{
      var img = document.createElement('img'); img.src = API_PREFIX + sr.screenshot_path; img.alt = 'Screenshot';
      detail.appendChild(img);
    }} else {{
      var noData = document.createElement('span'); noData.style.color = 'var(--text-tertiary)'; noData.textContent = 'No details for this step';
      detail.appendChild(noData);
    }}
    detail.style.display = 'block';
  }}

  function loadRunDetail(journeyId, runId) {{
    apiGet('/journeys/' + journeyId + '/runs/' + runId).then(function(data) {{
      var run = data.run, stepResults = data.step_results || [];
      var statusEl = document.getElementById('run-status-label');
      statusEl.textContent = run.status === 'passed' ? '\\u2713 Passed' : run.status === 'failed' ? '\\u2717 Failed' : run.status;
      statusEl.className = 'run-status ' + run.status;
      document.getElementById('run-meta').textContent = (run.duration_ms ? run.duration_ms + 'ms' : '') + (run.started_at ? ' \\u2022 ' + timeAgo(run.started_at) : '');
      var timeline = document.getElementById('step-timeline');
      timeline.textContent = '';
      stepResults.forEach(function(sr, i) {{
        var step = document.createElement('div'); step.className = 'tl-step';
        step.onclick = function() {{ showStepDetail(sr); }};
        var d = document.createElement('span'); d.className = 'status-dot ' + sr.status; step.appendChild(d);
        var n = document.createElement('span'); n.className = 'tl-num'; n.textContent = i + 1; step.appendChild(n);
        var l = document.createElement('span'); l.className = 'tl-label'; l.textContent = sr.label || sr.action; step.appendChild(l);
        timeline.appendChild(step);
      }});
    }});
  }}

  /* ── Delete ──────────────────────────────────────────── */
  window.deleteJourney = function() {{
    if (!confirm('Delete journey "' + currentJourney.title + '"?')) return;
    apiDel('/journeys/' + currentJourney.id).then(function() {{ toast('Deleted'); showList(); }});
  }};

  /* ── Linked Tickets ───────────────────────────────────── */
  function renderLinkedTickets() {{
    var container = document.getElementById('linked-tickets');
    container.textContent = '';
    var links = (currentJourney && currentJourney.linked_tickets) || [];
    if (links.length === 0) {{
      var noLinks = document.createElement('span');
      noLinks.style.color = 'var(--text-tertiary)';
      noLinks.textContent = 'No tickets linked yet';
      container.appendChild(noLinks);
      return;
    }}
    links.forEach(function(link) {{
      var row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0;';
      var idSpan = document.createElement('span');
      idSpan.style.cssText = 'font-family:"SF Mono",Monaco,monospace;color:var(--accent);';
      idSpan.textContent = link.ticket_id;
      var unlinkBtn = document.createElement('button');
      unlinkBtn.className = 'btn btn-danger btn-sm btn-icon';
      unlinkBtn.textContent = '\u00D7';
      unlinkBtn.title = 'Unlink';
      (function(tid) {{
        unlinkBtn.onclick = function() {{
          apiDel('/journeys/' + currentJourney.id + '/link/' + tid).then(function() {{
            currentJourney.linked_tickets = currentJourney.linked_tickets.filter(function(l) {{ return l.ticket_id !== tid; }});
            renderLinkedTickets();
            toast('Unlinked ' + tid);
          }});
        }};
      }})(link.ticket_id);
      row.appendChild(idSpan);
      row.appendChild(unlinkBtn);
      container.appendChild(row);
    }});
  }}

  window.linkTicket = function() {{
    var ticketId = prompt('Ticket ID to link (e.g. B-01):');
    if (!ticketId) return;
    apiPost('/journeys/' + currentJourney.id + '/link', {{ticket_id: ticketId.trim()}}).then(function(r) {{
      if (r.data.ok) {{
        if (!currentJourney.linked_tickets) currentJourney.linked_tickets = [];
        currentJourney.linked_tickets.push({{ticket_id: ticketId.trim(), journey_id: currentJourney.id, project_id: '', step_id: null}});
        renderLinkedTickets();
        toast('Linked ' + ticketId);
      }} else {{
        toast(r.data.error || 'Failed', 'error');
      }}
    }});
  }};

  /* ── Infer from Tickets ───────────────────────────────── */
  window.inferJourneys = function() {{
    toast('Analyzing tickets...');
    apiPost('/journeys/infer', {{}}).then(function(r) {{
      var suggestions = r.data.suggestions || [];
      if (suggestions.length === 0) {{
        toast('No suggestions \u2014 add some tickets first', 'error');
        return;
      }}
      // Show suggestions as a list with approve buttons
      var list = document.getElementById('journey-list');
      list.textContent = '';
      var header = document.createElement('div');
      header.style.cssText = 'margin-bottom:12px;display:flex;align-items:center;justify-content:space-between;';
      var h = document.createElement('h3');
      h.style.cssText = 'font-size:14px;font-weight:600;';
      h.textContent = 'Suggested Journeys (' + suggestions.length + ')';
      var cancelBtn = document.createElement('button');
      cancelBtn.className = 'btn btn-ghost btn-sm';
      cancelBtn.textContent = 'Cancel';
      cancelBtn.onclick = function() {{ loadList(); }};
      header.appendChild(h);
      header.appendChild(cancelBtn);
      list.appendChild(header);

      suggestions.forEach(function(s) {{
        var card = document.createElement('div');
        card.className = 'journey-card';
        card.style.cursor = 'default';
        var topRow = document.createElement('div');
        topRow.className = 'top-row';
        var titleSpan = document.createElement('span');
        titleSpan.className = 'title';
        titleSpan.textContent = s.title;
        var approveBtn = document.createElement('button');
        approveBtn.className = 'btn btn-success btn-sm';
        approveBtn.textContent = 'Approve';
        approveBtn.onclick = function() {{ approveSuggestion(s); }};
        topRow.appendChild(titleSpan);
        topRow.appendChild(approveBtn);
        card.appendChild(topRow);
        var meta = document.createElement('div');
        meta.className = 'meta';
        var descSpan = document.createElement('span');
        descSpan.textContent = s.description;
        var stepsSpan = document.createElement('span');
        stepsSpan.textContent = s.steps.length + ' steps';
        var personaSpan = document.createElement('span');
        personaSpan.className = 'persona';
        personaSpan.textContent = s.persona;
        meta.appendChild(descSpan);
        meta.appendChild(stepsSpan);
        meta.appendChild(personaSpan);
        card.appendChild(meta);
        list.appendChild(card);
      }});
    }});
  }};

  function approveSuggestion(s) {{
    apiPost('/journeys', {{title:s.title, description:s.description, persona:s.persona}}).then(function(r) {{
      if (r.status !== 201) {{ toast(r.data.error || 'Failed', 'error'); return; }}
      var jid = r.data.id;
      // Update journey metadata
      apiPut('/journeys/' + jid, {{
        actors_json: s.actors_json,
        seed_json: s.seed_json,
      }});
      // Add steps sequentially
      var chain = Promise.resolve();
      s.steps.forEach(function(step) {{
        chain = chain.then(function() {{
          return apiPost('/journeys/' + jid + '/steps', {{
            action: step.action,
            label: step.label || '',
            actor: step.actor || 'user',
            target: step.target,
            value: step.value || '',
            key: step.key || '',
            capture: step.capture,
          }});
        }});
      }});
      chain.then(function() {{
        toast('Journey created: ' + s.title);
        loadList();
      }});
    }});
  }}

  /* ── Init ────────────────────────────────────────────── */
  loadList();

  // Auto-open journey from URL
  var openId = '{open_journey_id}';
  if (openId) {{
    setTimeout(function() {{ openJourney(openId); }}, 200);
  }}

  // Browser back/forward
  window.addEventListener('popstate', function(e) {{
    if (e.state && e.state.journeyId) {{
      openJourney(e.state.journeyId);
    }} else {{
      showList();
    }}
  }});
}})();
</script>
</body>
</html>'''


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
<div style="margin-top:32px;padding-top:16px;border-top:1px solid var(--border-default);">
  <h3 style="font-size:14px;font-weight:600;color:var(--text-primary);margin-bottom:12px;">Scenarios</h3>
  <div id="scenarios-section">
    <p style="color:var(--text-secondary);font-size:12px;">Loading scenarios...</p>
  </div>
</div>
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
<script>
// Scenarios section
(function() {{
  var section = document.getElementById('scenarios-section');
  if (!section) return;
  var pid = '{pid}';

  function renderScenarios(data) {{
    if (!data.scenarios || data.scenarios.length === 0) {{
      section.innerHTML = '<p style="color:var(--text-secondary);font-size:12px;">No scenario manifests found in tests/scenarios/</p>';
      return;
    }}
    var html = '';
    data.scenarios.forEach(function(s) {{
      var tags = (s.tags || []).map(function(t) {{
        return '<span style="display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;background:var(--bg-hover);color:var(--text-secondary);margin-right:4px;">' + t + '</span>';
      }}).join('');
      var lastRun = s.last_run ? '<span style="font-size:11px;color:' + (s.last_run.status === 'passed' ? '#22c55e' : s.last_run.status === 'failed' ? '#ef4444' : 'var(--text-secondary)') + ';">' + s.last_run.status + '</span>' : '';
      html += '<div class="scenario-row" data-scenario-id="' + s.id + '" style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border-default);">'
        + '<div style="flex:1;"><div style="font-size:13px;font-weight:500;color:var(--text-primary);">' + s.title + '</div><div style="margin-top:2px;">' + tags + '</div></div>'
        + '<div style="min-width:60px;text-align:right;">' + lastRun + '</div>'
        + '<div id="run-result-' + s.id + '"></div>'
        + '<button onclick="runScenario(\'' + s.id + '\', false)" style="font-size:11px;padding:4px 10px;border-radius:4px;border:1px solid var(--border-default);background:none;color:var(--text-primary);cursor:pointer;">Run</button>'
        + '<button onclick="runScenario(\'' + s.id + '\', true)" style="font-size:11px;padding:4px 10px;border-radius:4px;border:1px solid var(--border-default);background:none;color:var(--accent);cursor:pointer;">Run + Publish</button>'
        + '</div>';
    }});
    section.innerHTML = html;
  }}

  fetch('/' + pid + '/api/scenarios').then(function(r) {{ return r.json(); }}).then(renderScenarios).catch(function() {{
    section.innerHTML = '<p style="color:var(--text-secondary);font-size:12px;">Failed to load scenarios</p>';
  }});

  window.runScenario = function(scenarioId, publish) {{
    var resultEl = document.getElementById('run-result-' + scenarioId);
    if (resultEl) resultEl.innerHTML = '<span style="font-size:11px;color:var(--text-secondary);">Starting...</span>';

    fetch('/' + pid + '/api/scenarios/run', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{scenario_id: scenarioId, publish: publish}})
    }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
      if (data.error) {{
        if (resultEl) resultEl.innerHTML = '<span style="font-size:11px;color:#ef4444;">' + data.error + '</span>';
        return;
      }}
      pollRun(data.run_id, scenarioId);
    }});
  }};

  function pollRun(runId, scenarioId) {{
    var resultEl = document.getElementById('run-result-' + scenarioId);
    var interval = setInterval(function() {{
      fetch('/' + pid + '/api/scenarios/runs/' + runId).then(function(r) {{ return r.json(); }}).then(function(data) {{
        if (data.status === 'running') {{
          if (resultEl) resultEl.innerHTML = '<span style="font-size:11px;color:var(--text-secondary);">Running...</span>';
          return;
        }}
        clearInterval(interval);
        var color = data.status === 'passed' ? '#22c55e' : '#ef4444';
        var html = '<span style="font-size:11px;color:' + color + ';font-weight:600;">' + data.status + '</span>';
        if (data.summary && data.summary.screenshots && data.summary.screenshots.length > 0) {{
          html += '<div style="display:flex;gap:4px;margin-top:4px;">';
          data.summary.screenshots.forEach(function(spath) {{
            var fname = spath.split('/').pop();
            html += '<img src="/' + pid + '/api/scenarios/runs/' + runId + '/artifacts/' + fname + '" style="width:60px;height:40px;object-fit:cover;border-radius:4px;border:1px solid var(--border-default);" title="' + fname + '">';
          }});
          html += '</div>';
        }}
        if (resultEl) resultEl.innerHTML = html;
      }});
    }}, 2000);
  }}
}})();
</script>
<script>
// Scenario Drafting section
(function() {{
  var pid = '{pid}';

  function escHtml(s) {{
    var d = document.createElement('div');
    d.appendChild(document.createTextNode(String(s)));
    return d.innerHTML;
  }}

  // Build and insert the drafting panel before the danger zone
  var dangerZone = document.querySelector('.danger');
  if (!dangerZone) return;

  var draftSection = document.createElement('div');
  draftSection.style.cssText = 'margin-top:32px;padding-top:16px;border-top:1px solid var(--border-default);';

  var heading = document.createElement('h3');
  heading.style.cssText = 'font-size:14px;font-weight:600;color:var(--text-primary);margin-bottom:12px;';
  heading.textContent = 'Generate Draft Scenario';
  draftSection.appendChild(heading);

  var hint = document.createElement('p');
  hint.style.cssText = 'color:var(--text-secondary);font-size:12px;margin-bottom:10px;';
  hint.textContent = 'Describe what the scenario should demonstrate in plain language.';
  draftSection.appendChild(hint);

  var inputRow = document.createElement('div');
  inputRow.style.cssText = 'display:flex;gap:8px;align-items:flex-start;';

  var goalInput = document.createElement('textarea');
  goalInput.id = 'draft-goal';
  goalInput.rows = 2;
  goalInput.placeholder = 'e.g. user creates a ticket and moves it to WIP';
  goalInput.style.cssText = 'flex:1;resize:vertical;font-size:13px;';

  var draftBtn = document.createElement('button');
  draftBtn.id = 'draft-btn';
  draftBtn.style.cssText = 'padding:8px 14px;font-size:13px;border-radius:6px;border:1px solid rgba(59,130,246,0.3);background:rgba(59,130,246,0.12);color:var(--accent);cursor:pointer;white-space:nowrap;font-family:inherit;';
  draftBtn.textContent = 'Generate Drafts';

  inputRow.appendChild(goalInput);
  inputRow.appendChild(draftBtn);
  draftSection.appendChild(inputRow);

  var draftResults = document.createElement('div');
  draftResults.id = 'draft-results';
  draftResults.style.marginTop = '12px';
  draftSection.appendChild(draftResults);

  dangerZone.parentNode.insertBefore(draftSection, dangerZone);

  function confidenceBadge(c) {{
    var color = c === 'high' ? '#22c55e' : c === 'medium' ? '#f59e0b' : '#ef4444';
    var span = document.createElement('span');
    span.style.cssText = 'display:inline-block;padding:1px 7px;border-radius:10px;font-size:10px;font-weight:600;background:' + color + '22;color:' + color + ';border:1px solid ' + color + '44;';
    span.textContent = c;
    return span;
  }}

  var lastCandidates = [];

  window.previewDraft = function(idx) {{
    var pre = document.getElementById('draft-preview-' + idx);
    if (!pre) return;
    if (pre.style.display === 'none') {{
      pre.textContent = JSON.stringify(lastCandidates[idx] && lastCandidates[idx].manifest, null, 2);
      pre.style.display = 'block';
    }} else {{
      pre.style.display = 'none';
    }}
  }};

  window.approveDraft = function(idx) {{
    var c = lastCandidates[idx];
    if (!c) return;
    var msgEl = document.getElementById('draft-approve-msg-' + idx);
    if (msgEl) {{ msgEl.style.color = 'var(--text-secondary)'; msgEl.textContent = 'Saving...'; }}
    fetch('/' + pid + '/api/scenarios/drafts/approve', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ manifest: c.manifest, filename: c.manifest.id + '.json' }})
    }}).then(function(r) {{ return r.json().then(function(j) {{ return {{ok: r.ok, data: j}}; }}); }})
    .then(function(res) {{
      if (!msgEl) return;
      if (res.ok) {{
        msgEl.style.color = '#22c55e';
        msgEl.textContent = 'Saved as ' + (res.data.filename || '?');
      }} else {{
        msgEl.style.color = '#ef4444';
        msgEl.textContent = res.data.error || 'Failed';
      }}
    }}).catch(function() {{
      if (msgEl) {{ msgEl.style.color = '#ef4444'; msgEl.textContent = 'Network error'; }}
    }});
  }};

  function buildCandidateCard(c, i) {{
    var card = document.createElement('div');
    card.id = 'draft-candidate-' + i;
    card.style.cssText = 'margin-bottom:12px;padding:12px;border-radius:8px;border:1px solid var(--border-default);background:var(--bg-card);';

    var titleRow = document.createElement('div');
    titleRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:6px;';

    var titleEl = document.createElement('span');
    titleEl.style.cssText = 'font-size:13px;font-weight:600;color:var(--text-primary);flex:1;';
    titleEl.textContent = c.title;
    titleRow.appendChild(titleEl);
    titleRow.appendChild(confidenceBadge(c.confidence));
    card.appendChild(titleRow);

    var summaryEl = document.createElement('div');
    summaryEl.style.cssText = 'font-size:12px;color:var(--text-secondary);margin-bottom:6px;';
    summaryEl.textContent = c.summary;
    card.appendChild(summaryEl);

    if (c.assumptions && c.assumptions.length) {{
      var details = document.createElement('details');
      details.style.marginBottom = '4px';
      var summary = document.createElement('summary');
      summary.style.cssText = 'font-size:11px;color:var(--text-tertiary);cursor:pointer;';
      summary.textContent = 'Assumptions (' + c.assumptions.length + ')';
      details.appendChild(summary);
      var ul = document.createElement('ul');
      ul.style.cssText = 'margin:4px 0 0 14px;font-size:11px;color:var(--text-secondary);';
      c.assumptions.forEach(function(a) {{
        var li = document.createElement('li');
        li.textContent = a;
        ul.appendChild(li);
      }});
      details.appendChild(ul);
      card.appendChild(details);
    }}

    if (c.prerequisites && c.prerequisites.length) {{
      var prereqBox = document.createElement('div');
      prereqBox.style.cssText = 'margin-bottom:6px;padding:6px 10px;border-radius:4px;background:rgba(239,68,68,0.06);border:1px solid rgba(239,68,68,0.2);';
      var prereqHead = document.createElement('div');
      prereqHead.style.cssText = 'font-size:10px;font-weight:600;color:#ef4444;margin-bottom:2px;';
      prereqHead.textContent = 'Prerequisites / Blockers';
      prereqBox.appendChild(prereqHead);
      c.prerequisites.forEach(function(p) {{
        var pEl = document.createElement('div');
        pEl.style.cssText = 'font-size:11px;color:#ef4444cc;margin-top:1px;';
        pEl.textContent = p;
        prereqBox.appendChild(pEl);
      }});
      card.appendChild(prereqBox);
    }}

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;gap:8px;margin-top:8px;align-items:center;';

    var approveBtn = document.createElement('button');
    approveBtn.style.cssText = 'font-size:11px;padding:4px 12px;border-radius:4px;border:1px solid rgba(34,197,94,0.35);background:rgba(34,197,94,0.08);color:#22c55e;cursor:pointer;';
    approveBtn.textContent = 'Approve & Save';
    approveBtn.addEventListener('click', function() {{ window.approveDraft(i); }});

    var previewBtn = document.createElement('button');
    previewBtn.style.cssText = 'font-size:11px;padding:4px 10px;border-radius:4px;border:1px solid var(--border-default);background:none;color:var(--text-secondary);cursor:pointer;';
    previewBtn.textContent = 'Preview JSON';
    previewBtn.addEventListener('click', function() {{ window.previewDraft(i); }});

    var approveMsg = document.createElement('span');
    approveMsg.id = 'draft-approve-msg-' + i;
    approveMsg.style.cssText = 'font-size:11px;';

    btnRow.appendChild(approveBtn);
    btnRow.appendChild(previewBtn);
    btnRow.appendChild(approveMsg);
    card.appendChild(btnRow);

    var pre = document.createElement('pre');
    pre.id = 'draft-preview-' + i;
    pre.style.cssText = 'display:none;margin-top:8px;padding:8px;border-radius:4px;background:var(--bg-page);font-size:10px;color:var(--text-secondary);overflow-x:auto;max-height:200px;';
    card.appendChild(pre);

    return card;
  }}

  function renderDraftResults(data) {{
    while (draftResults.firstChild) draftResults.removeChild(draftResults.firstChild);

    if (data.warnings && data.warnings.length) {{
      var warnBox = document.createElement('div');
      warnBox.style.cssText = 'margin-bottom:10px;padding:8px 12px;border-radius:6px;background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.25);';
      var warnHead = document.createElement('div');
      warnHead.style.cssText = 'font-size:11px;font-weight:600;color:#f59e0b;margin-bottom:4px;';
      warnHead.textContent = 'Warnings';
      warnBox.appendChild(warnHead);
      data.warnings.forEach(function(w) {{
        var wEl = document.createElement('div');
        wEl.style.cssText = 'font-size:12px;color:var(--text-secondary);margin-top:2px;';
        wEl.textContent = w;
        warnBox.appendChild(wEl);
      }});
      draftResults.appendChild(warnBox);
    }}

    if (data.intent_summary) {{
      var intentEl = document.createElement('div');
      intentEl.style.cssText = 'font-size:11px;color:var(--text-tertiary);margin-bottom:10px;';
      intentEl.textContent = data.intent_summary;
      draftResults.appendChild(intentEl);
    }}

    lastCandidates = data.candidates || [];
    if (!lastCandidates.length) {{
      var noResults = document.createElement('p');
      noResults.style.cssText = 'color:var(--text-secondary);font-size:12px;';
      noResults.textContent = 'No candidates generated.';
      draftResults.appendChild(noResults);
      return;
    }}
    lastCandidates.forEach(function(c, i) {{
      draftResults.appendChild(buildCandidateCard(c, i));
    }});
  }}

  draftBtn.addEventListener('click', function() {{
    var goal = goalInput.value.trim();
    if (!goal) return;
    while (draftResults.firstChild) draftResults.removeChild(draftResults.firstChild);
    var loading = document.createElement('p');
    loading.style.cssText = 'color:var(--text-secondary);font-size:12px;';
    loading.textContent = 'Generating...';
    draftResults.appendChild(loading);

    fetch('/' + pid + '/api/scenarios/draft', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ goal: goal }})
    }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
      if (data.error) {{
        while (draftResults.firstChild) draftResults.removeChild(draftResults.firstChild);
        var errEl = document.createElement('p');
        errEl.style.cssText = 'color:#ef4444;font-size:12px;';
        errEl.textContent = data.error;
        draftResults.appendChild(errEl);
        return;
      }}
      renderDraftResults(data);
    }}).catch(function() {{
      while (draftResults.firstChild) draftResults.removeChild(draftResults.firstChild);
      var errEl = document.createElement('p');
      errEl.style.cssText = 'color:#ef4444;font-size:12px;';
      errEl.textContent = 'Request failed';
      draftResults.appendChild(errEl);
    }});
  }});
}})();
</script>
<div id="confirm-modal\" style="display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px);align-items:center;justify-content:center;">
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


def _aggregate_kitchen_state() -> dict:
    """Aggregate Kitchen state across all registered projects.

    Returns the shape consumed by /api/kitchen and _render_kitchen_view:

      {
        "buckets": {
          "needs_me": [...], "running": [...], "ready_to_delegate": [...],
          "held": [...], "failed": [...],
        },
        "projects": [{id, name, counts: {wip, review, blocked, running, needs_me}}, ...],
      }

    Each item is {project_id, project_name, ticket_id, title, section, status,
                  automation_mode, latest_run_status, hold_reason, eligibility_reasons?}.
    Subjects appear in at most one bucket, with priority needs_me > running >
    ready_to_delegate > held > failed.
    """
    from actions import eligibility as _elig

    with _PROJECTS_CACHE_LOCK:
        # Skip projects with watched=false. Default (missing key) is true so
        # the aggregator includes everything by default.
        projects = [p for p in _PROJECTS_CACHE.values() if p.get("watched", True)]

    buckets = {k: [] for k in ("needs_me", "running", "ready_to_delegate", "held", "failed")}
    project_summaries = []

    with _db_lock:
        conn = get_db()
        init_db(conn)

        for proj in projects:
            pid = proj["id"]
            pname = proj.get("name", pid)
            counts = {"wip": 0, "review": 0, "blocked": 0, "running": 0, "needs_me": 0}

            tickets = conn.execute(
                "SELECT id, title, section, status FROM tickets "
                "WHERE project_id = ? AND archived = 0 AND draft = 0",
                (pid,),
            ).fetchall()

            for t in tickets:
                tid = t["id"]
                if t["section"] == "WIP":
                    counts["wip"] += 1
                elif t["section"] == "For Review":
                    counts["review"] += 1
                if t["status"] == "blocked":
                    counts["blocked"] += 1

                am_row = conn.execute(
                    "SELECT automation_mode, hold_reason FROM automation_subjects "
                    "WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ?",
                    (pid, tid),
                ).fetchone()
                mode = am_row["automation_mode"] if am_row else "manual"
                hold_reason = am_row["hold_reason"] if am_row else None

                latest = conn.execute(
                    "SELECT status FROM runs WHERE project_id = ? AND subject_type='ticket' AND subject_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (pid, tid),
                ).fetchone()
                run_status = latest["status"] if latest else None

                if run_status in ("preparing", "running"):
                    counts["running"] += 1
                if run_status == "needs_input":
                    counts["needs_me"] += 1

                base_item = {
                    "project_id": pid, "project_name": pname,
                    "ticket_id": tid, "title": t["title"],
                    "section": t["section"], "status": t["status"],
                    "automation_mode": mode, "latest_run_status": run_status,
                    "hold_reason": hold_reason,
                }

                # Bucket assignment with single-priority placement.
                if run_status == "needs_input":
                    buckets["needs_me"].append(base_item)
                elif run_status in ("preparing", "running"):
                    buckets["running"].append(base_item)
                elif run_status in ("failed", "stalled"):
                    buckets["failed"].append(base_item)
                elif mode == "held":
                    buckets["held"].append(base_item)
                elif mode == "auto":
                    # Eligibility check — only show in Ready To Delegate when
                    # there's no active run AND DCSTL gates pass.
                    try:
                        er = _elig(conn, pid, "ticket", tid)
                        if er.eligible:
                            buckets["ready_to_delegate"].append(base_item)
                    except Exception:
                        pass

            project_summaries.append({
                "id": pid, "name": pname,
                "path": proj.get("path", ""),
                "counts": counts,
            })

        conn.close()

    # Sort each bucket: most-recent activity first by ticket id desc
    # (proxy for recency without joining timestamps; cheap, deterministic).
    for k in buckets:
        buckets[k].sort(key=lambda x: (x["project_id"], x["ticket_id"]))

    return {"buckets": buckets, "projects": project_summaries}


def _render_kitchen_view(port: int) -> str:
    """Render the Kitchen landing page — cross-project work surface."""
    state = _aggregate_kitchen_state()
    bucket_titles = [
        ("needs_me",          "Needs Me",          "Paused for human input or eligible-but-failed."),
        ("running",           "Running",           "Active runs — agents currently cooking."),
        ("ready_to_delegate", "Ready To Delegate", "Auto + all gates clear — waiting for a slot."),
        ("held",              "Held",              "Paused intentionally, with a reason."),
        ("failed",            "Failed",            "Last run failed or stalled — needs attention."),
    ]

    def _items_html(items: list) -> str:
        if not items:
            return '<div class="kv-empty">Nothing here.</div>'
        rows = []
        for it in items:
            ticket_url = f"/{it['project_id']}/?ticket={it['ticket_id']}"
            mode_html = f'<span class="kv-mode kv-mode-{it["automation_mode"]}">{it["automation_mode"]}</span>'
            run_html = (
                f'<span class="kv-run kv-run-{it["latest_run_status"]}">{it["latest_run_status"]}</span>'
                if it["latest_run_status"] else ""
            )
            hold_html = (
                f'<span class="kv-hold-reason" title="{_html.escape(it["hold_reason"] or "")}">— {_html.escape(it["hold_reason"] or "")}</span>'
                if it["hold_reason"] else ""
            )
            rows.append(
                f'<a class="kv-row" href="{ticket_url}">'
                f'<span class="kv-tid">{_html.escape(it["ticket_id"])}</span>'
                f'<span class="kv-title">{_html.escape(it["title"])}</span>'
                f'<span class="kv-proj">{_html.escape(it["project_name"])}</span>'
                f'{mode_html}{run_html}{hold_html}'
                f'</a>'
            )
        return "".join(rows)

    sections_html = ""
    for key, title, desc in bucket_titles:
        items = state["buckets"][key]
        sections_html += (
            f'<section class="kv-bucket" data-bucket="{key}">'
            f'  <header class="kv-bucket-header">'
            f'    <h2>{_html.escape(title)} <span class="kv-count">{len(items)}</span></h2>'
            f'    <span class="kv-bucket-desc">{_html.escape(desc)}</span>'
            f'  </header>'
            f'  <div class="kv-bucket-list">{_items_html(items)}</div>'
            f'</section>'
        )

    project_rows = []
    for p in state["projects"]:
        c = p["counts"]
        project_rows.append(
            f'<a class="kv-project-row" href="/{p["id"]}/">'
            f'  <span class="kv-project-name">{_html.escape(p["name"])}</span>'
            f'  <span class="kv-project-stat">WIP <strong>{c["wip"]}</strong></span>'
            f'  <span class="kv-project-stat">Review <strong>{c["review"]}</strong></span>'
            f'  <span class="kv-project-stat">Blocked <strong>{c["blocked"]}</strong></span>'
            f'  <span class="kv-project-stat">Running <strong>{c["running"]}</strong></span>'
            f'  <span class="kv-project-stat">Needs Me <strong>{c["needs_me"]}</strong></span>'
            f'</a>'
        )
    projects_html = "".join(project_rows) or '<div class="kv-empty">No projects registered. Add one from <a href="/projects">the project picker</a>.</div>'

    return f"""<!doctype html>
<html data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kitchen — Ticket Takeaway</title>
<script>
(function () {{
  var t = localStorage.getItem('tt-theme') || 'system';
  var dark = t === 'dark' || (t === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
}})();
</script>
<style>
:root[data-theme="dark"] {{
  --bg: #0a0a0d; --surface: #16161a; --border: #2a2a32;
  --text: #e8e8ec; --text-2: #9b9ba6; --text-3: #6e6e7a;
  --accent: #6b9eff;
}}
:root[data-theme="light"] {{
  --bg: #f8f8fb; --surface: #ffffff; --border: #e5e5ec;
  --text: #1a1a22; --text-2: #5f5f6e; --text-3: #8b8b96;
  --accent: #3b82f6;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; }}
.kv-page {{ max-width: 1100px; margin: 0 auto; padding: 24px 20px 60px; }}
.kv-header {{ display: flex; align-items: baseline; gap: 16px; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
.kv-header h1 {{ margin: 0; font-size: 22px; font-weight: 700; }}
.kv-header-sub {{ color: var(--text-3); font-size: 13px; }}
.kv-header-nav {{ margin-left: auto; display: flex; gap: 12px; }}
.kv-header-nav a {{ color: var(--text-2); text-decoration: none; font-size: 13px; padding: 4px 10px; border-radius: 6px; border: 1px solid transparent; }}
.kv-header-nav a:hover {{ color: var(--text); border-color: var(--border); }}
.kv-bucket {{ margin-bottom: 22px; }}
.kv-bucket-header {{ display: flex; align-items: baseline; gap: 12px; margin-bottom: 8px; }}
.kv-bucket-header h2 {{ margin: 0; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-2); font-weight: 700; }}
.kv-bucket-header .kv-count {{ font-size: 11px; padding: 1px 7px; border-radius: 10px; background: var(--surface); color: var(--text-3); border: 1px solid var(--border); }}
.kv-bucket-desc {{ color: var(--text-3); font-size: 12px; }}
.kv-bucket-list {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
.kv-empty {{ padding: 14px 16px; color: var(--text-3); font-size: 12px; font-style: italic; }}
.kv-row {{ display: grid; grid-template-columns: 70px 1fr 140px auto auto auto; gap: 10px; padding: 8px 14px; align-items: center; border-bottom: 1px solid var(--border); color: var(--text); text-decoration: none; font-size: 13px; }}
.kv-row:last-child {{ border-bottom: 0; }}
.kv-row:hover {{ background: rgba(255,255,255,0.03); }}
.kv-tid {{ font-family: ui-monospace, SF Mono, monospace; font-size: 11px; color: var(--accent); opacity: 0.75; }}
.kv-title {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.kv-proj {{ font-size: 11px; color: var(--text-3); }}
.kv-mode, .kv-run {{ font-size: 10px; padding: 1px 6px; border-radius: 8px; font-weight: 700; text-transform: uppercase; }}
.kv-mode-auto {{ background: rgba(59,130,246,0.18); color: #6b9eff; }}
.kv-mode-held {{ background: rgba(245,158,11,0.18); color: #f59e0b; }}
.kv-mode-manual {{ display: none; }}
.kv-run-running, .kv-run-preparing, .kv-run-queued {{ background: rgba(59,130,246,0.18); color: #6b9eff; }}
.kv-run-needs_input {{ background: rgba(245,158,11,0.22); color: #f59e0b; }}
.kv-run-failed, .kv-run-stalled {{ background: rgba(239,68,68,0.18); color: #ef4444; }}
.kv-run-cancelled {{ background: rgba(107,114,128,0.18); color: #9ca3af; }}
.kv-hold-reason {{ font-size: 11px; color: var(--text-3); font-style: italic; }}
.kv-projects-section {{ margin-top: 32px; }}
.kv-project-row {{ display: grid; grid-template-columns: 1fr repeat(5, 110px); gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--border); color: var(--text); text-decoration: none; align-items: center; font-size: 13px; }}
.kv-project-row:last-child {{ border-bottom: 0; }}
.kv-project-row:hover {{ background: rgba(255,255,255,0.03); }}
.kv-project-name {{ font-weight: 600; }}
.kv-project-stat {{ font-size: 11px; color: var(--text-3); text-align: right; }}
.kv-project-stat strong {{ color: var(--text); margin-left: 4px; font-variant-numeric: tabular-nums; }}
</style>
</head>
<body>
<div class="kv-page">
  <header class="kv-header">
    <h1>Kitchen</h1>
    <span class="kv-header-sub">Cross-project work surface — what needs me, what's running, what's ready.</span>
    <nav class="kv-header-nav">
      <a href="/projects">All Projects</a>
    </nav>
  </header>
  {sections_html}
  <section class="kv-projects-section">
    <header class="kv-bucket-header">
      <h2>Projects</h2>
      <span class="kv-bucket-desc">Per-project health snapshot.</span>
    </header>
    <div class="kv-bucket-list">{projects_html}</div>
  </section>
</div>
</body>
</html>"""


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
.browse-btn {{ display: inline-block; padding: 8px 16px; background: rgba(59,130,246,0.12); border: 1px solid rgba(59,130,246,0.3); color: var(--accent); border-radius: 6px; cursor: pointer; font-size: 13px; font-family: inherit; }}
.browse-btn:hover {{ background: rgba(59,130,246,0.22); }}
.path-display {{ font-family: monospace; font-size: 13px; color: var(--text-secondary); padding: 8px 0 0; min-height: 20px; }}
.picker-overlay {{ display: none; position: fixed; inset: 0; z-index: 1000; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px); align-items: center; justify-content: center; }}
.picker-overlay.visible {{ display: flex; }}
.picker-box {{ background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 12px; padding: 20px; width: 460px; max-width: 90vw; max-height: 70vh; display: flex; flex-direction: column; box-shadow: 0 8px 32px rgba(0,0,0,0.5); }}
.picker-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }}
.picker-header h3 {{ font-size: 14px; font-weight: 600; }}
.picker-breadcrumb {{ font-size: 11px; color: var(--text-tertiary); font-family: monospace; margin-bottom: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.picker-list {{ flex: 1; overflow-y: auto; border: 1px solid var(--border-subtle); border-radius: 8px; }}
.picker-item {{ display: flex; align-items: center; gap: 8px; padding: 8px 12px; cursor: pointer; font-size: 13px; border-bottom: 1px solid var(--border-subtle); }}
.picker-item:last-child {{ border-bottom: none; }}
.picker-item:hover {{ background: var(--bg-hover); }}
.picker-item.selected {{ background: rgba(59,130,246,0.12); }}
.picker-icon {{ color: var(--text-tertiary); font-size: 14px; }}
.picker-footer {{ display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; align-items: center; }}
.picker-footer .btn-cancel {{ padding: 6px 16px; border-radius: 6px; border: 1px solid var(--border-default); background: none; color: var(--text-secondary); cursor: pointer; font-size: 12px; font-family: inherit; }}
.picker-footer .btn-select {{ padding: 6px 16px; border-radius: 6px; border: none; background: rgba(59,130,246,0.2); color: var(--accent); cursor: pointer; font-size: 12px; font-weight: 600; font-family: inherit; }}
.picker-footer .btn-select:hover {{ background: rgba(59,130,246,0.3); }}
.picker-footer .btn-select:disabled {{ opacity: 0.4; cursor: not-allowed; }}
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
  <label>Project Folder</label>
  <div style="display:flex;gap:8px;align-items:center;">
    <button type="button" class="browse-btn" id="browse-btn" data-testid="add-project-browse">Browse...</button>
    <div class="path-display" id="path-display" data-testid="add-project-path-display"></div>
  </div>
  <input type="hidden" name="path" id="add-path" data-testid="add-project-path">
  <label>Project Name</label>
  <input name="name" placeholder="My Project" required data-testid="add-project-name">
  <label>Project ID <span style="color:var(--text-tertiary)">(auto-generated from name)</span></label>
  <input name="id" placeholder="my-project" data-testid="add-project-id">
  <button type="submit" class="btn">Add Project</button>
  <div class="error" id="add-error"></div>
</form>
<div id="folder-picker" class="picker-overlay" data-testid="folder-picker">
  <div class="picker-box">
    <div class="picker-header">
      <h3>Select Project Folder</h3>
    </div>
    <div class="picker-breadcrumb" id="picker-breadcrumb"></div>
    <div class="picker-list" id="picker-list"></div>
    <div class="picker-footer">
      <button type="button" class="btn-cancel" id="picker-cancel">Cancel</button>
      <button type="button" class="btn-select" id="picker-select-current" title="Use the current directory">Select This Folder</button>
    </div>
  </div>
</div>
<script>
(function() {{
  var nameInput = document.querySelector('#add-form [name="name"]');
  var idInput = document.querySelector('#add-form [name="id"]');
  var pathHidden = document.getElementById('add-path');
  var pathDisplay = document.getElementById('path-display');
  var browseBtn = document.getElementById('browse-btn');
  var picker = document.getElementById('folder-picker');
  var pickerList = document.getElementById('picker-list');
  var pickerBreadcrumb = document.getElementById('picker-breadcrumb');
  var pickerCancel = document.getElementById('picker-cancel');
  var pickerSelectCurrent = document.getElementById('picker-select-current');

  var currentBrowsePath = '~/projects';
  var currentAbsPath = '';
  var selectedDir = null;

  // Name → ID auto-derive
  if (nameInput && idInput) {{
    nameInput.addEventListener('input', function() {{
      if (!idInput.dataset.manual) {{
        idInput.value = nameInput.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      }}
    }});
    idInput.addEventListener('input', function() {{ idInput.dataset.manual = '1'; }});
  }}

  function toSlug(name) {{
    return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  }}

  function selectPath(displayPath, absPath) {{
    pathHidden.value = absPath;
    pathDisplay.textContent = displayPath;
    // Auto-fill name and ID from directory name
    var dirName = displayPath.split('/').filter(Boolean).pop() || '';
    if (nameInput && !nameInput.value) {{
      // Title-case the directory name
      nameInput.value = dirName.replace(/[-_]/g, ' ').split(' ').map(function(w) {{ return w.charAt(0).toUpperCase() + w.slice(1); }}).join(' ');
    }}
    if (idInput && !idInput.dataset.manual && !idInput.value) {{
      idInput.value = toSlug(dirName);
    }}
    picker.classList.remove('visible');
  }}

  function loadDir(browsePath) {{
    currentBrowsePath = browsePath;
    selectedDir = null;
    var items = pickerList.querySelectorAll('.picker-item');
    for (var i = 0; i < items.length; i++) items[i].classList.remove('selected');

    fetch('/api/browse?path=' + encodeURIComponent(browsePath))
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (data.error) {{ pickerBreadcrumb.textContent = data.error; return; }}
        currentAbsPath = data.absolute;
        pickerBreadcrumb.textContent = data.path;
        while (pickerList.firstChild) pickerList.removeChild(pickerList.firstChild);

        // Parent dir entry
        if (data.path !== '~') {{
          var parentPath = data.path.split('/').slice(0, -1).join('/') || '~';
          var upItem = document.createElement('div');
          upItem.className = 'picker-item';
          var upIcon = document.createElement('span');
          upIcon.className = 'picker-icon';
          upIcon.textContent = '\u2190';
          var upLabel = document.createElement('span');
          upLabel.textContent = '..';
          upItem.appendChild(upIcon);
          upItem.appendChild(upLabel);
          upItem.addEventListener('click', function() {{ loadDir(parentPath); }});
          pickerList.appendChild(upItem);
        }}

        data.dirs.forEach(function(name) {{
          var item = document.createElement('div');
          item.className = 'picker-item';
          item.setAttribute('data-testid', 'picker-dir-' + name);
          var icon = document.createElement('span');
          icon.className = 'picker-icon';
          icon.textContent = String.fromCodePoint(0x1F4C1);
          var label = document.createElement('span');
          label.textContent = name;
          item.appendChild(icon);
          item.appendChild(label);
          var dirPath = data.path + '/' + name;
          var dirAbs = data.absolute + '/' + name;
          item.addEventListener('dblclick', function() {{ loadDir(dirPath); }});
          item.addEventListener('click', function() {{
            var prev = pickerList.querySelector('.selected');
            if (prev) prev.classList.remove('selected');
            item.classList.add('selected');
            selectedDir = {{ display: dirPath, absolute: dirAbs }};
          }});
          pickerList.appendChild(item);
        }});
      }});
  }}

  browseBtn.addEventListener('click', function() {{
    picker.classList.add('visible');
    loadDir(currentBrowsePath);
  }});

  pickerCancel.addEventListener('click', function() {{
    picker.classList.remove('visible');
  }});

  picker.addEventListener('click', function(e) {{
    if (e.target === picker) picker.classList.remove('visible');
  }});

  pickerSelectCurrent.addEventListener('click', function() {{
    if (selectedDir) {{
      selectPath(selectedDir.display, selectedDir.absolute);
    }} else {{
      selectPath(currentBrowsePath, currentAbsPath);
    }}
  }});

  // Form submit
  var form = document.getElementById('add-form');
  var errorDiv = document.getElementById('add-error');
  if (form) {{
    form.addEventListener('submit', function(e) {{
      e.preventDefault();
      errorDiv.style.display = 'none';
      if (!pathHidden.value) {{
        errorDiv.textContent = 'Please select a project folder first';
        errorDiv.style.display = 'block';
        return;
      }}
      var data = {{
        id: form.elements.id.value || toSlug(form.elements.name.value),
        name: form.elements.name.value,
        path: pathHidden.value,
        description: ''
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
            # Root: Kitchen — cross-project work surface (M2)
            if remainder == "/" or remainder == "":
                html = _render_kitchen_view(SERVER_PORT)
                self._send_html(html)
                return

            # Project picker (relocated from / in M2)
            if remainder == "/projects":
                html = _render_project_picker(SERVER_PORT)
                self._send_html(html)
                return

            # Kitchen JSON aggregation (M2)
            if remainder == "/api/kitchen":
                self._send_json(_aggregate_kitchen_state())
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

            # GET /api/browse?path=~ — list subdirectories for folder picker
            if remainder.startswith("/api/browse"):
                query = urlparse(self.path).query
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                browse_path = unquote(params.get("path", "~/projects"))
                resolved = Path(os.path.realpath(os.path.expanduser(browse_path)))
                home = Path.home().resolve()
                # Safety: must be within home dir
                try:
                    resolved.relative_to(home)
                except ValueError:
                    self._send_json({"error": "path must be within home directory"}, 400)
                    return
                if not resolved.is_dir():
                    self._send_json({"error": "path does not exist"}, 400)
                    return
                dirs = []
                try:
                    for entry in sorted(resolved.iterdir()):
                        if entry.is_dir() and not entry.name.startswith("."):
                            dirs.append(entry.name)
                except PermissionError:
                    pass
                display = str(resolved).replace(str(home), "~")
                self._send_json({"path": display, "absolute": str(resolved), "dirs": dirs})
                return

            # Legacy backward compat: --project flag redirects bare /api/ routes
            if _LEGACY_PROJECT_ID and remainder.startswith("/api/"):
                self.send_response(301)
                self.send_header("Location", f"/{_LEGACY_PROJECT_ID}{remainder}")
                self.end_headers()
                return

            self._send_json({"error": "Not found"}, 404)
            return

        # ── Project-scoped routes ────────────────────────────────────

        # Project settings page
        if remainder == "/settings":
            html = _render_project_settings(proj, SERVER_PORT)
            self._send_html(html)
            return

        # Journeys page
        if remainder == "/journeys":
            html = _render_journeys_page(proj, SERVER_PORT)
            self._send_html(html)
            return

        m = re.match(r"^/journeys/([A-Za-z0-9_-]+)$", remainder)
        if m:
            html = _render_journeys_page(proj, SERVER_PORT, open_journey_id=m.group(1))
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

        # Managed files
        if remainder == "/api/managed-files":
            self._send_json(_get_managed_files(proj))
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

        # Kitchen (M1b): activity history for a ticket. Newest first.
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/history$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                limit = int(parse_qs(urlparse(self.path).query).get("limit", ["100"])[0])
            except (ValueError, TypeError):
                limit = 100
            limit = max(1, min(limit, 500))
            with _db_lock:
                conn = get_db()
                init_db(conn)
                # Resolve canonical id (case-insensitive lookup) to match how
                # subject_id is written by emit_event.
                row = conn.execute(
                    "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
                    (ticket_id, proj["id"]),
                ).fetchone()
                tid = row["id"] if row else ticket_id
                rows = conn.execute(
                    "SELECT id, actor_type, actor_id, event_kind, payload_json, "
                    "       occurred_at, discarded_run_id "
                    "FROM activity_events "
                    "WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (proj["id"], tid, limit),
                ).fetchall()
                conn.close()
            events = []
            for r in rows:
                try:
                    payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
                except Exception:
                    payload = {}
                events.append({
                    "id": r["id"],
                    "actor_type": r["actor_type"],
                    "actor_id": r["actor_id"],
                    "event_kind": r["event_kind"],
                    "payload": payload,
                    "occurred_at": r["occurred_at"],
                    "discarded_run_id": r["discarded_run_id"],
                })
            self._send_json({"events": events})
            return

        # Workflow Bounce GET routes
        if remainder == "/api/workflow/agents":
            custom = _list_workflow_agents()
            for a in custom:
                a["source"] = "custom"
                a["editable"] = True
            discovered = _discover_project_agents(proj)
            self._send_json({"agents": custom + discovered})
            return

        if remainder == "/api/workflow/workflows":
            self._send_json({"workflows": _list_workflows()})
            return

        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/workflow/runs$", remainder)
        if m:
            runs = _list_workflow_runs(proj["id"], m.group(1))
            for r in runs:
                with _workflow_runs_lock:
                    mem = _workflow_runs.get(r["id"])
                if mem:
                    r["status"] = mem.get("status", r["status"])
                    if "current_step" in mem:
                        r["current_step"] = mem["current_step"]
            self._send_json({"runs": runs})
            return

        m = re.match(r"^/api/workflow/runs/([A-Za-z0-9_.-]+)$", remainder)
        if m:
            run = _get_workflow_run(m.group(1))
            if not run:
                self._send_json({"error": "Run not found"}, 404)
                return
            with _workflow_runs_lock:
                mem = _workflow_runs.get(m.group(1))
            if mem:
                run["status"] = mem.get("status", run["status"])
                if "current_step" in mem:
                    run["current_step"] = mem["current_step"]
            self._send_json(run)
            return

        # Scenario API: serve artifact files (must come before run status check)
        if remainder.startswith("/api/scenarios/runs/") and "/artifacts/" in remainder:
            parts = remainder[len("/api/scenarios/runs/"):].split("/artifacts/", 1)
            if len(parts) == 2:
                run_id, filename = parts
                with _scenario_runs_lock:
                    run = _scenario_runs.get(run_id)
                if run and run.get("output_dir"):
                    for root, dirs, files in os.walk(run["output_dir"]):
                        if filename in files:
                            filepath = os.path.join(root, filename)
                            if filepath.endswith(".png"):
                                self.send_response(200)
                                self.send_header("Content-Type", "image/png")
                                with open(filepath, "rb") as f:
                                    data = f.read()
                                self.send_header("Content-Length", str(len(data)))
                                self.end_headers()
                                self.wfile.write(data)
                                return
                            elif filepath.endswith(".json"):
                                with open(filepath) as f:
                                    self._send_json(json.load(f))
                                return
            self._send_json({"error": "Artifact not found"}, 404)
            return

        # Scenario API: get run status
        if remainder.startswith("/api/scenarios/runs/"):
            run_id = remainder[len("/api/scenarios/runs/"):]
            with _scenario_runs_lock:
                run = _scenario_runs.get(run_id)
            if not run:
                self._send_json({"error": "Run not found"}, 404)
                return
            # Check if process has finished
            proc = run.get("process")
            if proc and proc.poll() is not None:
                with _scenario_runs_lock:
                    run["status"] = "passed" if proc.returncode == 0 else "failed"
                    run["returncode"] = proc.returncode
            resp = {
                "run_id": run_id,
                "scenario_id": run["scenario_id"],
                "status": run["status"],
                "started_at": run.get("started_at"),
                "returncode": run.get("returncode"),
                "output_dir": run.get("output_dir", ""),
            }
            # If complete, try to read summary
            summary_path = None
            if run.get("output_dir"):
                output_dir_path = Path(run["output_dir"])
                if output_dir_path.is_dir():
                    for d in output_dir_path.iterdir():
                        sp = d / "summary.json"
                        if sp.exists():
                            summary_path = sp
                            break
            if summary_path and summary_path.exists():
                try:
                    with open(summary_path) as f:
                        resp["summary"] = json.load(f)
                except Exception:
                    pass
            self._send_json(resp)
            return

        # Scenario API: list discovered scenarios
        if remainder == "/api/scenarios":
            project_path = proj.get("path", "")
            scenarios_dir = os.path.join(project_path, "tests", "scenarios")
            try:
                manifests = discover_scenarios(scenarios_dir) if os.path.isdir(scenarios_dir) else []
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
                return
            # Attach last run status if available
            with _scenario_runs_lock:
                for manifest in manifests:
                    for rid, run in _scenario_runs.items():
                        if run["scenario_id"] == manifest["id"]:
                            manifest["last_run"] = {"run_id": rid, "status": run["status"], "started_at": run.get("started_at")}
            self._send_json({"scenarios": manifests})
            return

        # Journey API: list journeys
        if remainder == "/api/journeys":
            project_id = proj["id"]
            with _db_lock:
                conn = get_db()
                init_db(conn)
                journeys = list_journeys(conn, project_id)
                conn.close()
            self._send_json({"journeys": journeys})
            return

        # Journey API: get single journey with steps + runs
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)$", remainder)
        if m:
            journey_id = m.group(1)
            project_id = proj["id"]
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    journey = get_journey(conn, project_id, journey_id)
                    conn.close()
                self._send_json(journey)
            except ValueError as e:
                self._send_json({"error": str(e)}, 404)
            return

        # Journey API: get run details
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/runs/([A-Za-z0-9_-]+)$", remainder)
        if m:
            journey_id = m.group(1)
            run_id = m.group(2)
            with _db_lock:
                conn = get_db()
                init_db(conn)
                run = conn.execute(
                    "SELECT * FROM journey_runs WHERE id = ? AND journey_id = ? AND project_id = ?",
                    (run_id, journey_id, proj["id"]),
                ).fetchone()
                if not run:
                    conn.close()
                    self._send_json({"error": "Run not found"}, 404)
                    return
                run_dict = dict(run)
                step_results = conn.execute(
                    "SELECT * FROM journey_step_results WHERE run_id = ? ORDER BY sort_order",
                    (run_id,),
                ).fetchall()
                run_dict["step_results"] = [dict(sr) for sr in step_results]
                conn.close()
            self._send_json(run_dict)
            return

        # Journey API: serve run screenshot
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/runs/([A-Za-z0-9_-]+)/screenshots/(.+\.png)$", remainder)
        if m:
            journey_id, run_id, filename = m.group(1), m.group(2), m.group(3)
            if "/" in filename or "\\" in filename or ".." in filename:
                self._send_json({"error": "Invalid filename"}, 400)
                return
            # Look up artifact_dir from DB (run_id and dir basename may differ)
            with _db_lock:
                conn = get_db()
                init_db(conn)
                row = conn.execute("SELECT artifact_dir FROM journey_runs WHERE id = ?", (run_id,)).fetchone()
                conn.close()
            if row and row["artifact_dir"]:
                screenshot_path = os.path.join(row["artifact_dir"], filename)
            else:
                project_path = proj.get("path", "")
                screenshot_path = os.path.join(project_path, ".artifacts", "journeys", journey_id, run_id, filename)
            if os.path.isfile(screenshot_path):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                with open(screenshot_path, "rb") as f:
                    data = f.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_json({"error": "Screenshot not found"}, 404)
            return

        # Screens API: get cached scan results
        if remainder == "/api/screens":
            project_id = proj["id"]
            with _page_scan_lock:
                cached = _page_scan_cache.get(project_id)
            if cached:
                self._send_json({"screens": cached})
            else:
                self._send_json({"screens": [], "hint": "No scan yet. POST /api/screens/scan to discover pages."})
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
                        # M2-03: 'watched' is a Kitchen-aggregator filter flag.
                        for field in ("name", "path", "description", "active", "watched"):
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

        # ── Journey PUT routes ───────────────────────────────────────
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/steps/(\d+)$", remainder)
        if m:
            journey_id = m.group(1)
            step_id = int(m.group(2))
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    updated = update_step(conn, step_id, **body)
                    conn.commit()
                    conn.close()
                self._send_json(updated)
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            return

        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)$", remainder)
        if m:
            journey_id = m.group(1)
            project_id = proj["id"]
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    updated = update_journey(conn, project_id, journey_id, **body)
                    conn.commit()
                    conn.close()
                self._send_json(updated)
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            return

        # ── Workflow Bounce PUT routes ──────────────────────────────

        # Update workflow agent
        m = re.match(r"^/api/workflow/agents/([a-z0-9][a-z0-9_-]*)$", remainder)
        if m:
            agent_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            updated = _update_workflow_agent(agent_id, body)
            if updated:
                self._send_json(updated)
            else:
                self._send_json({"error": "Agent not found"}, 404)
            return

        # Update workflow
        m = re.match(r"^/api/workflow/workflows/([a-z0-9][a-z0-9_-]*)$", remainder)
        if m:
            workflow_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            # Auto-serialize steps list to JSON
            if "steps" in body and isinstance(body["steps"], list):
                body["steps"] = json.dumps(body["steps"])
            updated = _update_workflow(workflow_id, body)
            if updated:
                self._send_json(updated)
            else:
                self._send_json({"error": "Workflow not found"}, 404)
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

        # Handle adding a new acceptance criterion (from gate-check panel) —
        # routed through _add_criterion so the criteria_added M1b event fires.
        if "add_criteria" in body:
            text = body["add_criteria"]
            if isinstance(text, str) and text.strip():
                _add_criterion(proj, ticket_id, text.strip())
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
                backlog = Path(os.path.expanduser(new_project["path"])) / "PRODUCT_BACKLOG.md"
                result = dict(new_project)
                if backlog.exists():
                    count = cli.seed_project(conn, new_project)
                    result["seeded"] = count
                else:
                    cli.scaffold_project(conn, new_project)
                    result["scaffolded"] = True
                conn.close()
                self._send_json(result, 201)
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

        # Kitchen (M1a): set automation mode (manual / auto / held)
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/automation$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            mode = body.get("mode", "")
            hold_reason = body.get("hold_reason")
            try:
                with _db_lock:
                    conn = get_db(); init_db(conn)
                    _kitchen_set_mode(
                        conn, proj["id"], "ticket", ticket_id, mode,
                        ActorContext.human(), hold_reason=hold_reason,
                    )
                    conn.commit(); conn.close()
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            t = _get_ticket_json(proj["id"], ticket_id)
            self._send_json(t or {"ok": True})
            return

        # Kitchen (M1a): toggle no_test_required eligibility bypass
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/no-test-required$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            enabled = bool(body.get("enabled", False))
            note = body.get("note", "")
            try:
                with _db_lock:
                    conn = get_db(); init_db(conn)
                    _kitchen_set_ntr(
                        conn, proj["id"], ticket_id, enabled, note,
                        ActorContext.human(),
                    )
                    conn.commit(); conn.close()
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            t = _get_ticket_json(proj["id"], ticket_id)
            self._send_json(t or {"ok": True})
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

        if remainder == "/api/seek":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                body = {}
            sources = body.get("sources", None)
            project_path = os.path.expanduser(proj.get("path", ""))
            from seek import run_seek
            with _db_lock:
                conn = get_db()
                init_db(conn)
                cli.ingest_markdown(conn, proj)
                result = run_seek(conn, proj["id"], project_path, sources=sources)
                cli.sync_to_markdown(conn, proj)
                conn.close()
            cli.regenerate_dashboard(proj)
            self._send_json(result)
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

        # Screens API: scan pages for interactive elements
        if remainder == "/api/screens/scan":
            project_id = proj["id"]
            base_url = f"http://localhost:{SERVER_PORT}/{project_id}"
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    scans = scan_all_screens(base_url, browser)
                    browser.close()
                result = scans_to_json(scans)
                with _page_scan_lock:
                    _page_scan_cache[project_id] = result
                self._send_json({"screens": result})
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return

        # Journey API: create journey
        if remainder == "/api/journeys":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            title = body.get("title", "").strip()
            if not title:
                self._send_json({"error": "title is required"}, 400)
                return
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    journey = add_journey(conn, proj["id"], title,
                                          description=body.get("description", ""),
                                          persona=body.get("persona", ""))
                    conn.commit()
                    conn.close()
                self._send_json(journey, 201)
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            return

        # Journey API: add step
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/steps$", remainder)
        if m:
            journey_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    step = add_step(conn, journey_id, proj["id"],
                                    action=body.get("action", "click"),
                                    label=body.get("label", ""),
                                    actor=body.get("actor", "user"),
                                    target=body.get("target"),
                                    value=body.get("value", ""),
                                    key=body.get("key", ""),
                                    capture=body.get("capture"),
                                    assertion=body.get("assertion"))
                    conn.commit()
                    conn.close()
                _auto_export_journey(proj["id"], journey_id, proj.get("path", ""))
                self._send_json(step, 201)
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            return

        # Journey API: validate
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/validate$", remainder)
        if m:
            journey_id = m.group(1)
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    manifest = compile_to_manifest(conn, proj["id"], journey_id)
                    conn.close()
                from scenarios import validate_manifest, ScenarioValidationError
                validate_manifest(manifest)
                self._send_json({"ok": True, "manifest": manifest})
            except (ValueError, ScenarioValidationError) as e:
                self._send_json({"error": str(e)}, 422)
            return

        # Journey API: run journey
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/run$", remainder)
        if m:
            journey_id = m.group(1)
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    manifest = compile_to_manifest(conn, proj["id"], journey_id)
                    conn.close()
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            scenario_id = manifest["id"]
            run_id = f"{scenario_id}-{int(time.time())}"
            project_path = proj.get("path", "")
            scenarios_dir = os.path.join(project_path, "tests", "scenarios")
            os.makedirs(scenarios_dir, exist_ok=True)
            with open(os.path.join(scenarios_dir, f"{journey_id}.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)
                f.write("\n")
            cmd = [sys.executable, "-m", "pytest", "tests/test_scenarios.py", "-v", f"--scenario-id={scenario_id}"]
            env = {**os.environ, "TT_SCENARIO_BASE_URL": f"http://localhost:{SERVER_PORT}/{proj['id']}"}
            proc = subprocess.Popen(cmd, cwd=project_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            with _scenario_runs_lock:
                _scenario_runs[run_id] = {"scenario_id": scenario_id, "status": "running", "process": proc,
                    "output_dir": os.path.join(project_path, ".artifacts", "scenarios"),
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self._send_json({"run_id": run_id, "status": "running"})
            return

        # Journey API: infer from tickets
        if remainder == "/api/journeys/infer":
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    suggestions = infer_journeys(conn, proj["id"])
                    conn.close()
                self._send_json({"suggestions": suggestions})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        # Journey API: link ticket
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/link$", remainder)
        if m:
            journey_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            ticket_id = body.get("ticket_id", "").strip()
            if not ticket_id:
                self._send_json({"error": "ticket_id is required"}, 400)
                return
            with _db_lock:
                conn = get_db()
                init_db(conn)
                link_ticket(conn, journey_id, proj["id"], ticket_id)
                conn.commit()
                conn.close()
            self._send_json({"ok": True})
            return

        # Journey API: build from path
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/build-path$", remainder)
        if m:
            journey_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            path_entries = body.get("path", [])
            actor = body.get("actor", "user")
            if not path_entries:
                self._send_json({"error": "path is required"}, 400)
                return
            try:
                from journeys import build_steps_from_path
                step_dicts = build_steps_from_path(path_entries, actor)
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    conn.execute("DELETE FROM journey_steps WHERE journey_id = ? AND project_id = ?", (journey_id, proj["id"]))
                    for sd in step_dicts:
                        add_step(conn, journey_id, proj["id"], **sd)
                    conn.commit()
                    conn.close()
                _auto_export_journey(proj["id"], journey_id, proj.get("path", ""))
                self._send_json({"ok": True, "steps_created": len(step_dicts)})
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            return

        # Scenario API: start a run
        if remainder == "/api/scenarios/run":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            scenario_id = body.get("scenario_id")
            publish = body.get("publish", False)
            if not scenario_id:
                self._send_json({"error": "scenario_id required"}, 400)
                return

            project_path = proj.get("path", "")
            run_id = f"{scenario_id}-{int(time.time())}"

            cmd = [
                sys.executable, "-m", "pytest",
                "tests/test_scenarios.py", "-v",
                f"--scenario-id={scenario_id}",
            ]
            if publish:
                cmd.append("--publish")

            env = {**os.environ, "TT_SCENARIO_BASE_URL": f"http://localhost:{SERVER_PORT}/{proj['id']}"}

            proc = subprocess.Popen(
                cmd,
                cwd=project_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            with _scenario_runs_lock:
                _scenario_runs[run_id] = {
                    "scenario_id": scenario_id,
                    "status": "running",
                    "process": proc,
                    "output_dir": os.path.join(project_path, ".artifacts", "scenarios"),
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }

            self._send_json({"run_id": run_id, "status": "running"})
            return

        # Scenario API: generate draft manifests from a natural-language goal
        if remainder == "/api/scenarios/draft":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            goal = body.get("goal", "").strip()
            if not goal:
                self._send_json({"error": "'goal' is required"}, 400)
                return

            req = DraftRequest(
                goal=goal,
                actor_hints=body.get("actor_hints", []),
                target_surface=body.get("target_surface", ""),
                tags=body.get("tags", []),
            )

            # Build context from the project's existing scenarios + known testids
            project_path = proj.get("path", "")
            scenarios_dir = os.path.join(project_path, "tests", "scenarios")
            try:
                existing = discover_scenarios(scenarios_dir) if os.path.isdir(scenarios_dir) else []
            except Exception:
                existing = []

            ctx = DraftContext(
                available_testids=list(KNOWN_TESTIDS),
                existing_scenarios=existing,
                known_routes=[""],
            )

            try:
                result = generate_drafts(req, ctx)
            except Exception as exc:
                self._send_json({"error": f"Drafting failed: {exc}"}, 500)
                return

            # Serialize dataclasses to plain dicts
            candidates_out = []
            for c in result.candidates:
                candidates_out.append({
                    "title": c.title,
                    "summary": c.summary,
                    "manifest": c.manifest,
                    "assumptions": c.assumptions,
                    "prerequisites": c.prerequisites,
                    "confidence": c.confidence,
                })

            self._send_json({
                "intent_summary": result.intent_summary,
                "candidates": candidates_out,
                "warnings": result.warnings,
            })
            return

        # Scenario API: approve and save a draft manifest
        if remainder == "/api/scenarios/drafts/approve":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            manifest = body.get("manifest")
            filename = body.get("filename", "").strip()

            if not manifest or not isinstance(manifest, dict):
                self._send_json({"error": "'manifest' dict is required"}, 400)
                return
            if not filename:
                # Derive filename from manifest id
                manifest_id = manifest.get("id", "")
                if not manifest_id:
                    self._send_json({"error": "'filename' or manifest 'id' is required"}, 400)
                    return
                filename = f"{manifest_id}.json"

            # Ensure .json extension
            if not filename.endswith(".json"):
                filename = filename + ".json"

            # Security: reject path traversal
            if "/" in filename or "\\" in filename or ".." in filename:
                self._send_json({"error": "filename must be a plain filename with no path separators"}, 400)
                return

            # Validate the manifest
            from scenarios import validate_manifest, ScenarioValidationError
            try:
                validate_manifest(manifest, filepath=filename)
            except ScenarioValidationError as exc:
                self._send_json({"error": f"Manifest validation failed: {exc}"}, 422)
                return

            # Write to tests/scenarios/
            project_path = proj.get("path", "")
            scenarios_dir = os.path.join(project_path, "tests", "scenarios")
            os.makedirs(scenarios_dir, exist_ok=True)
            dest = os.path.join(scenarios_dir, filename)

            try:
                import json as _json
                with open(dest, "w", encoding="utf-8") as f:
                    _json.dump(manifest, f, indent=2, ensure_ascii=False)
                    f.write("\n")
            except OSError as exc:
                self._send_json({"error": f"Failed to write manifest: {exc}"}, 500)
                return

            self._send_json({"ok": True, "filename": filename, "path": dest})
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

        # ── Workflow Bounce POST routes ─────────────────────────────

        # Create workflow agent
        if remainder == "/api/workflow/agents":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            agent_id = body.get("id", "").strip()
            if not agent_id or not re.match(r"^[a-z0-9][a-z0-9_-]*$", agent_id):
                self._send_json({"error": "Invalid agent id — must match ^[a-z0-9][a-z0-9_-]*$"}, 400)
                return
            name = body.get("name", agent_id)
            command = body.get("command", "claude")
            args = body.get("args", "[]")
            if isinstance(args, list):
                args = json.dumps(args)
            system_prompt = body.get("system_prompt", "")
            agent = _create_workflow_agent(agent_id, name, command, args, system_prompt)
            if agent:
                self._send_json(agent, 201)
            else:
                self._send_json({"error": f"Agent '{agent_id}' already exists"}, 409)
            return

        # Create workflow
        if remainder == "/api/workflow/workflows":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            workflow_id = body.get("id", "").strip()
            if not workflow_id or not re.match(r"^[a-z0-9][a-z0-9_-]*$", workflow_id):
                self._send_json({"error": "Invalid workflow id — must match ^[a-z0-9][a-z0-9_-]*$"}, 400)
                return
            name = body.get("name", workflow_id)
            description = body.get("description", "")
            steps = body.get("steps", [])
            if isinstance(steps, list):
                steps = json.dumps(steps)
            wf = _create_workflow(workflow_id, name, description, steps)
            if wf:
                self._send_json(wf, 201)
            else:
                self._send_json({"error": f"Workflow '{workflow_id}' already exists"}, 409)
            return

        # Start a workflow run for a ticket
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/workflow/run$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            workflow_id = body.get("workflow_id", "").strip()
            if not workflow_id:
                self._send_json({"error": "workflow_id is required"}, 400)
                return
            workflow = _get_workflow(workflow_id)
            if not workflow:
                self._send_json({"error": f"Workflow '{workflow_id}' not found"}, 404)
                return
            # Parse steps to get total
            try:
                steps = json.loads(workflow.get("steps", "[]")) if isinstance(workflow.get("steps"), str) else workflow.get("steps", [])
            except (json.JSONDecodeError, TypeError):
                steps = []
            import uuid
            run_id = str(uuid.uuid4())[:12]
            # Create DB record
            with _db_lock:
                conn = get_db()
                init_db(conn)
                conn.execute(
                    "INSERT INTO workflow_runs (id, ticket_id, project_id, workflow_id, status, total_steps) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, ticket_id, proj["id"], workflow_id, "running", len(steps)),
                )
                conn.commit()
                conn.close()
            # Spawn background thread
            t = threading.Thread(
                target=_run_workflow_thread,
                args=(run_id, proj["id"], ticket_id, workflow, proj),
                daemon=True,
            )
            with _workflow_runs_lock:
                _workflow_runs[run_id] = {
                    "status": "running",
                    "thread": t,
                    "workflow_id": workflow_id,
                    "started_at": datetime.utcnow().isoformat(),
                    "current_step": 0,
                }
            t.start()
            self._send_json({"run_id": run_id, "status": "running"})
            return

        # Cancel a workflow run
        m = re.match(r"^/api/workflow/runs/([A-Za-z0-9_-]+)/cancel$", remainder)
        if m:
            run_id = m.group(1)
            with _workflow_runs_lock:
                mem = _workflow_runs.get(run_id)
                if mem:
                    mem["status"] = "cancelled"
            _update_workflow_run(run_id, status="cancelled",
                                completed_at=datetime.utcnow().isoformat())
            self._send_json({"ok": True, "run_id": run_id, "status": "cancelled"})
            return

        # Resume a paused workflow run
        m = re.match(r"^/api/workflow/runs/([A-Za-z0-9_-]+)/resume$", remainder)
        if m:
            run_id = m.group(1)
            with _workflow_runs_lock:
                mem = _workflow_runs.get(run_id)
                if mem:
                    mem["status"] = "running"
            _update_workflow_run(run_id, status="running")
            self._send_json({"ok": True, "run_id": run_id, "status": "running"})
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

        # ── Journey DELETE routes ────────────────────────────────────
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/steps/(\d+)$", remainder)
        if m:
            journey_id = m.group(1)
            step_id = int(m.group(2))
            with _db_lock:
                conn = get_db()
                init_db(conn)
                delete_step(conn, step_id)
                conn.commit()
                conn.close()
            self._send_json({"ok": True, "deleted": step_id})
            return

        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/link/([A-Za-z0-9_-]+)$", remainder)
        if m:
            journey_id = m.group(1)
            ticket_id = m.group(2)
            with _db_lock:
                conn = get_db()
                init_db(conn)
                unlink_ticket(conn, journey_id, proj["id"], ticket_id)
                conn.commit()
                conn.close()
            self._send_json({"ok": True})
            return

        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)$", remainder)
        if m:
            journey_id = m.group(1)
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    delete_journey(conn, proj["id"], journey_id)
                    conn.commit()
                    conn.close()
                self._send_json({"ok": True, "deleted": journey_id})
            except ValueError as e:
                self._send_json({"error": str(e)}, 404)
            return

        # ── Workflow Bounce DELETE routes ───────────────────────────

        # Delete workflow agent
        m = re.match(r"^/api/workflow/agents/([a-z0-9][a-z0-9_-]*)$", remainder)
        if m:
            agent_id = m.group(1)
            if _delete_workflow_agent(agent_id):
                self._send_json({"ok": True, "deleted": agent_id})
            else:
                self._send_json({"error": "Agent not found"}, 404)
            return

        # Delete workflow
        m = re.match(r"^/api/workflow/workflows/([a-z0-9][a-z0-9_-]*)$", remainder)
        if m:
            workflow_id = m.group(1)
            if _delete_workflow(workflow_id):
                self._send_json({"ok": True, "deleted": workflow_id})
            else:
                self._send_json({"error": "Workflow not found"}, 404)
            return

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
