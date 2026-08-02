#!/usr/bin/env python3
"""Ticket Takeaway Dashboard Server — serves the dashboard with editing API.

Usage:
    python3 serve.py [--port PORT] [--project ID] [--bind HOST]

Starts an HTTP server at http://localhost:PORT (default 8787) that:
  - GET /              → serves the generated HTML dashboard
  - GET /api/tickets   → JSON ticket data
  - PUT /api/tickets/<id>      → update ticket fields
  - POST /api/tickets/<id>/move → move ticket between sections
"""

import atexit
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlparse

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
import html as _html

import conditions as _conditions
import evidence as _kitchen_evidence
import kitchen as _kitchen
import kitchen_feed
import kitchen_view
from actions import (
    # Kitchen (M1a)
    ActorContext,
    # Typed errors (paired with DashboardHandler._send_typed_error below)
    AppError,
)
from actions import (
    accept_ticket as _actions_accept_ticket,
)
from actions import (
    add_ticket as _actions_add_ticket,
)
from actions import (
    eligibility as _kitchen_eligibility,
)
from actions import (
    emit_event as _kitchen_emit_event,
)
from actions import (
    get_activity_feed as _actions_get_activity_feed,
)
from actions import (
    get_project_branches as _actions_get_project_branches,
)
from actions import (
    # Branch / PR (main)
    get_ticket_branches as _actions_get_ticket_branches,
)
from actions import (
    list_bookmarks as _actions_list_bookmarks,
)
from actions import (
    list_recents as _actions_list_recents,
)
from actions import (
    move_ticket as _actions_move_ticket,
)
from actions import (
    scan_branches as _actions_scan_branches,
)
from actions import (
    scan_prs as _actions_scan_prs,
)
from actions import (
    set_automation_mode as _kitchen_set_mode,
)
from actions import (
    set_no_test_required as _kitchen_set_ntr,
)
from actions import (
    # Bookmarks + Recents (I-43)
    toggle_bookmark as _actions_toggle_bookmark,
)
from actions import (
    touch_recent as _actions_touch_recent,
)
from actions import (
    update_ticket as _actions_update_ticket,
)
from constants import (
    EVENT_GROUP_COLORS,
    EVENT_KIND_GROUPS,
    EVENT_KIND_ICONS,
    EVENT_KIND_LABELS,
    GATE_BANNER_BY_SECTION,
    REGISTRY_PATH,
    SECTION_SLUGS,
    WORKFLOW_AGENT_TIMEOUT,
)
from constants import VALID_READINESS_FLAGS as _CANONICAL_READINESS_FLAGS
from db import get_db, init_db
from endpoints import extract_session_id as _endpoints_extract_session_id
from journeys import (
    add_journey,
    add_step,
    compile_to_manifest,
    delete_journey,
    delete_step,
    get_journey,
    infer_journeys,
    link_ticket,
    list_journeys,
    unlink_ticket,
    update_journey,
    update_step,
)
from page_scraper import scan_all_screens, scans_to_json
from runners import _resolve_argv_for_agent
from scenario_drafting import KNOWN_TESTIDS, DraftContext, DraftRequest, generate_drafts
from scenarios import discover_scenarios
from workflows_seed import (
    seed_default_agents as _seed_default_agents,
)
from workflows_seed import (
    seed_default_endpoints as _seed_default_endpoints,
)
from workflows_seed import (
    seed_default_workflows as _seed_default_workflows,
)
from workspaces import wipe_for_retry_fresh as _kitchen_wipe_fresh


def _auto_export_journey(*args, **kwargs):
    """Stub — full implementation on journeys branch."""


# Registry cache — populated at startup, refreshed on /api/projects mutations
_PROJECTS_CACHE: dict[str, dict] = {}
_PROJECTS_CACHE_LOCK = threading.Lock()

# Global route prefixes that must never be captured as project IDs
_GLOBAL_PREFIXES = frozenset(
    {
        "api",
        "settings",
        "static",
        "health",
        "favicon.ico",
        "index.html",
        "workflows",
        "",
    }
)

# Reserved project IDs that cannot be registered
_RESERVED_IDS = frozenset(
    {"api", "settings", "static", "health", "favicon.ico", "index.html"}
)


def _refresh_projects_cache() -> None:
    """Reload registry.json into the module-level cache. Thread-safe."""
    if not REGISTRY_PATH.exists():
        return
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
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
# PWA — shared head tags + service worker registration script
# ---------------------------------------------------------------------------
# Single source for the manifest link, theme colors, and apple-touch metadata
# so every top-level page renders identical install / standalone-mode hints.

PWA_HEAD_TAGS = (
    '<meta name="theme-color" content="#0c0c0e" media="(prefers-color-scheme: dark)">'
    '<meta name="theme-color" content="#fafafa" media="(prefers-color-scheme: light)">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    '<meta name="apple-mobile-web-app-title" content="Tickets">'
    '<meta name="mobile-web-app-capable" content="yes">'
    '<link rel="manifest" href="/manifest.webmanifest">'
    '<link rel="apple-touch-icon" href="/icon-180.png">'
    "<script>"
    "if('serviceWorker' in navigator && (location.protocol==='https:'"
    "||location.hostname==='localhost'||location.hostname==='127.0.0.1')){"
    "window.addEventListener('load',function(){"
    "navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(function(e){"
    "console.warn('[pwa] sw register failed:',e);});});}"
    "</script>"
)


# ---------------------------------------------------------------------------
# Kitchen demo state — populated stub for GET /kitchen/demo so the new
# layout can be reviewed visually when the live DB has no actionable
# tickets. Safe to delete once the feature has matured.
# ---------------------------------------------------------------------------

_DEMO_KITCHEN_STATE = {
    "paused": False,
    "totals": {
        "all": 8,
        "needs_me": 2,
        "running": 1,
        "ready_to_delegate": 3,
        "paused_ticket": 1,
        "failed": 1,
    },
    "projects": [
        {
            "id": "ticket-takeaway",
            "name": "Ticket Takeaway",
            "counts": {
                "all": 5,
                "needs_me": 1,
                "running": 1,
                "ready_to_delegate": 2,
                "paused_ticket": 0,
                "failed": 1,
            },
        },
        {
            "id": "goodform",
            "name": "Goodform",
            "counts": {
                "all": 3,
                "needs_me": 1,
                "running": 0,
                "ready_to_delegate": 1,
                "paused_ticket": 1,
                "failed": 0,
            },
        },
    ],
    "items": [
        {
            "ticket_id": "B-24",
            "project_id": "ticket-takeaway",
            "project_name": "Ticket Takeaway",
            "title": "Implement CLI endpoint abstraction for project add modal",
            "section": "WIP",
            "status": "in-progress",
            "bucket": "running",
            "time_bucket": "today",
            "updated_at": "2026-05-11T13:55:00",
            "is_unread": True,
            "automation_mode": "auto",
            "agent_name": "primary",
            "latest_run_status": "running",
            "pause_reason": None,
        },
        {
            "ticket_id": "B-31",
            "project_id": "ticket-takeaway",
            "project_name": "Ticket Takeaway",
            "title": "Switch development environment to use new workspaces module",
            "section": "For Review",
            "status": "for-review",
            "bucket": "needs_me",
            "time_bucket": "today",
            "updated_at": "2026-05-11T11:00:00",
            "is_unread": True,
            "automation_mode": "auto",
            "agent_name": "reviewer",
            "latest_run_status": "needs_input",
            "pause_reason": None,
        },
        {
            "ticket_id": "B-29",
            "project_id": "ticket-takeaway",
            "project_name": "Ticket Takeaway",
            "title": "Create PWA version of ticket-takeaway",
            "section": "Backlog",
            "status": "proposed",
            "bucket": "ready_to_delegate",
            "time_bucket": "today",
            "updated_at": "2026-05-11T09:00:00",
            "is_unread": False,
            "automation_mode": "auto",
            "agent_name": None,
            "latest_run_status": None,
            "pause_reason": None,
        },
        {
            "ticket_id": "BUG-12",
            "project_id": "goodform",
            "project_name": "Goodform",
            "title": "Fix project add modal and seek button label overlap",
            "section": "Bugs",
            "status": "bug-found",
            "bucket": "needs_me",
            "time_bucket": "today",
            "updated_at": "2026-05-11T08:30:00",
            "is_unread": True,
            "automation_mode": "auto",
            "agent_name": None,
            "latest_run_status": "needs_input",
            "pause_reason": None,
        },
        {
            "ticket_id": "B-19",
            "project_id": "goodform",
            "project_name": "Goodform",
            "title": "llm-node-logical-ritchie",
            "section": "Backlog",
            "status": "proposed",
            "bucket": "ready_to_delegate",
            "time_bucket": "yesterday",
            "updated_at": "2026-05-10T14:00:00",
            "is_unread": False,
            "automation_mode": "auto",
            "agent_name": None,
            "latest_run_status": None,
            "pause_reason": None,
        },
        {
            "ticket_id": "B-15",
            "project_id": "ticket-takeaway",
            "project_name": "Ticket Takeaway",
            "title": "llm-node-enumerated-hartmanis",
            "section": "Backlog",
            "status": "proposed",
            "bucket": "ready_to_delegate",
            "time_bucket": "yesterday",
            "updated_at": "2026-05-10T10:00:00",
            "is_unread": False,
            "automation_mode": "auto",
            "agent_name": None,
            "latest_run_status": None,
            "pause_reason": None,
        },
        {
            "ticket_id": "B-08",
            "project_id": "ticket-takeaway",
            "project_name": "Ticket Takeaway",
            "title": "Ensure gc-pillars-page work is in main branch",
            "section": "WIP",
            "status": "in-progress",
            "bucket": "failed",
            "time_bucket": "this_week",
            "updated_at": "2026-05-08T18:00:00",
            "is_unread": False,
            "automation_mode": "auto",
            "agent_name": "primary",
            "latest_run_status": "failed",
            "pause_reason": None,
        },
        {
            "ticket_id": "B-03",
            "project_id": "goodform",
            "project_name": "Goodform",
            "title": "Waiting on legal review (paused demo)",
            "section": "Backlog",
            "status": "blocked",
            "bucket": "paused_ticket",
            "time_bucket": "older",
            "updated_at": "2026-04-15T10:00:00",
            "is_unread": False,
            "automation_mode": "paused",
            "agent_name": None,
            "latest_run_status": None,
            "pause_reason": "Waiting on legal review",
        },
    ],
}


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

_LEGACY_PROJECT_ID = None  # Set from --project arg for backward compat
SERVER_PORT = 8787

# Lock for DB operations (sqlite3 connections aren't thread-safe)
_db_lock = (
    threading.RLock()
)  # Reentrant — write functions call _get_ticket_json while holding lock

# Scenario run tracking
_scenario_runs: dict[
    str, dict
] = {}  # run_id -> {status, scenario_id, process, output_dir, started_at}
_scenario_runs_lock = threading.Lock()

# Workflow bounce tracking
_workflow_runs: dict[str, dict] = {}
_workflow_runs_lock = threading.Lock()

# Page scan results — POST /api/screens/scan writes, GET /api/screens serves
_page_scan_cache: dict[str, list[dict]] = {}  # project_id -> scans_to_json result
_page_scan_lock = threading.Lock()

# Managed CDP Chrome process — spawned on demand when a CDP run is dispatched
# and nothing is already listening on the debug port. Reused across runs.
_cdp_chrome_proc: subprocess.Popen | None = None
_cdp_chrome_lock = threading.Lock()
_CDP_DEFAULT_ENDPOINT = "http://localhost:9222"


def _cdp_endpoint_reachable(endpoint: str, timeout_s: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=timeout_s) as r:
            r.read()
        return True
    except Exception:
        return False


def _ensure_cdp_chrome(
    endpoint: str = _CDP_DEFAULT_ENDPOINT,
    boot_timeout_s: float = 10.0,
) -> tuple[bool, str]:
    """Make sure a CDP-debuggable Chrome is reachable at `endpoint`.

    Returns (ok, detail). If something already answers (the user's own
    Chrome or our managed instance), returns (True, "external"|"managed").
    Otherwise spawns a headless Chrome with the debug port open, waits
    until it answers, and returns (True, "spawned"). Falls through to
    (False, error_message) if we can't bring one up.
    """
    global _cdp_chrome_proc

    if _cdp_endpoint_reachable(endpoint):
        with _cdp_chrome_lock:
            if _cdp_chrome_proc and _cdp_chrome_proc.poll() is None:
                return True, "managed"
        return True, "external"

    with _cdp_chrome_lock:
        # Re-check after acquiring the lock — another request may have
        # spawned Chrome while we were waiting.
        if _cdp_endpoint_reachable(endpoint):
            return True, "managed" if _cdp_chrome_proc else "external"

        # Clean up a dead managed process before respawning.
        if _cdp_chrome_proc and _cdp_chrome_proc.poll() is not None:
            _cdp_chrome_proc = None

        chrome_bin = (
            shutil.which("google-chrome")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
            or shutil.which("chrome")
        )
        if not chrome_bin:
            return False, "No chromium/chrome binary found on PATH."

        port = endpoint.rsplit(":", 1)[-1]
        profile_dir = os.path.join(tempfile.gettempdir(), "tt-cdp-profile")
        os.makedirs(profile_dir, exist_ok=True)
        cmd = [
            chrome_bin,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--no-sandbox",
            "about:blank",
        ]
        try:
            _cdp_chrome_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            return False, f"Failed to launch Chrome: {e}"

        deadline = time.time() + boot_timeout_s
        while time.time() < deadline:
            if _cdp_endpoint_reachable(endpoint):
                return True, "spawned"
            if _cdp_chrome_proc.poll() is not None:
                _cdp_chrome_proc = None
                return False, "Chrome exited before opening debug port."
            time.sleep(0.25)

        try:
            _cdp_chrome_proc.terminate()
        except Exception:
            pass
        _cdp_chrome_proc = None
        return False, f"Chrome did not open debug port within {boot_timeout_s:.0f}s."


@atexit.register
def _shutdown_cdp_chrome() -> None:
    proc = _cdp_chrome_proc
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _finalize_journey_run(run_id: str) -> bool:
    """Bridge a finished scenario subprocess into journey_runs/step_results.

    Idempotent — safe to call repeatedly. Returns True if the run was just
    finalised on this call, False if the subprocess is still running, the
    run isn't a journey run, or it was already finalised.
    """
    with _scenario_runs_lock:
        run = _scenario_runs.get(run_id)
        if not run or "journey_id" not in run:
            return False
        proc = run.get("process")
        if proc is None or proc.poll() is None:
            return False
        if run.get("_finalized"):
            return False

    journey_id = run["journey_id"]
    project_id = run["project_id"]
    scenario_id = run["scenario_id"]
    output_dir = run.get("output_dir", "")

    # Locate the artifact directory created by this run. The runner names it
    # `{scenario_id}-{ts}` where ts is its own time.time() call (close to but
    # not always identical to ours), so we pick the newest dir starting with
    # the scenario id created at-or-after this run's start.
    artifact_dir = ""
    summary: dict = {}
    if output_dir and os.path.isdir(output_dir):
        candidates = sorted(
            (
                os.path.join(output_dir, d)
                for d in os.listdir(output_dir)
                if d.startswith(scenario_id + "-")
                and os.path.isdir(os.path.join(output_dir, d))
            ),
            key=lambda p: os.path.getmtime(p),
            reverse=True,
        )
        for cand in candidates:
            sp = os.path.join(cand, "summary.json")
            if os.path.isfile(sp):
                try:
                    with open(sp) as f:
                        loaded = json.load(f)
                    if loaded.get("scenario_id") == scenario_id:
                        artifact_dir = os.path.join(cand, "screenshots")
                        summary = loaded
                        break
                except Exception:
                    continue

    if not summary:
        # Subprocess crashed before writing summary.json. Mark failed but
        # don't lose the row — the user still wants to see the failure.
        summary = {
            "status": "failed" if proc.returncode != 0 else "passed",
            "duration_ms": 0,
            "failed_step_index": 0 if proc.returncode != 0 else None,
            "error_message": "Run produced no summary.json",
        }

    finished_at = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = get_db()
        init_db(conn)
        # Update run row
        conn.execute(
            "UPDATE journey_runs "
            "SET status = ?, finished_at = ?, duration_ms = ?, "
            "    error_message = ?, artifact_dir = ? "
            "WHERE id = ? AND project_id = ?",
            (
                summary.get("status", "failed"),
                finished_at,
                summary.get("duration_ms", 0),
                summary.get("error_message", ""),
                artifact_dir,
                run_id,
                project_id,
            ),
        )
        # Update step results based on failed_step_index
        failed_idx = summary.get("failed_step_index")
        step_results = conn.execute(
            "SELECT id, sort_order FROM journey_step_results "
            "WHERE run_id = ? ORDER BY sort_order",
            (run_id,),
        ).fetchall()
        for sr in step_results:
            i = sr["sort_order"]
            if (
                summary.get("status") == "passed"
                or failed_idx is not None
                and i < failed_idx
            ):
                status = "passed"
            elif failed_idx is not None and i == failed_idx:
                status = "failed"
            else:
                status = "skipped"
            err = summary.get("error_message", "") if status == "failed" else ""
            conn.execute(
                "UPDATE journey_step_results SET status = ?, error_message = ? "
                "WHERE id = ?",
                (status, err, sr["id"]),
            )
        conn.commit()

        # Backfill screenshot paths onto capture-step results
        if artifact_dir and os.path.isdir(artifact_dir):
            pngs = sorted(
                f
                for f in os.listdir(artifact_dir)
                if f.endswith(".png") and not f.startswith("FAILURE-")
            )
            capture_results = conn.execute(
                "SELECT jsr.id "
                "FROM journey_step_results jsr "
                "JOIN journey_steps js ON jsr.step_id = js.id "
                "WHERE jsr.run_id = ? AND js.action = 'capture' "
                "ORDER BY jsr.sort_order",
                (run_id,),
            ).fetchall()
            for i, png in enumerate(pngs):
                if i >= len(capture_results):
                    break
                url = f"/api/journeys/{journey_id}/runs/{run_id}/screenshots/{png}"
                conn.execute(
                    "UPDATE journey_step_results SET screenshot_path = ? WHERE id = ?",
                    (url, capture_results[i]["id"]),
                )
            conn.commit()
        conn.close()

    with _scenario_runs_lock:
        if run_id in _scenario_runs:
            _scenario_runs[run_id]["_finalized"] = True
            _scenario_runs[run_id]["status"] = summary.get("status", "failed")
    return True


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
    (
        "PRODUCT_BACKLOG.md",
        "Ticket backlog \u2014 auto-regenerated from DB on every write",
        False,
    ),
    (
        "PRODUCT_SPECIFICATION.md",
        "Accepted feature specs \u2014 append-only on /accept",
        False,
    ),
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
        result.append(
            {
                "path": rel_path,
                "description": description,
                "exists": os.path.exists(full),
                "gitignored": gitignored,
            }
        )
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

    from constants import (
        FEEDBACKS_DEFAULT_PORT,
        FEEDBACKS_DETECTION_CACHE_TTL,
        FEEDBACKS_REPO_URL,
    )

    settings = _get_all_settings()
    feedbacks_home = settings.get("feedbacks.home", "")
    feedbacks_enabled = settings.get("feedbacks.enabled", "").lower() in (
        "true",
        "1",
        "yes",
    )

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
        req = urllib.request.Request(
            f"http://127.0.0.1:{FEEDBACKS_DEFAULT_PORT}/config"
        )
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


def _add_attachment(
    project_id, ticket_id, attachment_type, name, path="", summary="", metadata="{}"
):
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
            from actions import ActorContext as _AC
            from actions import emit_event as _emit

            _emit(
                conn,
                project_id,
                "ticket",
                tid,
                "attachment_added",
                {
                    "attachment_id": att["id"] if att else None,
                    "kind": attachment_type,
                    "label": name,
                },
                _AC.human(),
            )
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
            from actions import ActorContext as _AC
            from actions import emit_event as _emit

            _emit(
                conn,
                project_id,
                "ticket",
                att["ticket_id"],
                "attachment_removed",
                {
                    "attachment_id": attachment_id,
                    "kind": att["attachment_type"],
                    "label": att["name"],
                },
                _AC.human(),
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
        "title",
        "priority",
        "status",
        "description",
        "parent",
        "commit_hash",
        "release_tag",
        "draft",
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
            (ticket_id, project_id),
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
            (value, datetime.now().isoformat(), tid, project_id),
        )
        # M1b: emit_event in same tx. status changes stay on the M1a status_change
        # event; everything else is field_changed.
        from actions import ActorContext as _AC
        from actions import emit_event as _emit

        if field == "status":
            _emit(
                conn,
                project_id,
                "ticket",
                tid,
                "status_change",
                {"before": before_val, "after": value},
                _AC.human(),
            )
        else:
            _emit(
                conn,
                project_id,
                "ticket",
                tid,
                "field_changed",
                {"field": field, "before": before_val, "after": value},
                _AC.human(),
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
            _actions_move_ticket(
                conn, project_id, ticket_id, section, project_path=project_path
            )
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
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        criterion = conn.execute(
            "SELECT id, checked FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC LIMIT 1 OFFSET ?",
            (tid, project_id, criterion_index),
        ).fetchone()
        if not criterion:
            conn.close()
            return False

        new_checked = 0 if criterion["checked"] else 1
        conn.execute(
            "UPDATE acceptance_criteria SET checked = ? WHERE id = ?",
            (new_checked, criterion["id"]),
        )
        conn.execute(
            "UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
            (datetime.now().isoformat(), tid, project_id),
        )
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _update_criterion_text(
    proj: dict, ticket_id: str, criterion_index: int, new_text: str
) -> bool:
    """Update the text of a criterion at a given index."""
    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
        cli.ingest_markdown(conn, proj)

        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        criterion = conn.execute(
            "SELECT id, text FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC LIMIT 1 OFFSET ?",
            (tid, project_id, criterion_index),
        ).fetchone()
        if not criterion:
            conn.close()
            return False

        before_text = criterion["text"]
        conn.execute(
            "UPDATE acceptance_criteria SET text = ? WHERE id = ?",
            (new_text, criterion["id"]),
        )
        conn.execute(
            "UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
            (datetime.now().isoformat(), tid, project_id),
        )
        # M1b: criteria_changed event with {before, after}
        if before_text != new_text:
            from actions import ActorContext as _AC
            from actions import emit_event as _emit

            _emit(
                conn,
                project_id,
                "ticket",
                tid,
                "criteria_changed",
                {
                    "criterion_id": criterion["id"],
                    "before": before_text,
                    "after": new_text,
                },
                _AC.human(),
            )
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
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        criterion = conn.execute(
            "SELECT id, text FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC LIMIT 1 OFFSET ?",
            (tid, project_id, criterion_index),
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
            (tid, project_id),
        ).fetchall()
        for i, r in enumerate(remaining):
            conn.execute(
                "UPDATE acceptance_criteria SET sort_order = ? WHERE id = ?",
                (i, r["id"]),
            )

        conn.execute(
            "UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
            (datetime.now().isoformat(), tid, project_id),
        )
        # M1b: criteria_removed event carries the removed text for restoration.
        from actions import ActorContext as _AC
        from actions import emit_event as _emit

        _emit(
            conn,
            project_id,
            "ticket",
            tid,
            "criteria_removed",
            {"criterion_id": removed_id, "text": removed_text},
            _AC.human(),
        )
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
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
            (tid, project_id),
        ).fetchone()["next_order"]

        cur = conn.execute(
            "INSERT INTO acceptance_criteria (ticket_id, project_id, text, checked, sort_order) VALUES (?,?,?,0,?)",
            (tid, project_id, text, max_order),
        )
        new_crit_id = cur.lastrowid
        conn.execute(
            "UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
            (datetime.now().isoformat(), tid, project_id),
        )
        # M1b: criteria_added event
        from actions import ActorContext as _AC
        from actions import emit_event as _emit

        _emit(
            conn,
            project_id,
            "ticket",
            tid,
            "criteria_added",
            {"criterion_id": new_crit_id, "text": text},
            _AC.human(),
        )
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
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        before = [
            r[0]
            for r in conn.execute(
                "SELECT depends_on_id FROM depends WHERE ticket_id = ? AND project_id = ? ORDER BY depends_on_id",
                (tid, project_id),
            ).fetchall()
        ]
        conn.execute(
            "DELETE FROM depends WHERE ticket_id = ? AND project_id = ?",
            (tid, project_id),
        )
        cleaned: list[str] = []
        for dep_id in depends_list:
            dep_id = dep_id.strip()
            if dep_id:
                conn.execute(
                    "INSERT OR IGNORE INTO depends (ticket_id, project_id, depends_on_id) VALUES (?,?,?)",
                    (tid, project_id, dep_id),
                )
                cleaned.append(dep_id)
        after = sorted(set(cleaned))
        conn.execute(
            "UPDATE tickets SET updated_at = ? WHERE id = ? AND project_id = ?",
            (datetime.now().isoformat(), tid, project_id),
        )
        # M1b: dependency_changed event with sorted-list before/after for clean diffs.
        if sorted(before) != after:
            from actions import ActorContext as _AC
            from actions import emit_event as _emit

            _emit(
                conn,
                project_id,
                "ticket",
                tid,
                "dependency_changed",
                {"before": before, "after": after},
                _AC.human(),
            )
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
    description = body.get("description", "")

    with _db_lock:
        conn = get_db()
        init_db(conn)
        cli.ingest_markdown(conn, proj)

        draft = bool(body.get("draft", False))
        tags_raw = body.get("tags", [])
        tags = (
            [t.strip().lower() for t in tags_raw if isinstance(t, str) and t.strip()]
            if isinstance(tags_raw, list)
            else []
        )
        # add_ticket emits its own ticket_created event with origin=human
        ticket_id = _actions_add_ticket(
            conn,
            project_id,
            title,
            section=section,
            priority=priority,
            description=description,
            draft=draft,
            tags=tags or None,
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

        # Snapshot the row so the ticket_deleted event carries enough state
        # for a future "undelete" / restore path.
        row = conn.execute(
            "SELECT * FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        snapshot = {k: row[k] for k in row.keys()}
        conn.execute(
            "DELETE FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
            (tid, project_id),
        )
        conn.execute(
            "DELETE FROM depends WHERE ticket_id = ? AND project_id = ?",
            (tid, project_id),
        )
        conn.execute(
            "DELETE FROM tickets WHERE id = ? AND project_id = ?", (tid, project_id)
        )
        # M1b: ticket_deleted event with snapshot. Activity row references a
        # subject that no longer exists in tickets — by design, the audit log
        # outlives the row.
        from actions import ActorContext as _AC
        from actions import emit_event as _emit

        _emit(
            conn,
            project_id,
            "ticket",
            tid,
            "ticket_deleted",
            {"snapshot": snapshot},
            _AC.human(),
        )
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _accept_ticket(proj: dict, ticket_id: str, force: str = "") -> tuple[bool, str]:
    """Accept a ticket — move to Done with status 'done'.

    Returns ``(ok, message)``. On refusal the message is the close gate's own
    explanation, forwarded verbatim: a dashboard click must be told exactly what
    a CLI caller would be told, or the two surfaces have effectively different
    rules. The gate itself lives in actions.accept_ticket().
    """
    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
        cli.ingest_markdown(conn, proj)

        project_path = os.path.expanduser(proj.get("path", ""))
        project_name = proj.get("name", proj.get("id", ""))
        try:
            _actions_accept_ticket(
                conn, project_id, ticket_id, project_path, project_name, force=force
            )
        except ValueError as exc:
            conn.close()
            return (False, str(exc))

        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return (True, "")


# Same registry the CLI validates against — the HTTP surface must not be able to
# accept a flag the headless path rejects, or vice versa.
VALID_READINESS_FLAGS = _CANONICAL_READINESS_FLAGS


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
        row = conn.execute(
            "SELECT * FROM workflow_agents WHERE id = ?", (agent_id,)
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def _create_workflow_agent(
    agent_id: str, name: str, command: str, args: str, system_prompt: str
) -> dict | None:
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
            row = conn.execute(
                "SELECT * FROM workflow_agents WHERE id = ?", (agent_id,)
            ).fetchone()
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
        row = conn.execute(
            "SELECT * FROM workflow_agents WHERE id = ?", (agent_id,)
        ).fetchone()
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
        results.append(
            {
                "id": f"_project_{slug}",
                "name": name,
                "command": "claude",
                "args": "[]",
                "system_prompt": "",
                "source": "project",
                "editable": False,
            }
        )
    return results


def _serialize_workflow(row: dict) -> dict:
    """Convert a raw workflow DB row into the API response shape.

    Parses trigger_json, on_success_json, and steps from stored JSON strings
    into Python objects so callers get parsed values, not raw strings.
    """
    d = dict(row)
    for field in ("trigger_json", "on_success_json"):
        raw = d.get(field)
        if raw:
            try:
                d[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[field] = None
        else:
            d[field] = None
    raw_steps = d.get("steps")
    if raw_steps:
        try:
            d["steps"] = json.loads(raw_steps)
        except (json.JSONDecodeError, TypeError):
            d["steps"] = []
    else:
        d["steps"] = []
    # Normalize integer flags
    d["system"] = int(d.get("system") or 0)
    d["enabled"] = int(d.get("enabled") if d.get("enabled") is not None else 1)
    return d


def _list_workflows(project_id: "str | None" = None) -> list[dict]:
    """Return workflows visible to a project (or all workflows if project_id is None).

    Post-migration-16 model: workflows are first-class. A workflow is visible to a
    project iff it has a row in `workflow_projects` linking the two. Per-project
    enable state lives on the join row (workflow_projects.enabled), and is folded
    into the returned dict as `enabled` for the requesting project.
    """
    with _db_lock:
        conn = get_db()
        init_db(conn)
        if project_id is None:
            rows = conn.execute("SELECT * FROM workflows ORDER BY name").fetchall()
            results = [_serialize_workflow(dict(r)) for r in rows]
        else:
            rows = conn.execute(
                """
                SELECT w.*, wp.enabled AS link_enabled
                FROM workflows w
                INNER JOIN workflow_projects wp ON w.id = wp.workflow_id
                WHERE wp.project_id = ?
                ORDER BY w.name
            """,
                (project_id,),
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["enabled"] = d.pop("link_enabled", d.get("enabled", 1))
                results.append(_serialize_workflow(d))
        conn.close()
    return results


def _get_workflow(workflow_id: str, project_id: "str | None" = None) -> dict | None:
    """Return a single workflow by ID. If project_id is provided, only returns
    the row when project_id is linked via workflow_projects.
    """
    with _db_lock:
        conn = get_db()
        init_db(conn)
        if project_id is None:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT w.*, wp.enabled AS link_enabled
                FROM workflows w
                INNER JOIN workflow_projects wp ON w.id = wp.workflow_id
                WHERE w.id = ? AND wp.project_id = ?
            """,
                (workflow_id, project_id),
            ).fetchone()
        conn.close()
    if not row:
        return None
    d = dict(row)
    if "link_enabled" in d:
        d["enabled"] = d.pop("link_enabled", d.get("enabled", 1))
    return _serialize_workflow(d)


def _create_workflow(
    workflow_id: str,
    name: str,
    description: str,
    steps: str,
    *,
    project_id: "str | None" = None,
    enabled: int = 1,
    trigger_json: "str | None" = None,
    on_success_json: "str | None" = None,
    subject_type: str = "ticket",
) -> dict | None:
    """Insert a new workflow. steps/trigger_json/on_success_json should be JSON strings.

    Post-migration-16 the dispatcher reads visibility from
    `workflow_projects(workflow_id, project_id, enabled)`. When project_id is
    provided, we also insert the matching join row so the new workflow is
    immediately picked up. Without this, user-created workflows would be
    invisible to the kitchen tick.
    """
    with _db_lock:
        conn = get_db()
        init_db(conn)
        try:
            conn.execute(
                "INSERT INTO workflows "
                "(id, name, description, steps, system, enabled, trigger_json, on_success_json, subject_type, project_id) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    workflow_id,
                    name,
                    description,
                    steps,
                    enabled,
                    trigger_json,
                    on_success_json,
                    subject_type,
                    project_id,
                ),
            )
            if project_id:
                conn.execute(
                    "INSERT OR IGNORE INTO workflow_projects (workflow_id, project_id, enabled) "
                    "VALUES (?, ?, ?)",
                    (workflow_id, project_id, enabled),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            conn.close()
            return _serialize_workflow(dict(row)) if row else None
        except sqlite3.IntegrityError:
            conn.close()
            return None


def _update_workflow(workflow_id: str, updates: dict) -> dict | None:
    """Update an existing workflow. Returns updated record or None.

    For system workflows, only `enabled` may be changed (403 is returned by the
    caller; this function accepts any allowed field to keep the logic clean).

    When `enabled` is in the update set, mirror it into ALL `workflow_projects`
    links for this workflow. The dispatcher reads visibility from that join
    table — without this mirror, the column-level toggle on /workflows would
    update display state but never reach the dispatcher.
    """
    allowed = {
        "name",
        "description",
        "steps",
        "enabled",
        "trigger_json",
        "on_success_json",
    }
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
        if "enabled" in fields:
            conn.execute(
                "UPDATE workflow_projects SET enabled = ? WHERE workflow_id = ?",
                (int(bool(fields["enabled"])), workflow_id),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        conn.close()
    return _serialize_workflow(dict(row)) if row else None


def _delete_workflow(workflow_id: str) -> bool:
    """Delete a workflow. Returns True if deleted."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        cur = conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        conn.commit()
        conn.close()
    return cur.rowcount > 0


def _preview_workflow_matches(workflow: dict, sample_limit: int = 5) -> dict:
    """Evaluate this workflow's trigger against every linked project's tickets.

    Returns ``{count, by_project, samples, manual}``. ``manual`` is True when
    the workflow has no trigger (no auto-fire predicate), in which case count
    is 0 and samples is empty. The caller renders this inline on /workflows so
    users can see operationalised behaviour at a glance.
    """
    import json as _json

    from conditions import build_subject_context, evaluate_trigger

    trigger_raw = workflow.get("trigger_json")
    if trigger_raw in (None, "", "null"):
        return {
            "count": 0,
            "manual": True,
            "by_project": {},
            "samples": [],
        }
    try:
        trigger = (
            _json.loads(trigger_raw) if isinstance(trigger_raw, str) else trigger_raw
        )
    except (_json.JSONDecodeError, TypeError):
        return {
            "count": 0,
            "manual": False,
            "by_project": {},
            "samples": [],
            "error": "trigger_json invalid",
        }

    workflow_id = workflow.get("id")
    samples: list[dict] = []
    by_project: dict[str, int] = {}
    total = 0

    with _db_lock:
        conn = get_db()
        init_db(conn)
        link_rows = conn.execute(
            "SELECT project_id, enabled FROM workflow_projects WHERE workflow_id = ? AND enabled = 1",
            (workflow_id,),
        ).fetchall()

        # Collect non-draft / non-archived tickets for each linked project. We
        # evaluate the trigger predicate against every candidate; keeping the
        # iteration narrow per project avoids context-cost blow-up on big DBs.
        for link in link_rows:
            pid = link["project_id"]
            ticket_rows = conn.execute(
                "SELECT id, title FROM tickets "
                "WHERE project_id = ? AND archived = 0 AND draft = 0",
                (pid,),
            ).fetchall()
            project_count = 0
            for t in ticket_rows:
                try:
                    ctx = build_subject_context(conn, pid, t["id"])
                    passed, _reasons = evaluate_trigger(trigger, ctx)
                except Exception:
                    passed = False
                if passed:
                    project_count += 1
                    total += 1
                    if len(samples) < sample_limit:
                        samples.append(
                            {
                                "id": t["id"],
                                "title": t["title"],
                                "project_id": pid,
                            }
                        )
            if project_count:
                by_project[pid] = project_count
        conn.close()

    return {
        "count": total,
        "manual": False,
        "by_project": by_project,
        "samples": samples,
    }


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


def _get_workflow_run(run_id: str, project_id: "str | None" = None) -> dict | None:
    """Return a single workflow run with parsed conversation. When project_id is
    provided, the row is only returned if it belongs to that project — used by
    request handlers to prevent cross-project leakage of run state.
    """
    with _db_lock:
        conn = get_db()
        init_db(conn)
        if project_id is None:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE id = ? AND project_id = ?",
                (run_id, project_id),
            ).fetchone()
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
    allowed = {"status", "current_step", "conversation", "completed_at", "session_ids"}
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


def _recover_stuck_workflow_runs() -> None:
    """Mark any runs stuck in 'running' as 'failed' — their threads died with the previous server."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        stuck = conn.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE status = 'running'"
        ).fetchone()[0]
        if stuck:
            conn.execute(
                "UPDATE workflow_runs SET status = 'failed', completed_at = ? WHERE status = 'running'",
                (datetime.utcnow().isoformat(),),
            )
            conn.commit()
            print(f"  Recovered {stuck} stuck workflow run(s) → failed")
        conn.close()


# ---------------------------------------------------------------------------
# Phase 3A helpers — workflow inspect, kitchen settings, run observability
# ---------------------------------------------------------------------------


def _inspect_workflows_for_ticket(project_id: str, ticket_id: str) -> dict:
    """Evaluate all enabled workflows against a ticket and return per-condition results.

    This is the eligibility inspector (endpoint C). Unlike evaluate_trigger()
    which returns a single bool, we evaluate each condition independently so
    the UI can show pass/fail per condition.
    """
    with _db_lock:
        conn = get_db()
        init_db(conn)
        try:
            ctx = _conditions.build_subject_context(conn, project_id, ticket_id)
        except ValueError:
            conn.close()
            raise

        # Build a compact summary from the assembled context for the response
        ticket = ctx["ticket"]
        subj = ctx.get("automation_subject")
        automation_mode = subj["automation_mode"] if subj else "manual"

        # Count criteria
        criteria_count = conn.execute(
            "SELECT COUNT(*) FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()[0]

        # deps_clear (reuse condition evaluator)
        from actions import _deps_clear  # type: ignore[import]

        deps_ok, _ = _deps_clear(conn, project_id, ticket_id)

        # tests_covered
        from actions import _tests_covered  # type: ignore[import]

        tc_ok, _ = _tests_covered(conn, ctx["ticket_row"])

        # has_description
        has_description = bool((ticket.get("description") or "").strip())

        active_run = ctx["active_run"]

        subject_context_summary = {
            "section": ticket.get("section", ""),
            "automation_mode": automation_mode,
            "has_description": has_description,
            "criteria_count": criteria_count,
            "deps_clear": deps_ok,
            "tests_covered": tc_ok,
            "active_run": active_run,
        }

        # Fetch enabled workflows owned by this project. We intentionally exclude
        # rows with project_id IS NULL — those are unmigrated legacy entries that
        # would otherwise leak across projects.
        wf_rows = conn.execute(
            "SELECT * FROM workflows WHERE (enabled = 1 OR enabled = '1') "
            "AND project_id = ?",
            (project_id,),
        ).fetchall()
        conn.close()

    def _flatten_conditions(node):
        """Walk an arbitrarily-nested all_of/any_of trigger and yield each leaf
        condition. Mirrors the recursion in conditions.evaluate_trigger so the
        inspector reports every condition the dispatcher will actually check.
        """
        if not isinstance(node, dict):
            return
        if "all_of" in node and isinstance(node["all_of"], list):
            for child in node["all_of"]:
                yield from _flatten_conditions(child)
        elif "any_of" in node and isinstance(node["any_of"], list):
            for child in node["any_of"]:
                yield from _flatten_conditions(child)
        elif "kind" in node:
            yield node

    workflow_results = []
    for wf_row in wf_rows:
        wf = _serialize_workflow(dict(wf_row))
        trigger = wf.get("trigger_json")  # already parsed by _serialize_workflow

        condition_results = []
        all_passed = True

        if trigger is None:
            pass
        else:
            for cond in _flatten_conditions(trigger):
                kind = cond.get("kind", "")
                params = {k: v for k, v in cond.items() if k != "kind"}
                entry = _conditions.CONDITION_CATALOG.get(kind)
                if entry:
                    try:
                        passed, reason = entry["evaluator"](ctx, params)
                    except Exception as exc:
                        passed, reason = False, f"evaluator error: {exc}"
                else:
                    passed, reason = False, f"unknown condition kind {kind!r}"
                condition_results.append(
                    {
                        "kind": kind,
                        "params": params,
                        "passed": passed,
                        "reason": reason,
                    }
                )
                if not passed:
                    all_passed = False
            # Trust the official evaluator for the top-level pass/fail — flatten
            # is for display only. all_of vs any_of semantics matter at the root.
            try:
                all_passed, _ = _conditions.evaluate_trigger(trigger, ctx)
            except Exception:
                pass

        workflow_results.append(
            {
                "workflow_id": wf["id"],
                "name": wf.get("name", ""),
                "system": wf.get("system", 0),
                "enabled": wf.get("enabled", 1),
                "passed": all_passed,
                "conditions": condition_results,
            }
        )

    return {
        "ticket_id": ticket_id,
        "subject_context_summary": subject_context_summary,
        "workflows": workflow_results,
    }


_KITCHEN_BOOL_KEYS = frozenset({"kitchen.use_db_workflows", "kitchen.paused"})
_KITCHEN_INT_KEYS = frozenset({"kitchen.poll_seconds"})


def _get_kitchen_settings() -> dict:
    """Return all kitchen.* settings as a parsed dict (bool/int coercion applied)."""
    all_settings = _get_all_settings()
    kitchen = {}
    for key, raw in all_settings.items():
        if not key.startswith("kitchen."):
            continue
        short_key = key[len("kitchen.") :]
        if key in _KITCHEN_BOOL_KEYS:
            kitchen[short_key] = str(raw).lower() == "true"
        elif key in _KITCHEN_INT_KEYS:
            try:
                kitchen[short_key] = int(raw)
            except (TypeError, ValueError):
                kitchen[short_key] = raw
        else:
            kitchen[short_key] = raw
    # Inject live paused state from kitchen module
    kitchen["paused"] = _kitchen.is_paused()
    return kitchen


def _set_kitchen_settings(updates: dict) -> tuple[dict | None, str | None]:
    """Validate and persist kitchen settings updates.

    Returns (settings_dict, error_message). error_message is None on success.
    """
    KNOWN_BOOL = {"use_db_workflows", "paused"}
    KNOWN_INT = {"poll_seconds"}

    validated = {}
    for key, value in updates.items():
        if key in KNOWN_BOOL:
            if not isinstance(value, bool):
                return None, f"{key!r} must be a boolean"
            validated[f"kitchen.{key}"] = str(value).lower()
        elif key in KNOWN_INT:
            if not isinstance(value, int):
                return None, f"{key!r} must be an integer"
            validated[f"kitchen.{key}"] = str(value)
        else:
            # Allow unknown keys with raw string storage
            validated[f"kitchen.{key}"] = str(value)

    _set_settings(validated)
    return _get_kitchen_settings(), None


def _serialize_kitchen_run(row, title_map: dict | None = None) -> dict:
    """Convert a runs table row into the API response shape."""
    d = dict(row)
    # Parse metadata_json
    raw_meta = d.pop("metadata_json", None)
    if raw_meta:
        try:
            d["workflow_meta"] = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            d["workflow_meta"] = None
    else:
        d["workflow_meta"] = None
    # Enrich with ticket title when subject_type='ticket'
    if title_map and d.get("subject_type") == "ticket":
        d["ticket_title"] = title_map.get(d.get("subject_id", ""))
    else:
        d["ticket_title"] = None
    return d


def _get_ticket_titles(conn, project_id: str) -> dict:
    """Return a {ticket_id: title} map for all tickets in the project."""
    rows = conn.execute(
        "SELECT id, title FROM tickets WHERE project_id = ?", (project_id,)
    ).fetchall()
    return {r["id"]: r["title"] for r in rows}


_RUNS_SELECT = (
    "SELECT id, project_id, subject_type, subject_id, runner_kind, status, "
    "       started_at, claimed_at, heartbeat_at, "
    "       finished_at, duration_ms, error_message, attempt, metadata_json "
    "FROM runs"
)

_ACTIVE_STATUSES = ("queued", "preparing", "running", "needs_input")
_FINISHED_STATUSES = ("succeeded", "failed", "cancelled", "stalled")


def _get_active_kitchen_runs(project_id: str) -> list[dict]:
    """Return all active kitchen runs for a project."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
        rows = conn.execute(
            f"{_RUNS_SELECT} WHERE project_id = ? AND status IN ({placeholders}) ORDER BY id DESC",
            (project_id, *_ACTIVE_STATUSES),
        ).fetchall()
        title_map = _get_ticket_titles(conn, project_id)
        conn.close()
    return [_serialize_kitchen_run(r, title_map) for r in rows]


def _get_recent_kitchen_runs(project_id: str, limit: int = 25) -> list[dict]:
    """Return most recent finished kitchen runs for a project."""
    limit = max(1, min(limit, 100))
    with _db_lock:
        conn = get_db()
        init_db(conn)
        placeholders = ",".join("?" for _ in _FINISHED_STATUSES)
        rows = conn.execute(
            f"{_RUNS_SELECT} WHERE project_id = ? AND status IN ({placeholders}) "
            "ORDER BY finished_at DESC, id DESC LIMIT ?",
            (project_id, *_FINISHED_STATUSES, limit),
        ).fetchall()
        title_map = _get_ticket_titles(conn, project_id)
        conn.close()
    return [_serialize_kitchen_run(r, title_map) for r in rows]


def _get_kitchen_run_detail(project_id: str, run_id: int) -> dict | None:
    """Return full run detail with activity_events."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        r = conn.execute(
            "SELECT id, project_id, subject_type, subject_id, runner_kind, status, "
            "       started_at, claimed_at, heartbeat_at, "
            "       finished_at, duration_ms, error_message, attempt, metadata_json, "
            "       workspace_path, evidence_dir, needs_input_prompt, triggered_by "
            "FROM runs WHERE id = ? AND project_id = ?",
            (run_id, project_id),
        ).fetchone()
        if not r:
            conn.close()
            return None
        run_dict = _serialize_kitchen_run(r)

        # Fetch activity events for the run's subject in the run's time window
        started_at = run_dict.get("started_at") or "1970-01-01"
        finished_at = run_dict.get("finished_at") or datetime.utcnow().isoformat()
        evt_rows = conn.execute(
            "SELECT id, actor_type, actor_id, event_kind, payload_json, occurred_at, discarded_run_id "
            "FROM activity_events "
            "WHERE project_id = ? AND subject_type = ? AND subject_id = ? "
            "  AND occurred_at >= ? AND occurred_at <= ? "
            "ORDER BY id ASC",
            (
                project_id,
                run_dict.get("subject_type", ""),
                run_dict.get("subject_id", ""),
                started_at,
                finished_at,
            ),
        ).fetchall()
        conn.close()

    events = []
    for ev in evt_rows:
        try:
            payload = json.loads(ev["payload_json"]) if ev["payload_json"] else {}
        except Exception:
            payload = {}
        events.append(
            {
                "id": ev["id"],
                "actor_type": ev["actor_type"],
                "actor_id": ev["actor_id"],
                "event_kind": ev["event_kind"],
                "payload": payload,
                "occurred_at": ev["occurred_at"],
                "discarded_run_id": ev["discarded_run_id"],
            }
        )
    return {"run": run_dict, "events": events}


def _get_run_evidence(project_id: str, run_id: int) -> list[dict] | None:
    """Return evidence file listing for a run. Returns None if run not found."""
    with _db_lock:
        conn = get_db()
        init_db(conn)
        r = conn.execute(
            "SELECT evidence_dir FROM runs WHERE id = ? AND project_id = ?",
            (run_id, project_id),
        ).fetchone()
        conn.close()
    if r is None:
        return None  # run not found
    evidence_dir = r["evidence_dir"]
    if not evidence_dir or not os.path.isdir(evidence_dir):
        return []
    files = []
    try:
        for name in os.listdir(evidence_dir):
            fpath = os.path.join(evidence_dir, name)
            if os.path.isfile(fpath):
                st = os.stat(fpath)
                files.append(
                    {
                        "name": name,
                        "path": fpath,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
    except OSError:
        pass
    return files


# ---------------------------------------------------------------------------
# End Phase 3A helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


def _run_workflow_thread(
    run_id: str, project_id: str, ticket_id: str, workflow: dict, proj: dict
) -> None:
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
            _update_workflow_run(
                run_id,
                status="failed",
                conversation=[{"role": "system", "content": "Ticket not found"}],
                completed_at=datetime.utcnow().isoformat(),
            )
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
            criteria_text = "\n".join(
                f"- {'[x]' if c.get('checked') else '[ ]'} {c.get('text', '')}"
                for c in criteria
            )
            context_parts.append(f"Acceptance Criteria:\n{criteria_text}")
        ticket_context = "\n\n".join(context_parts)

        steps = []
        try:
            steps = (
                json.loads(workflow.get("steps", "[]"))
                if isinstance(workflow.get("steps"), str)
                else workflow.get("steps", [])
            )
        except (json.JSONDecodeError, TypeError):
            steps = []

        if not steps:
            _update_workflow_run(
                run_id,
                status="failed",
                conversation=[{"role": "system", "content": "Workflow has no steps"}],
                completed_at=datetime.utcnow().isoformat(),
            )
            with _workflow_runs_lock:
                if run_id in _workflow_runs:
                    _workflow_runs[run_id]["status"] = "failed"
            return

        conversation = []

        for step_idx, step in enumerate(steps):
            # Check for cancellation
            with _workflow_runs_lock:
                run_state = _workflow_runs.get(run_id, {})
                if run_state.get("status") == "cancelled":
                    _update_workflow_run(
                        run_id,
                        status="cancelled",
                        conversation=conversation,
                        completed_at=datetime.utcnow().isoformat(),
                    )
                    return

            # Check for pause (from disagreement)
            while True:
                with _workflow_runs_lock:
                    run_state = _workflow_runs.get(run_id, {})
                    st = run_state.get("status", "running")
                if st == "cancelled":
                    _update_workflow_run(
                        run_id,
                        status="cancelled",
                        conversation=conversation,
                        completed_at=datetime.utcnow().isoformat(),
                    )
                    return
                if st != "paused":
                    break
                time.sleep(1)

            agent_id = step.get("agent_id", "")
            prompt_modifier = step.get("prompt_modifier", step.get("prompt", ""))

            # Load agent config
            agent = _get_workflow_agent(agent_id)
            if not agent:
                conversation.append(
                    {
                        "role": "system",
                        "step": step_idx,
                        "content": f"Agent '{agent_id}' not found — skipping step",
                    }
                )
                _update_workflow_run(
                    run_id, current_step=step_idx, conversation=conversation
                )
                continue

            # Build prompt
            prompt_parts = []
            if agent.get("system_prompt"):
                prompt_parts.append(agent["system_prompt"])

            # Include last 3 conversation turns for context
            recent = conversation[-3:] if len(conversation) > 3 else conversation
            if recent:
                history = "\n\n".join(
                    f"[{t.get('agent', 'system')}]: {t.get('content', '')}"
                    for t in recent
                )
                prompt_parts.append(f"Previous conversation:\n{history}")

            prompt_parts.append(f"Ticket context:\n{ticket_context}")

            if prompt_modifier:
                prompt_parts.append(prompt_modifier)

            prompt = "\n\n---\n\n".join(prompt_parts)

            # Read the freshest session_ids from DB (another handler may have updated it)
            fresh_run = _get_workflow_run(run_id)
            session_ids: dict = {}
            if fresh_run:
                try:
                    session_ids = json.loads(fresh_run.get("session_ids") or "{}")
                except (json.JSONDecodeError, TypeError):
                    session_ids = {}
            prior_sid = session_ids.get(agent_id)

            # For compat (no endpoint_id) non-persist agents: inject --no-session-persistence
            # This block remains for the compat (NULL endpoint_id) path, which stays for one
            # release per spec. Remove when compat columns are dropped.
            # into the agent args so the compat Endpoint picks it up via build_invocation.
            # Real endpoint agents carry this in their endpoint config instead.
            _agent_for_invocation = agent
            if not agent.get("endpoint_id") and not agent.get("persist_session"):
                _cmd_name = agent.get("command", "claude")
                if _cmd_name.lower() == "claude":
                    try:
                        _raw_args = agent.get("args", "[]")
                        _parsed_args = (
                            json.loads(_raw_args)
                            if isinstance(_raw_args, str)
                            else list(_raw_args or [])
                        )
                        if isinstance(_parsed_args, str):
                            _parsed_args = [_parsed_args]
                    except (json.JSONDecodeError, TypeError):
                        _parsed_args = []
                    if "--no-session-persistence" not in _parsed_args:
                        _agent_for_invocation = dict(agent)
                        _agent_for_invocation["args"] = json.dumps(
                            _parsed_args + ["--no-session-persistence"]
                        )

            # Determine session_id to pass (only for persist_session agents with a prior session)
            _session_id_for_agent = (
                prior_sid if agent.get("persist_session") and prior_sid else None
            )

            # Resolve the argv via endpoint abstraction (falls back to compat columns when endpoint_id IS NULL)
            with _db_lock:
                _ep_conn = get_db()
            try:
                step_ep, cmd = _resolve_argv_for_agent(
                    _ep_conn,
                    _agent_for_invocation,
                    prompt,
                    session_id=_session_id_for_agent,
                )
            finally:
                _ep_conn.close()

            # Progress entry so the UI shows which agent is running immediately
            agent_label = agent.get("name", agent_id)
            conversation.append(
                {
                    "role": "system",
                    "step": step_idx,
                    "content": f"Running agent '{agent_label}'…",
                }
            )
            _update_workflow_run(
                run_id,
                current_step=step_idx,
                conversation=conversation,
                status="running",
            )
            with _workflow_runs_lock:
                if run_id in _workflow_runs:
                    _workflow_runs[run_id]["status"] = "running"
                    _workflow_runs[run_id]["current_step"] = step_idx

            # Streaming subprocess — read stdout line by line, flush to DB periodically
            deadline = time.time() + WORKFLOW_AGENT_TIMEOUT
            started_at = time.time()
            timed_out = False
            exit_code = None
            all_output_lines: list = []
            turn: dict = {
                "role": "streaming",
                "agent": agent_label,
                "agent_id": agent_id,
                "step": step_idx,
                "streaming": True,
                "content": "",
                "ts": datetime.utcnow().isoformat(),
            }
            conversation.append(turn)
            last_flush = time.time()
            line_count = 0

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=os.path.expanduser(proj.get("path", ".")),
                )
                while True:
                    if time.time() > deadline:
                        proc.kill()
                        timed_out = True
                        break
                    line = proc.stdout.readline()
                    if line == "" and proc.poll() is not None:
                        break
                    if line:
                        all_output_lines.append(line)
                        turn["content"] += line
                        line_count += 1
                        now = time.time()
                        if (now - last_flush >= 1.0) or (line_count % 16 == 0):
                            _update_workflow_run(
                                run_id, current_step=step_idx, conversation=conversation
                            )
                            last_flush = now
                exit_code = proc.poll()
            except Exception as _popen_err:
                turn["streaming"] = False
                turn["content"] = f"Subprocess error: {_popen_err}"
                conversation.append(
                    {
                        "role": "system",
                        "step": step_idx,
                        "content": f"step failed: {_popen_err}",
                        "ts": datetime.utcnow().isoformat(),
                    }
                )
                _update_workflow_run(
                    run_id, current_step=step_idx, conversation=conversation
                )
                continue

            turn["streaming"] = False
            turn["exit_code"] = exit_code

            # Remove the "Running agent..." placeholder (added before streaming turn)
            conversation = [
                t
                for t in conversation
                if not (
                    t.get("role") == "system"
                    and t.get("step") == step_idx
                    and "Running agent" in t.get("content", "")
                )
            ]

            if timed_out:
                turn["content"] += "\n[timeout]"
                conversation.append(
                    {
                        "role": "system",
                        "step": step_idx,
                        "content": f"Agent '{agent_label}' timed out after {WORKFLOW_AGENT_TIMEOUT}s",
                        "ts": datetime.utcnow().isoformat(),
                    }
                )
                _update_workflow_run(
                    run_id, current_step=step_idx, conversation=conversation
                )
                continue

            # Capture session id for persist_session agents
            if agent.get("persist_session"):
                full_output = "".join(all_output_lines)
                new_sid = _endpoints_extract_session_id(
                    step_ep, full_output, "", started_at
                )
                if new_sid and new_sid != prior_sid:
                    session_ids[agent_id] = new_sid
                    _update_workflow_run(run_id, session_ids=json.dumps(session_ids))

            # Check for non-zero exit code
            if exit_code != 0:
                err = turn["content"].strip()[:2000] or f"Exit code {exit_code}"
                conversation.append(
                    {
                        "role": "system",
                        "step": step_idx,
                        "content": f"Agent '{agent_label}' failed:\n{err}",
                        "ts": datetime.utcnow().isoformat(),
                    }
                )
                _update_workflow_run(
                    run_id, current_step=step_idx, conversation=conversation
                )
                continue

            # Parse response from accumulated output — same pattern as gate-check
            response_text = turn["content"].strip()
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

            conversation.append(
                {
                    "role": "agent",
                    "agent": agent_label,
                    "agent_id": agent_id,
                    "step": step_idx,
                    "content": response_content,
                    "ts": datetime.utcnow().isoformat(),
                }
            )

            _update_workflow_run(
                run_id, current_step=step_idx, conversation=conversation
            )

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

                    with _db_lock:
                        _agree_ep_conn = get_db()
                    try:
                        _agree_ep, agree_cmd = _resolve_argv_for_agent(
                            _agree_ep_conn,
                            primary_agent,
                            agree_prompt,
                        )
                    finally:
                        _agree_ep_conn.close()
                    try:
                        agree_result = subprocess.run(
                            agree_cmd,
                            capture_output=True,
                            text=True,
                            stdin=subprocess.DEVNULL,
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
                                    agree_json = {
                                        "agreed": True,
                                        "summary": agree_json["result"],
                                    }
                        except json.JSONDecodeError:
                            agree_json = {"agreed": True, "summary": agree_text}

                        conversation.append(
                            {
                                "role": "arbiter",
                                "agent": primary_agent.get("name", primary_agent_id),
                                "step": step_idx,
                                "agreed": agree_json.get("agreed", True),
                                "summary": agree_json.get("summary", ""),
                                "contention": agree_json.get("contention"),
                                "content": agree_json.get("summary", ""),
                            }
                        )

                        if not agree_json.get("agreed", True):
                            _update_workflow_run(
                                run_id,
                                status="paused",
                                current_step=step_idx,
                                conversation=conversation,
                            )
                            with _workflow_runs_lock:
                                if run_id in _workflow_runs:
                                    _workflow_runs[run_id]["status"] = "paused"
                    except (subprocess.TimeoutExpired, Exception) as e:
                        conversation.append(
                            {
                                "role": "system",
                                "step": step_idx,
                                "content": f"Agreement check error: {e}",
                            }
                        )
                        _update_workflow_run(
                            run_id, current_step=step_idx, conversation=conversation
                        )

        # Completed — create attachment
        summary_parts = []
        for turn in conversation:
            if turn.get("role") == "agent":
                summary_parts.append(
                    f"**{turn.get('agent', 'Agent')}**: {turn.get('content', '')[:200]}"
                )
        summary_text = (
            "\n\n".join(summary_parts) if summary_parts else "Workflow completed"
        )

        _add_attachment(
            project_id,
            ticket_id,
            attachment_type="workflow_bounce",
            name=f"workflow-{run_id[:8]}",
            path="",
            summary=summary_text[:1000],
            metadata=json.dumps(
                {"run_id": run_id, "workflow_id": workflow.get("id", "")}
            ),
        )

        _update_workflow_run(
            run_id,
            status="completed",
            conversation=conversation,
            current_step=len(steps) - 1,
            completed_at=datetime.utcnow().isoformat(),
        )
        with _workflow_runs_lock:
            if run_id in _workflow_runs:
                _workflow_runs[run_id]["status"] = "completed"

    except Exception as e:
        _update_workflow_run(
            run_id,
            status="failed",
            conversation=[{"role": "system", "content": f"Workflow error: {e}"}],
            completed_at=datetime.utcnow().isoformat(),
        )
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
        re.match(r"^#{1,4}\s", lines[0]) or re.match(r"^\*\*.*\*\*:?\s*$", lines[0])
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
        analysis["add_criteria"] = [
            _clean_criteria_item(c) for c in analysis["add_criteria"] if c and c.strip()
        ]
    if "categories" in analysis and isinstance(analysis["categories"], dict):
        for cat in analysis["categories"].values():
            if isinstance(cat, dict):
                for key in ("suggestion", "current_summary", "content"):
                    if key in cat and isinstance(cat[key], str):
                        cat[key] = _clean_ai_text(cat[key])
                if "add_criteria" in cat and isinstance(cat["add_criteria"], list):
                    cat["add_criteria"] = [
                        _clean_criteria_item(c)
                        for c in cat["add_criteria"]
                        if c and c.strip()
                    ]
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

TICKET: {ticket["id"]} — {ticket["title"]}
MOVE: {ticket["section"]} → {target_section}
Priority: {ticket["priority"]} | Status: {ticket["status"]}

CURRENT STATE:

[D] DESCRIPTION:
{ticket["description"] or "(empty)"}

[C] ACCEPTANCE CRITERIA ({checked}/{total} complete):
{criteria_text}

[L] LEARNINGS: {"SET" if "reviewed" in flags else "NOT SET"}

DEPENDENCIES: {deps_text}

TASK: Analyze readiness for moving to {target_section}. For each category (D, C, L), assess completeness and suggest specific improvements if needed.

Respond with ONLY valid JSON (no markdown fences, no explanation) matching this exact schema:
{{
  "verdict": "ready" or "needs-work" or "blocked",
  "summary": "one-line explanation",
  "categories": {{
    "D": {{ "status": "ok" or "needs-work", "current_summary": "brief state", "suggestion": "improvement or null" }},
    "C": {{ "status": "ok" or "needs-work", "current_summary": "brief", "suggestion": "improvement or null", "add_criteria": ["new criterion 1"] }},
    "L": {{ "status": "ok" or "needs-work", "current_summary": "brief", "suggestion": "improvement or null" }}
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
            capture_output=True,
            text=True,
            timeout=90,
            cwd=os.path.expanduser(proj.get("path", ".")),
        )
        # --output-format json wraps the response in {"type":"result","result":"..."}
        outer = json.loads(result.stdout)
        text = (
            outer.get("result", result.stdout)
            if isinstance(outer, dict)
            else result.stdout
        )
        # The agent's text response should be raw JSON
        analysis = json.loads(text) if isinstance(text, str) else text
    except _sp.TimeoutExpired:
        return {
            "error": "Gate check timed out",
            "verdict": "needs-work",
            "summary": "Analysis timed out — review manually.",
        }
    except (json.JSONDecodeError, KeyError):
        return {
            "error": "Failed to parse agent response",
            "verdict": "needs-work",
            "summary": "Could not parse analysis — review manually.",
        }

    # Attach metadata
    _clean_analysis(analysis)
    analysis["ticket_id"] = ticket_id
    analysis["target_section"] = target_section
    return analysis


# --------------- Per-category assessment ---------------

_CAT_LABELS = {"D": "Description", "C": "Acceptance Criteria", "L": "Learnings"}


def _build_category_prompt(ticket: dict, category: str, action: str) -> str:
    """Build a focused prompt for a single DCL category assessment."""
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
    elif category in ("L", "R"):
        # Accept either letter — old DCTRS clients may still send "R" for the
        # learnings/review pane while the new vocab is "L".
        current = (
            flags.get("reviewed") if isinstance(flags, dict) else ""
        ) or "(empty)"
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

TICKET: {ticket["id"]} — {ticket["title"]}
Priority: {ticket["priority"]} | Status: {ticket["status"]}

DESCRIPTION:
{ticket.get("description") or "(empty)"}

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


def _run_category_assess(
    proj: dict, ticket_id: str, category: str, action: str
) -> dict:
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
            capture_output=True,
            text=True,
            timeout=45,
            cwd=os.path.expanduser(proj.get("path", ".")),
        )
        outer = json.loads(result.stdout)
        text = (
            outer.get("result", result.stdout)
            if isinstance(outer, dict)
            else result.stdout
        )
        analysis = json.loads(text) if isinstance(text, str) else text
    except _sp.TimeoutExpired:
        return {
            "error": "Assessment timed out",
            "status": "needs-work",
            "current_summary": "Timed out",
            "suggestion": "Try again.",
        }
    except (json.JSONDecodeError, KeyError):
        return {
            "error": "Failed to parse response",
            "status": "needs-work",
            "current_summary": "Parse error",
            "suggestion": "Try again.",
        }

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
        "reviewed": "Learnings",
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

TICKET: {ticket["id"]} — {ticket["title"]}
Priority: {ticket["priority"]} | Status: {ticket["status"]}

DESCRIPTION:
{ticket.get("description") or "(empty)"}

ACCEPTANCE CRITERIA:
{criteria_text}

CURRENT {field_label.upper()} CONTENT:
{content or "(empty)"}

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
                    hunks.append(
                        {"type": "add", "original": "", "suggested": s, "index": idx}
                    )
                elif s is None:
                    hunks.append(
                        {"type": "remove", "original": o, "suggested": "", "index": idx}
                    )
                else:
                    hunks.append(
                        {"type": "modify", "original": o, "suggested": s, "index": idx}
                    )
                idx += 1
        elif tag == "delete":
            for line in orig_lines[i1:i2]:
                hunks.append(
                    {"type": "remove", "original": line, "suggested": "", "index": idx}
                )
                idx += 1
        elif tag == "insert":
            for line in sugg_lines[j1:j2]:
                hunks.append(
                    {"type": "add", "original": "", "suggested": line, "index": idx}
                )
                idx += 1

    return hunks


def _run_enrich(
    proj: dict, ticket_id: str, field: str, content: str, action: str
) -> dict:
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
            capture_output=True,
            text=True,
            timeout=90,
            cwd=os.path.expanduser(proj.get("path", ".")),
        )
        outer = json.loads(result.stdout)
        text = (
            outer.get("result", result.stdout)
            if isinstance(outer, dict)
            else result.stdout
        )
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


def _truncate_evidence(text: str, limit: int = 4000) -> str:
    """Trim evidence blocks so learning prompts stay bounded."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


def _collect_learning_evidence(proj: dict, ticket: dict) -> str:
    """Collect local, source-linked evidence for learning extraction."""
    project_path = Path(os.path.expanduser(proj.get("path", ".") or "."))
    blocks: list[str] = []

    feature_dir = project_path / "docs" / "features" / ticket["id"]
    for name in ("PLAN.md", "NOTES.md", "BUGS.md", "TESTS.md", "REVIEW.md"):
        path = feature_dir / name
        if not path.is_file():
            continue
        try:
            blocks.append(
                f"### docs/features/{ticket['id']}/{name}\n{_truncate_evidence(path.read_text(encoding='utf-8', errors='replace'), 3000)}"
            )
        except OSError:
            continue

    git_commands = [
        ("git status --short", ["git", "status", "--short"]),
        ("git diff --stat", ["git", "diff", "--stat"]),
        ("git diff --name-only", ["git", "diff", "--name-only"]),
    ]
    for label, cmd in git_commands:
        try:
            result = subprocess.run(
                cmd,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        output = (result.stdout or "").strip()
        if output:
            blocks.append(f"### {label}\n{_truncate_evidence(output, 2000)}")

    return "\n\n".join(blocks) if blocks else "(no extra local evidence found)"


def _build_learnings_prompt(ticket: dict, current_content: str, evidence: str) -> str:
    """Build a prompt that extracts candidate learning items for a ticket."""
    criteria_lines = []
    for c in ticket.get("acceptance_criteria", []):
        mark = "[x]" if c["checked"] else "[ ]"
        criteria_lines.append(f"- {mark} {c['text']}")
    criteria_text = "\n".join(criteria_lines) if criteria_lines else "(none)"

    flags = ticket.get("readiness_flags", {})
    existing_learnings = current_content or (
        flags.get("reviewed", "") if isinstance(flags, dict) else ""
    )

    return f"""You are extracting candidate learnings from a Ticket Takeaway ticket.

The human will review each item on the ticket. Suggest only useful, source-grounded learnings.
Do not include generic progress updates, restatements of acceptance criteria, or vague advice.
Prefer compact items that could help this ticket, this project, or the user's future work.

TICKET: {ticket["id"]} — {ticket["title"]}
Section: {ticket["section"]} | Status: {ticket["status"]} | Priority: {ticket["priority"]}

DESCRIPTION:
{ticket.get("description") or "(empty)"}

ACCEPTANCE CRITERIA:
{criteria_text}

CURRENT LEARNINGS:
{existing_learnings or "(empty)"}

LOCAL EVIDENCE:
{_truncate_evidence(evidence, 10000)}

Return ONLY valid JSON with this schema:
{{
  "summary": "brief note about what evidence produced these candidates",
  "items": [
    {{
      "text": "one actionable learning item",
      "scope": "ticket" | "project" | "global" | "skill",
      "type": "decision" | "procedure" | "bug" | "test" | "ux" | "architecture" | "preference" | "constraint",
      "source": "ticket" | "diff" | "notes" | "tests" | "review" | "feedback",
      "confidence": "low" | "medium" | "high"
    }}
  ]
}}

Rules:
- Return at most 8 items.
- Keep each text under 220 characters.
- Use "ticket" scope for current-ticket-only discoveries.
- Use "project" scope for repo conventions, architecture, recurring bugs, or file-specific gotchas.
- Use "global" scope only for the human's reusable practice across projects.
- Use "skill" scope only when the learning describes a repeatable workflow that should become a command/skill.
- Do not duplicate CURRENT LEARNINGS.
- If there is nothing worth saving, return an empty items array."""


def _clean_learning_text(text: str) -> str:
    """Normalize a learning item text without stripping meaningful content."""
    if not isinstance(text, str):
        return ""
    text = text.strip()
    text = re.sub(r"^-\s*\[[ xX]?\]\s*", "", text)
    text = re.sub(r"^[-*]\s+", "", text)
    text = re.sub(r"^\[[^\]]+\]\s*", "", text)
    text = re.sub(r"^\[[^\]]+\]\s*", "", text)
    text = re.sub(r"^\d+\.\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_learning_items(data: dict, existing_content: str = "") -> list[dict]:
    """Validate and normalize generated learning candidate items."""
    allowed_scopes = {"ticket", "project", "global", "skill"}
    allowed_types = {
        "decision",
        "procedure",
        "bug",
        "test",
        "ux",
        "architecture",
        "preference",
        "constraint",
    }
    allowed_sources = {"ticket", "diff", "notes", "tests", "review", "feedback"}
    allowed_confidence = {"low", "medium", "high"}

    existing_norm = {
        _clean_learning_text(line).lower()
        for line in (existing_content or "").splitlines()
        if _clean_learning_text(line)
    }

    raw_items = data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(raw_items, list):
        return []

    items: list[dict] = []
    seen: set[str] = set()
    for raw in raw_items:
        if isinstance(raw, str):
            raw = {"text": raw}
        if not isinstance(raw, dict):
            continue
        text = _clean_learning_text(raw.get("text", ""))
        key = text.lower()
        if not text or key in seen or key in existing_norm:
            continue
        seen.add(key)
        scope = raw.get("scope", "ticket")
        typ = raw.get("type", "decision")
        source = raw.get("source", "ticket")
        confidence = raw.get("confidence", "medium")
        items.append(
            {
                "text": text,
                "scope": scope if scope in allowed_scopes else "ticket",
                "type": typ if typ in allowed_types else "decision",
                "source": source if source in allowed_sources else "ticket",
                "confidence": confidence
                if confidence in allowed_confidence
                else "medium",
            }
        )
        if len(items) >= 8:
            break
    return items


def _run_learning_generation(
    proj: dict, ticket_id: str, current_content: str = ""
) -> dict:
    """Run Claude CLI to generate candidate learning items for a ticket."""
    project_id = proj["id"]
    ticket = _get_ticket_json(project_id, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    existing = current_content or ticket.get("readiness_flags", {}).get("reviewed", "")
    evidence = _collect_learning_evidence(proj, ticket)
    prompt = _build_learnings_prompt(ticket, existing, evidence)

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=os.path.expanduser(proj.get("path", ".")),
        )
        outer = json.loads(result.stdout)
        text = (
            outer.get("result", result.stdout)
            if isinstance(outer, dict)
            else result.stdout
        )
        data = json.loads(text) if isinstance(text, str) else text
    except subprocess.TimeoutExpired:
        return {"error": "Learning generation timed out — try again."}
    except OSError as e:
        return {"error": f"Learning generation could not start: {e}"}
    except (json.JSONDecodeError, KeyError) as e:
        return {"error": f"Failed to parse learning response: {e}"}

    summary = _clean_ai_text(data.get("summary", "")) if isinstance(data, dict) else ""
    items = _normalize_learning_items(data, existing)
    return {
        "ticket_id": ticket_id,
        "summary": summary,
        "items": items,
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
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        existing = conn.execute(
            "SELECT flag, content FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
            (tid, project_id, flag),
        ).fetchone()

        if existing:
            before = {"present": True, "content": existing["content"] or ""}
            conn.execute(
                "DELETE FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
                (tid, project_id, flag),
            )
            after = {"present": False, "content": ""}
        else:
            before = {"present": False, "content": ""}
            conn.execute(
                "INSERT INTO readiness_flags (ticket_id, project_id, flag, set_by) VALUES (?, ?, ?, 'dashboard')",
                (tid, project_id, flag),
            )
            after = {"present": True, "content": ""}

        # M1b: readiness_changed event with before/after presence + content
        from actions import ActorContext as _AC
        from actions import emit_event as _emit

        _emit(
            conn,
            project_id,
            "ticket",
            tid,
            "readiness_changed",
            {"flag": flag, "before": before, "after": after},
            _AC.human(),
        )
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _update_readiness_content(
    proj: dict, ticket_id: str, flag: str, content: str
) -> bool:
    """Update readiness flag content. Non-empty content upserts (auto-fills dot), empty deletes (auto-empties)."""
    project_id = proj["id"]
    if flag not in VALID_READINESS_FLAGS:
        return False

    with _db_lock:
        conn = get_db()
        init_db(conn)

        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return False

        tid = row["id"]
        content = content.strip()

        # Capture before
        existing = conn.execute(
            "SELECT content FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
            (tid, project_id, flag),
        ).fetchone()
        before = {
            "present": existing is not None,
            "content": (existing["content"] if existing else "") or "",
        }

        if content:
            conn.execute(
                """
                INSERT INTO readiness_flags (ticket_id, project_id, flag, content, set_by)
                VALUES (?, ?, ?, ?, 'dashboard')
                ON CONFLICT (ticket_id, project_id, flag)
                DO UPDATE SET content = excluded.content
            """,
                (tid, project_id, flag, content),
            )
            after = {"present": True, "content": content}
        else:
            conn.execute(
                "DELETE FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
                (tid, project_id, flag),
            )
            after = {"present": False, "content": ""}

        # M1b: readiness_changed event (no-op writes are also skipped from emit).
        if before != after:
            from actions import ActorContext as _AC
            from actions import emit_event as _emit

            _emit(
                conn,
                project_id,
                "ticket",
                tid,
                "readiness_changed",
                {"flag": flag, "before": before, "after": after},
                _AC.human(),
            )
        conn.commit()
        cli.sync_to_markdown(conn, proj)
        cli.regenerate_dashboard(proj)
        conn.close()
    return True


def _spec_tab_payload(proj: dict, ticket_id: str) -> dict | None:
    """Build the Spec tab JSON payload for a ticket. None when ticket missing."""
    import openspec_adapter as osa
    from actions import project_path_for, spec_status

    project_id = proj["id"]
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
        info = spec_status(conn, project_id, tid)
        conn.close()

    unrecorded = [
        {"name": name, "suggested_content": f"B:{name}"}
        for name in info.get("unrecorded") or []
    ]

    change_payload = None
    link = info.get("link") or {}
    change_name = (link.get("change") or "").strip()
    if change_name and change_name != "none":
        project_path = project_path_for(project_id) or os.path.expanduser(
            proj.get("path", "") or ""
        )
        if project_path:
            resolved = osa.resolve_change_dir(project_path, change_name)
            if resolved is not None:
                change_path, archived = resolved
                change_payload = {
                    "name": change_name,
                    "archived": archived,
                    "docs": osa.change_docs(change_path),
                }

    return {
        "status": info["status"],
        "link": info.get("link"),
        "set_by": info.get("set_by") or "",
        "detail": info.get("detail") or "",
        "unrecorded": unrecorded,
        "change": change_payload,
    }


def _spec_doc_read(
    proj: dict, ticket_id: str, rel_path: str
) -> tuple[dict | None, int]:
    """Read a change doc. Returns (payload, http_status)."""
    import openspec_adapter as osa
    from actions import project_path_for, spec_status

    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return ({"error": "Ticket not found"}, 404)
        tid = row["id"]
        info = spec_status(conn, project_id, tid)
        conn.close()

    link = info.get("link") or {}
    change_name = (link.get("change") or "").strip()
    if not change_name or change_name == "none":
        return ({"error": "No linked OpenSpec change"}, 400)

    project_path = project_path_for(project_id) or os.path.expanduser(
        proj.get("path", "") or ""
    )
    if not project_path:
        return ({"error": "Unknown project path"}, 400)

    try:
        resolved = osa.resolve_change_dir(project_path, change_name)
        if resolved is None:
            return ({"error": f"change not found: {change_name}"}, 400)
        _path, archived = resolved
        content = osa.read_change_doc(project_path, change_name, rel_path)
    except FileNotFoundError as exc:
        return ({"error": str(exc)}, 400)
    except ValueError as exc:
        return ({"error": str(exc)}, 400)

    return (
        {"path": rel_path, "content": content, "readonly": archived},
        200,
    )


def _spec_doc_write(
    proj: dict, ticket_id: str, rel_path: str, content: str
) -> tuple[dict, int]:
    """Write a change doc and emit spec_doc_edited. Returns (payload, http_status)."""
    import openspec_adapter as osa
    from actions import ActorContext, emit_event, project_path_for, spec_status

    project_id = proj["id"]
    with _db_lock:
        conn = get_db()
        init_db(conn)
        row = conn.execute(
            "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (ticket_id, project_id),
        ).fetchone()
        if not row:
            conn.close()
            return ({"error": "Ticket not found"}, 404)
        tid = row["id"]
        info = spec_status(conn, project_id, tid)

        link = info.get("link") or {}
        change_name = (link.get("change") or "").strip()
        if not change_name or change_name == "none":
            conn.close()
            return ({"error": "No linked OpenSpec change"}, 400)

        project_path = project_path_for(project_id) or os.path.expanduser(
            proj.get("path", "") or ""
        )
        if not project_path:
            conn.close()
            return ({"error": "Unknown project path"}, 400)

        try:
            osa.write_change_doc(project_path, change_name, rel_path, content)
        except osa.ArchivedChangeError:
            conn.close()
            return ({"error": "archived change is read-only"}, 409)
        except FileNotFoundError as exc:
            conn.close()
            return ({"error": str(exc)}, 400)
        except ValueError as exc:
            conn.close()
            return ({"error": str(exc)}, 400)

        emit_event(
            conn,
            project_id,
            "ticket",
            tid,
            "spec_doc_edited",
            {"change": change_name, "path": rel_path},
            ActorContext.human(),
        )
        conn.commit()
        conn.close()
    return ({"ok": True}, 200)


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
        (ticket_id, project_id),
    ).fetchone()
    if not row:
        conn.close()
        return None

    criteria = conn.execute(
        "SELECT text, checked FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
        (row["id"], project_id),
    ).fetchall()
    deps = conn.execute(
        "SELECT depends_on_id FROM depends WHERE ticket_id = ? AND project_id = ?",
        (row["id"], project_id),
    ).fetchall()
    try:
        flags = conn.execute(
            "SELECT flag, content FROM readiness_flags WHERE ticket_id = ? AND project_id = ?",
            (row["id"], project_id),
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
    pause_reason = None
    no_test_required = False
    no_test_required_note = ""
    latest_run_status = None
    try:
        am_row = conn.execute(
            "SELECT automation_mode, pause_reason FROM automation_subjects "
            "WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ?",
            (project_id, row["id"]),
        ).fetchone()
        if am_row:
            automation_mode = am_row["automation_mode"]
            pause_reason = am_row["pause_reason"]
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

    # Tags
    try:
        tag_rows = conn.execute(
            "SELECT tag FROM ticket_tags WHERE ticket_id = ? AND project_id = ? ORDER BY tag",
            (row["id"], project_id),
        ).fetchall()
        tags = [t["tag"] for t in tag_rows]
    except Exception:
        tags = []

    # Branches
    try:
        branch_rows = conn.execute(
            "SELECT branch_name, remote, pr_number, pr_status, pr_url, ahead, behind, auto_linked "
            "FROM ticket_branches WHERE ticket_id = ? AND project_id = ? ORDER BY created_at",
            (row["id"], project_id),
        ).fetchall()
        branches = [
            {
                "name": b["branch_name"],
                "remote": b["remote"],
                "pr_number": b["pr_number"],
                "pr_status": b["pr_status"],
                "pr_url": b["pr_url"],
                "ahead": b["ahead"],
                "behind": b["behind"],
                "auto_linked": bool(b["auto_linked"]),
            }
            for b in branch_rows
        ]
    except Exception:
        branches = []

    conn.close()

    # Build criteria text for clipboard prompts
    criteria_list = [
        {"text": c["text"], "checked": bool(c["checked"])} for c in criteria
    ]
    criteria_text = "\n".join(
        f"- [{'x' if c['checked'] else ' '}] {c['text']}" for c in criteria_list
    )

    return {
        "id": row["id"],
        "title": row["title"],
        "priority": row["priority"],
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
        "pause_reason": pause_reason,
        "no_test_required": no_test_required,
        "no_test_required_note": no_test_required_note,
        "latest_run_status": latest_run_status,
        "automation_eligible": eligible,
        "automation_eligibility_reasons": eligibility_reasons,
        "tags": tags,
        "branches": branches,
        "is_container": bool(row["is_container"])
        if "is_container" in row.keys()
        else False,
        "summary_oneliner": row["summary_oneliner"]
        if "summary_oneliner" in row.keys()
        else "",
    }


# ---------------------------------------------------------------------------
# Project picker renderer
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")


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
    api_base = f"/{pid}/api"  # origin-relative — works through Tailscale Serve, port-forwards, etc.

    rail_css = gen.build_nav_rail_css()
    rail_html = gen.build_nav_rail_html()
    rail_js = gen.build_nav_rail_js()
    drawer_css = gen.build_settings_drawer_css()
    drawer_html = gen.build_settings_drawer_html(gen._svg_icon("x", 14))
    drawer_js = gen.build_settings_drawer_js()

    with _PROJECTS_CACHE_LOCK:
        projects_meta_json = json.dumps(
            [
                {"id": p["id"], "name": p.get("name", p["id"])}
                for p in _PROJECTS_CACHE.values()
            ]
        )

    return f'''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>{name} — Journeys</title>
{PWA_HEAD_TAGS}
<meta name="current-project" content="{pid}">
<meta name="edit-api" content="{api_base}">
<meta name="projects-list" content='{_safe_attr(projects_meta_json)}'>
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
.header {{ display: flex; align-items: center; gap: 16px; padding: 8px 20px; border-bottom: 1px solid var(--border-subtle); background: var(--bg-surface); }}
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
/* Journey list — matches the Workflows + Kitchen list pattern: a single
   bordered container with thin-bordered rows, no card gaps. */
.journey-list {{ background: var(--bg-surface); border: 1px solid var(--border-subtle); border-radius: 8px; overflow: hidden; }}
.journey-card {{
  display: grid; grid-template-columns: 16px minmax(220px, 1.4fr) minmax(160px, 1fr) auto auto;
  gap: 12px; padding: 10px 14px; align-items: center; cursor: pointer;
  border-bottom: 1px solid var(--border-subtle); font-size: 13px;
  transition: background 0.15s;
}}
.journey-card:last-child {{ border-bottom: 0; }}
.journey-card:hover {{ background: rgba(255,255,255,0.03); }}
[data-theme="light"] .journey-card:hover {{ background: var(--bg-hover); }}
.journey-card .top-row {{ display: contents; /* flatten into the parent grid */ }}
.journey-card .title {{ font-weight: 600; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.journey-card .j-id {{ font-family: "SF Mono", Monaco, monospace; font-size: 11px; color: var(--text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.journey-card .meta {{ display: flex; align-items: center; gap: 10px; color: var(--text-tertiary); font-size: 11px; white-space: nowrap; }}
.journey-card .persona {{ color: var(--text-secondary); font-size: 11px; font-style: italic; }}
.journey-card .meta-sep {{ width: 3px; height: 3px; border-radius: 50%; background: var(--border-default); flex-shrink: 0; }}
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
.run-history-header {{ display: flex; align-items: baseline; gap: 12px; margin-bottom: 8px; }}
.run-history-header h3 {{ margin-bottom: 0; }}
.run-row {{ display: flex; align-items: center; gap: 10px; padding: 6px 8px; border-radius: 4px; font-size: 11px; cursor: pointer; color: var(--text-secondary); }}
.run-row:hover {{ background: var(--bg-hover); }}
.run-row .run-id {{ font-family: "SF Mono", Monaco, monospace; color: var(--text-tertiary); }}
.run-row.run-row-hidden {{ display: none; }}
.run-results-top {{ margin-top: 16px; padding-top: 12px; border-top: 1px dashed var(--border-default); }}
.run-history-top {{ margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border-default); }}
.btn-link {{ background: none; border: none; padding: 0; font-size: 11px; color: var(--accent); cursor: pointer; text-decoration: none; }}
.btn-link:hover {{ text-decoration: underline; }}
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
<style>{rail_css}</style>
<style>{drawer_css}</style>
</head>
<body>
{rail_html}
{drawer_html}
<div class="header">
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
      <div style="flex:1;">
        <input class="title-input" id="detail-title" data-testid="detail-title" placeholder="Journey title..." style="width:100%;">
        <div id="detail-journey-id" style="font-size:10px;font-family:'SF Mono',Monaco,monospace;color:var(--text-tertiary);margin-top:1px;"></div>
      </div>
      <span id="detail-badge" class="badge badge-draft" data-testid="detail-badge">draft</span>
      <div style="margin-left:auto;display:flex;gap:6px;align-items:center;">
        <button class="btn btn-ghost btn-sm" onclick="validateJourney()" data-testid="validate-btn">Validate</button>
        <select id="backend-select" data-testid="backend-select" title="Browser backend — Playwright launches its own browser, CDP connects to Chrome on port 9222"
                style="font-size:11px;padding:4px 6px;border:1px solid var(--border-default);background:var(--bg-elevated);color:var(--text-primary);border-radius:4px;cursor:pointer;">
          <option value="playwright">PW</option>
          <option value="cdp">CDP</option>
        </select>
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
    <div id="run-results" class="run-results run-results-top" style="display:none;" data-testid="run-results">
      <div class="run-summary">
        <span id="run-status-label" class="run-status" data-testid="run-status"></span>
        <span id="run-meta" class="run-meta" data-testid="run-meta"></span>
      </div>
      <div id="step-timeline" class="step-timeline" data-testid="step-timeline"></div>
      <div id="step-result-detail" class="step-result-detail" style="display:none;" data-testid="step-result-detail"></div>
    </div>
    <div id="run-history" class="run-history run-history-top" style="display:none;" data-testid="run-history">
      <div class="run-history-header">
        <h3>Run History</h3>
        <button id="run-history-toggle" type="button" class="btn-link" data-testid="run-history-toggle" style="display:none;"></button>
      </div>
      <div id="run-history-list" data-testid="run-history-list"></div>
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
  </div>
</div>
<script>
(function() {{
  var API = '{api_base}';
  var API_PREFIX = API.replace(/\\/api$/, '');
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
        // Single-row grid: dot · title · id · meta · status badge
        var row = document.createElement('div');
        row.className = 'journey-card';
        row.setAttribute('data-testid', 'journey-card-' + j.id);
        row.onclick = function() {{ openJourney(j.id); }};

        var dot = document.createElement('span');
        dot.className = 'status-dot ' + (j.last_run_status === 'passed' ? 'passed' : j.last_run_status === 'failed' ? 'failed' : 'pending');
        row.appendChild(dot);

        var titleSpan = document.createElement('span');
        titleSpan.className = 'title';
        titleSpan.textContent = j.title;
        row.appendChild(titleSpan);

        var idSpan = document.createElement('span');
        idSpan.className = 'j-id';
        idSpan.textContent = j.id;
        row.appendChild(idSpan);

        var meta = document.createElement('span');
        meta.className = 'meta';
        var stepCount = document.createElement('span');
        stepCount.textContent = (j.step_count || 0) + ' steps';
        meta.appendChild(stepCount);
        if (j.persona) {{
          var sep = document.createElement('span'); sep.className = 'meta-sep'; meta.appendChild(sep);
          var persona = document.createElement('span'); persona.className = 'persona'; persona.textContent = j.persona; meta.appendChild(persona);
        }}
        if (j.last_run_at) {{
          var sep2 = document.createElement('span'); sep2.className = 'meta-sep'; meta.appendChild(sep2);
          var runAt = document.createElement('span'); runAt.textContent = 'Last run: ' + timeAgo(j.last_run_at); meta.appendChild(runAt);
        }}
        row.appendChild(meta);

        var badge = document.createElement('span');
        badge.className = 'badge badge-' + j.status;
        badge.textContent = j.status;
        row.appendChild(badge);

        list.appendChild(row);
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
      document.getElementById('detail-journey-id').textContent = j.id;
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
      var editBtn = document.createElement('button'); editBtn.className = 'btn btn-ghost btn-sm btn-icon'; editBtn.textContent = '\u270e'; editBtn.title = 'Edit';
      (function(sid) {{ editBtn.onclick = function() {{ toggleExpand(sid); }}; }})(step.id);
      var delBtn = document.createElement('button'); delBtn.className = 'btn btn-danger btn-sm btn-icon'; delBtn.textContent = '\u00d7'; delBtn.title = 'Remove';
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
    var backendSel = document.getElementById('backend-select');
    var backend = backendSel ? backendSel.value : 'playwright';
    toast('Starting run (' + backend + ')...');
    apiPost('/journeys/' + currentJourney.id + '/run', {{backend: backend}}).then(function(r) {{
      if (r.data.error) {{ toast(r.data.error, 'error'); return; }}
      var runId = r.data.run_id;
      toast('Run started: ' + runId);
      var jid = currentJourney.id;
      // Poll the run until it leaves 'running', then refresh the journey
      // (which loads runs[] into the timeline + screenshots panel).
      var attempts = 0;
      var maxAttempts = 60;  // ~3 minutes at 3s interval
      function poll() {{
        attempts++;
        apiGet('/journeys/' + jid + '/runs/' + runId).then(function(d) {{
          var status = (d.run && d.run.status) || d.status;
          if (status && status !== 'running') {{
            toast('Run ' + status, status === 'passed' ? 'success' : 'error');
            openJourney(jid);
          }} else if (attempts < maxAttempts) {{
            setTimeout(poll, 3000);
          }} else {{
            toast('Run still running — refresh manually', 'error');
          }}
        }}).catch(function() {{
          if (attempts < maxAttempts) setTimeout(poll, 3000);
        }});
      }}
      setTimeout(poll, 2000);
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
      var HIST_PREVIEW = 2;
      runs.forEach(function(run, idx) {{
        var row = document.createElement('div');
        row.className = 'run-row' + (idx >= HIST_PREVIEW ? ' run-row-hidden' : '');
        var d = document.createElement('span'); d.className = 'status-dot ' + run.status; row.appendChild(d);
        var rid = document.createElement('span'); rid.className = 'run-id'; rid.textContent = run.id; row.appendChild(rid);
        var t = document.createElement('span'); t.textContent = timeAgo(run.started_at); row.appendChild(t);
        var dur = document.createElement('span'); dur.textContent = run.duration_ms ? run.duration_ms + 'ms' : ''; row.appendChild(dur);
        row.onclick = function() {{ loadRunDetail(journey.id, run.id); }};
        histList.appendChild(row);
      }});
      var toggleBtn = document.getElementById('run-history-toggle');
      if (toggleBtn) {{
        if (runs.length > HIST_PREVIEW) {{
          toggleBtn.style.display = '';
          var expanded = false;
          var hidden = runs.length - HIST_PREVIEW;
          toggleBtn.textContent = 'Show ' + hidden + ' more';
          toggleBtn.onclick = function() {{
            expanded = !expanded;
            histList.querySelectorAll('.run-row').forEach(function(r, i) {{
              if (i >= HIST_PREVIEW) r.classList.toggle('run-row-hidden', !expanded);
            }});
            toggleBtn.textContent = expanded ? 'Show fewer' : ('Show ' + hidden + ' more');
          }};
        }} else {{
          toggleBtn.style.display = 'none';
          toggleBtn.onclick = null;
        }}
      }}
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
      unlinkBtn.textContent = '\u00d7';
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
<script>{rail_js}</script>
<script>{drawer_js}</script>
</body>
</html>'''


# ---------------------------------------------------------------------------
# Lane B — Full-page ticket view
# ---------------------------------------------------------------------------

_EVENT_KIND_SUMMARY_MAP = {
    "run_started": lambda p: (
        f"Run #{p.get('run_id', '')} started (kind: {p.get('runner_kind', 'agent')})"
    ),
    "run_succeeded": lambda p: (
        f"Run #{p.get('run_id', '')} succeeded — {(p.get('summary', '') or '')[:80]}"
    ),
    "run_failed": lambda p: (
        f"Run #{p.get('run_id', '')} failed — {(p.get('error_class', '') or p.get('error_message', '') or '')[:80]}"
    ),
    "run_cancelled": lambda p: f"Run #{p.get('run_id', '')} cancelled",
    "section_change": lambda p: f"{p.get('before', '?')} → {p.get('after', '?')}",
    "status_change": lambda p: f"{p.get('before', '?')} → {p.get('after', '?')}",
    "criteria_check": lambda p: (
        f"criterion {'checked' if p.get('after') else 'unchecked'}"
    ),
    "criteria_added": lambda p: f"+ {p.get('text', 'criterion')}",
    "hook_started": lambda p: f"hook '{p.get('hook', '')}' started",
    "hook_succeeded": lambda p: f"hook '{p.get('hook', '')}' succeeded",
    "hook_failed": lambda p: f"hook '{p.get('hook', '')}' failed",
    "workspace_created": lambda p: (
        f"workspace at {p.get('path', '?')} ({'new' if not p.get('reused') else 'reused'})"
    ),
    "agent_output": lambda p: (p.get("summary", "") or "")[:100],
    "pause_set": lambda p: f"paused (reason: {p.get('reason', '') or 'none'})",
    "pause_cleared": lambda p: f"resumed (was: {p.get('before', '?')})",
    "handoff_recorded": lambda p: f"handoff recorded for run #{p.get('run_id', '')}",
    "input_provided": lambda p: f"user responded ({p.get('kind', 'text')})",
    "field_changed": lambda p: f"{p.get('field', '?')} changed",
    "ticket_created": lambda p: _ticket_created_summary(p),
}


def _ticket_created_summary(p: dict) -> str:
    origin = p.get("origin", "unknown")
    src = p.get("source_file", "")
    if origin == "seed":
        msg = f"imported from {src or 'PRODUCT_BACKLOG.md'}"
    elif origin == "markdown_edit":
        msg = f"detected in {src or 'PRODUCT_BACKLOG.md'}"
    elif origin == "seek":
        line = p.get("source_line", "?")
        msg = f"discovered by Seek in {src or '?'}:{line}"
    elif origin == "agent":
        msg = "created by agent"
    elif origin == "human":
        msg = "created by user"
    elif origin == "journey_gap":
        msg = f"opened from journey {p.get('linked_journey', '?')} gap (run #{p.get('from_gap_run_id', '?')})"
    elif origin == "backfill":
        msg = "ticket existed before activity tracking (origin unknown)"
    else:
        msg = f"origin: {origin}"
    if p.get("draft"):
        msg += " (draft)"
    return msg


_RUN_LINK_EVENT_KINDS = frozenset(
    {
        "run_started",
        "run_succeeded",
        "run_failed",
        "run_cancelled",
        "handoff_recorded",
        "agent_output",
    }
)


def _event_summary(event_kind: str, payload: dict) -> str:
    """Return a one-line human summary for an activity event payload."""
    fn = _EVENT_KIND_SUMMARY_MAP.get(event_kind)
    if fn:
        try:
            return fn(payload) or ""
        except Exception:
            pass
    return ""


def _render_ticket_tab_overview(ticket: dict, proj: dict, port: int) -> str:
    """Render the Overview tab body for the full-page ticket view."""
    import html as _h

    pid = _safe_attr(proj["id"])
    api_base = f"/{pid}/api"  # origin-relative — works through Tailscale Serve, port-forwards, etc.
    tid = _h.escape(ticket["id"])
    section = ticket.get("section", "Ideas")
    description = _h.escape(ticket.get("description", "") or "")
    parent = _h.escape(ticket.get("parent", "") or "")
    gate_banner = _h.escape(GATE_BANNER_BY_SECTION.get(section, ""))
    is_container = ticket.get("is_container", False)

    criteria = ticket.get("acceptance_criteria", [])
    total_c = len(criteria)
    done_c = sum(1 for c in criteria if c.get("checked"))

    # Build criteria pill
    if total_c == 0:
        crit_pill = '<span class="tp-crit-pill tp-crit-zero">0 criteria</span>'
    elif done_c == total_c:
        crit_pill = f'<span class="tp-crit-pill tp-crit-done">{done_c}/{total_c}</span>'
    elif done_c > 0:
        crit_pill = (
            f'<span class="tp-crit-pill tp-crit-progress">{done_c}/{total_c}</span>'
        )
    else:
        crit_pill = f'<span class="tp-crit-pill tp-crit-empty">0/{total_c}</span>'

    # Criteria list HTML
    criteria_items_html = ""
    for i, c in enumerate(criteria):
        chk = "checked" if c.get("checked") else ""
        text_esc = _h.escape(c.get("text", ""))
        criteria_items_html += (
            f'<li class="tp-criterion {chk}" data-index="{i}">'
            f'<input type="checkbox" {"checked" if chk else ""} '
            f'  data-criterion-index="{i}" data-ticket-id="{tid}" class="tp-crit-check"> '
            f'<span class="tp-crit-text">{text_esc}</span>'
            f'<button class="tp-crit-ask-ai btn btn-ghost btn-sm" data-index="{i}" '
            f'  title="Ask AI to help fulfil this criterion">Ask AI</button>'
            f"</li>"
        )

    tags_html = ""
    for tag in ticket.get("tags", []):
        tags_html += f'<span class="tag-pill">{_h.escape(tag)}</span>'

    depends_html = ""
    for dep in ticket.get("depends", []):
        dep_esc = _h.escape(dep)
        depends_html += (
            f'<a class="tp-dep-link" href="/{pid}/tickets/{dep_esc}">{dep_esc}</a> '
        )

    # Container children panel
    children_panel = ""
    if is_container:
        with _db_lock:
            conn = get_db()
            init_db(conn)
            from actions import get_children_summary as _get_children_summary

            children_summary = _get_children_summary(conn, proj["id"], ticket["id"])
            children_rows = conn.execute(
                "SELECT id, title, section, status, priority FROM tickets "
                "WHERE parent = ? AND project_id = ? AND archived = 0 ORDER BY sort_order ASC",
                (ticket["id"], proj["id"]),
            ).fetchall()
            conn.close()

        total_ch = children_summary["total"]
        done_ch = children_summary["done"]
        child_cards_html = ""
        for ch in children_rows:
            ch_section_slug = {
                "Ideas": "ideas",
                "Backlog": "backlog",
                "WIP": "wip",
                "For Review": "review",
                "Done": "done",
            }.get(ch["section"], "backlog")
            child_cards_html += (
                f'<a class="tp-child-card tp-child-{ch_section_slug}" '
                f'   href="/{pid}/tickets/{_h.escape(ch["id"])}">'
                f'  <span class="tp-child-id">{_h.escape(ch["id"])}</span>'
                f'  <span class="tp-child-title">{_h.escape(ch["title"] or "")}</span>'
                f'  <span class="tp-child-section">{_h.escape(ch["section"])}</span>'
                f"</a>"
            )
        children_panel = f"""
<div class="tp-section" id="tp-section-children">
  <div class="tp-section-header">
    <h3>Children <span class="tp-crit-pill tp-crit-{"done" if done_ch == total_ch and total_ch > 0 else "progress" if done_ch > 0 else "empty"}">{done_ch}/{total_ch} done</span></h3>
  </div>
  <div class="tp-children-grid">
    {child_cards_html or "<span class='tp-empty'>No children yet.</span>"}
  </div>
</div>"""

    return f'''
<div class="tp-gate-banner">{gate_banner}</div>

<div class="tp-section" id="tp-section-criteria">
  <div class="tp-section-header">
    <h3>Acceptance Criteria {crit_pill}</h3>
  </div>
  <ul class="tp-criteria-list" id="tp-criteria-list" data-ticket-id="{tid}" data-api-base="{_safe_attr(api_base)}">
    {criteria_items_html or "<li class='tp-empty'>No criteria yet.</li>"}
  </ul>
  <div class="tp-criteria-add">
    <input type="text" id="tp-crit-input" placeholder="+ Add criterion and press Enter" class="tp-input">
    <button id="tp-crit-add-btn" class="btn btn-primary btn-sm">Add</button>
  </div>
</div>

<div class="tp-section" id="tp-section-description">
  <div class="tp-section-header">
    <h3>Description</h3>
  </div>
  <textarea class="tp-editor" id="tp-desc-editor" data-field="description" data-ticket-id="{tid}"
    data-api-base="{_safe_attr(api_base)}" placeholder="No description yet. Click to write one.">{description}</textarea>
</div>

{children_panel}

<div class="tp-section" id="tp-section-meta">
  <div class="tp-section-header"><h3>Details</h3></div>
  <dl class="tp-meta-list">
    <dt>Tags</dt><dd>{tags_html or "<span class='tp-empty'>none</span>"}</dd>
    <dt>Parent</dt><dd>{f'<a href="/{pid}/tickets/{parent}">{parent}</a>' if parent else "<span class='tp-empty'>none</span>"}</dd>
    <dt>Dependencies</dt><dd>{depends_html or "<span class='tp-empty'>none</span>"}</dd>
    <dt>Container</dt>
    <dd>
      <label class="tp-toggle-label">
        <input type="checkbox" id="tp-container-toggle" {"checked" if is_container else ""}
          data-ticket-id="{tid}" data-api-base="{_safe_attr(api_base)}">
        Container ticket
      </label>
    </dd>
  </dl>
</div>
'''


def _render_ticket_tab_activity(ticket: dict, proj: dict, port: int) -> str:
    """Render the Activity tab body for the full-page ticket view."""
    import html as _h

    pid = _safe_attr(proj["id"])
    tid = _safe_attr(ticket["id"])
    api_base = f"/{pid}/api"  # origin-relative — works through Tailscale Serve, port-forwards, etc.

    # Build group filter chips from canonical EVENT_GROUP_ORDER plus any extras
    # that might have been added to EVENT_KIND_GROUPS without updating the order list.
    from constants import EVENT_GROUP_COLORS, EVENT_GROUP_ORDER, EVENT_KIND_GROUPS

    seen = set(EVENT_GROUP_ORDER)
    extra_groups = sorted({g for g in EVENT_KIND_GROUPS.values() if g not in seen})
    all_groups = list(EVENT_GROUP_ORDER) + extra_groups

    chips_html = ""
    for grp in all_groups:
        color = EVENT_GROUP_COLORS.get(grp, "#94a3b8")
        chips_html += (
            f'<button class="tp-act-chip active" data-group="{_h.escape(grp)}" '
            f'style="--chip-color:{color}" title="Toggle {_h.escape(grp)} events">'
            f"{_h.escape(grp)}</button>"
        )

    return f'''
<div class="tp-activity-toolbar">
  <div class="tp-act-filter-row">
    <span class="tp-act-filter-label">Type</span>
    <div class="tp-act-chip-row" id="tp-act-type-chips">
      {chips_html}
      <button class="tp-act-chip-action" id="tp-act-toggle-all"
              title="Toggle all on/off">all</button>
    </div>
  </div>
  <div class="tp-act-filter-row">
    <span class="tp-act-filter-label">When</span>
    <div class="tp-act-chip-row" id="tp-act-range-chips">
      <button class="tp-act-range" data-range="1h">1h</button>
      <button class="tp-act-range" data-range="24h">24h</button>
      <button class="tp-act-range" data-range="7d">7d</button>
      <button class="tp-act-range active" data-range="all">All</button>
    </div>
  </div>
  <div class="tp-act-meta-row">
    <span class="tp-act-status" id="tp-act-status">Loading…</span>
    <span class="tp-act-poll-hint">Polls every 5s while focused</span>
  </div>
</div>
<div id="tp-activity-feed" class="tp-activity-grid"
  data-ticket-id="{tid}" data-api-base="{_safe_attr(api_base)}">
  <div class="tp-activity-loading">Loading activity…</div>
</div>
<div id="tp-ni-panel" class="tp-ni-panel hidden" data-testid="tp-ni-panel">
  <!-- Populated by JS when a needs_input run is detected -->
</div>
'''


def _render_run_detail_shell_html(
    panel_id: str = "tp-run-detail-panel",
    header_id: str = "tp-run-detail-header",
    extra_cls: str = "",
) -> str:
    """Return the HTML shell for the per-run detail panel.

    Shared between the ticket Runs tab and the Kitchen side panel.
    Callers pass distinct IDs so both can coexist on the same page without
    selector collisions (though currently they live on separate pages).
    """
    cls = f"tp-run-detail-panel{' ' + extra_cls if extra_cls else ''}"
    return f'''<div class="{cls} hidden" id="{panel_id}" data-testid="{panel_id}">
    <div class="tp-run-detail-header" id="{header_id}"></div>
    <div class="tp-run-detail-body">
      <section class="tp-run-section" id="tp-run-stdout-section">
        <h4>Output</h4>
        <pre class="tp-run-stdout" id="tp-run-stdout"></pre>
      </section>
      <section class="tp-run-section hidden" id="tp-run-handoff-section">
        <h4>Handoff</h4>
        <pre class="tp-run-handoff" id="tp-run-handoff"></pre>
      </section>
      <section class="tp-run-section hidden" id="tp-run-chat-section">
        <h4>Chat Transcript</h4>
        <div class="tp-run-chat" id="tp-run-chat"></div>
      </section>
      <section class="tp-run-section hidden" id="tp-run-evidence-section">
        <h4>Evidence Files</h4>
        <ul class="tp-run-evidence-list" id="tp-run-evidence-list"></ul>
      </section>
    </div>
  </div>'''


def _render_run_detail_js_fn(api_base_var: str = "TP_API_BASE") -> str:
    """Return the renderRunDetail(run, events, apiBase) JS function definition.

    The function accepts an optional third argument `apiBase` that overrides
    the module-level api_base_var for the evidence fetch. This lets Kitchen pass
    the per-project API base at call time without a closure race condition.
    Shared between ticket Runs tab and Kitchen side panel.
    """
    return f"""
function renderRunDetail(run, events, apiBase) {{
  if (!apiBase) apiBase = {api_base_var};
  var runDetailPanel = document.getElementById('tp-run-detail-panel');
  if (!runDetailPanel) return;
  runDetailPanel.classList.remove('hidden');
  var header = document.getElementById('tp-run-detail-header');
  if (header) {{
    var statusColor = run.status === 'succeeded' ? 'var(--green)' :
      run.status === 'failed' ? 'var(--red)' : 'var(--text-secondary)';
    var durationStr = run.duration_ms ? ' · ' + _fmtDuration(run.duration_ms) : '';
    var agentStr = (run.workflow_meta && run.workflow_meta.workflow_name)
      ? ' · ' + esc(run.workflow_meta.workflow_name) : '';
    var exitStr = (run.exit_code !== null && run.exit_code !== undefined)
      ? ' · exit ' + run.exit_code : '';
    header.innerHTML = 'Run #' + run.id +
      ' <span style="color:' + statusColor + '">' + esc(run.status) + '</span>' +
      durationStr + agentStr + exitStr;
  }}
  // Stdout.
  var stdoutEl = document.getElementById('tp-run-stdout');
  if (stdoutEl) {{ stdoutEl.textContent = run.summary || '(no output)'; }}
  // Reset hidden sections.
  var handoffSec = document.getElementById('tp-run-handoff-section');
  var chatSec = document.getElementById('tp-run-chat-section');
  var evidSec = document.getElementById('tp-run-evidence-section');
  if (handoffSec) handoffSec.classList.add('hidden');
  if (chatSec) chatSec.classList.add('hidden');
  if (evidSec) evidSec.classList.add('hidden');
  // Handoff + chat.
  try {{
    var meta = JSON.parse(run.metadata_json || run.workflow_meta && JSON.stringify(run.workflow_meta) || '{{}}');
    if (meta.handoff && Object.keys(meta.handoff).length) {{
      var handoffEl = document.getElementById('tp-run-handoff');
      if (handoffEl) handoffEl.textContent = JSON.stringify(meta.handoff, null, 2);
      if (handoffSec) handoffSec.classList.remove('hidden');
    }}
    if (meta.chat && meta.chat.length) {{
      var chatEl = document.getElementById('tp-run-chat');
      if (chatEl) {{
        chatEl.innerHTML = meta.chat.map(function(entry) {{
          return '<div class="tp-chat-entry ' + esc(entry.role) + '">' +
            '<div class="tp-chat-role">' + esc(entry.role) + '</div>' +
            '<div>' + esc(entry.content) + '</div></div>';
        }}).join('');
        if (chatSec) chatSec.classList.remove('hidden');
      }}
    }}
  }} catch(e) {{}}
  // Evidence.
  fetch(apiBase + '/runs/' + run.id + '/evidence')
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      var files = data.files || [];
      if (!files.length) return;
      var listEl = document.getElementById('tp-run-evidence-list');
      if (listEl) {{
        listEl.innerHTML = files.map(function(f) {{
          return '<li style="font-size:12px;padding:2px 0;">' + esc(f.name) +
            ' <span style="color:var(--text-tertiary)">(' + Math.round(f.size/1024) + 'KB)</span></li>';
        }}).join('');
        if (evidSec) evidSec.classList.remove('hidden');
      }}
    }}).catch(function() {{}});
}}"""


def _render_ticket_tab_runs(ticket: dict, proj: dict, port: int) -> str:
    """Render the Runs tab body — list of past/active runs with per-run detail panel.

    Uses the shared _render_run_detail_shell_html() helper for the panel markup.
    The JS renderRunDetail() function is also defined via _render_run_detail_js_fn()
    and inlined in the ticket page's script block (see _render_ticket_page).
    """
    pid = _safe_attr(proj["id"])
    tid = _safe_attr(ticket["id"])
    api_base = f"/{pid}/api"  # origin-relative — works through Tailscale Serve, port-forwards, etc.
    detail_shell = _render_run_detail_shell_html()
    return f'''
<div class="tp-runs-layout" data-ticket-id="{tid}" data-api-base="{_safe_attr(api_base)}">
  <div class="tp-runs-list" id="tp-runs-list">
    <div class="tp-activity-loading">Loading runs…</div>
  </div>
  <!-- Per-run detail panel (shared component from _render_run_detail_shell_html) -->
  {detail_shell}
</div>
'''


def _render_ticket_tab_files(ticket: dict, proj: dict, port: int) -> str:
    """Render the Files tab body — attachments list + placeholder for MD files."""
    pid = _safe_attr(proj["id"])
    tid = _safe_attr(ticket["id"])
    api_base = f"/{pid}/api"  # origin-relative — works through Tailscale Serve, port-forwards, etc.
    return f'''
<div class="tp-files-layout" data-ticket-id="{tid}" data-api-base="{_safe_attr(api_base)}">
  <div id="tp-attachments-list" class="tp-files-list">
    <div class="tp-activity-loading">Loading attachments…</div>
  </div>
  <div class="tp-files-placeholder">
    <span class="tp-empty">Linked markdown files — coming soon.</span>
  </div>
</div>
'''


def _render_ticket_tab_graph(ticket: dict, proj: dict, port: int) -> str:
    """Render the Graph tab body — dependency graph placeholder."""
    return """
<div class="tp-graph-placeholder">
  <div class="tp-empty tp-empty-large">Dependency graph — coming soon.</div>
</div>
"""


def _render_ticket_tab_spec(ticket: dict, proj: dict, port: int) -> str:
    """Render the Spec tab — OpenSpec status, unrecorded changes, editable docs."""
    import html as _h

    pid = _safe_attr(proj["id"])
    tid = _h.escape(ticket["id"])
    api_base = f"/{pid}/api"

    return f'''
<div class="tp-section" id="tp-section-spec" data-ticket-id="{tid}" data-api-base="{_safe_attr(api_base)}">
  <div class="tp-section-header"><h3>OpenSpec</h3></div>
  <div id="tp-spec-status-strip" class="tp-spec-status-strip">
    <span class="tp-empty">Loading spec status…</span>
  </div>
  <div id="tp-spec-unrecorded" class="tp-spec-unrecorded" style="display:none"></div>
  <div id="tp-spec-edge" class="tp-spec-edge" style="display:none"></div>
  <div id="tp-spec-docs" class="tp-spec-docs"></div>
</div>
<script>
(function() {{
  var root = document.getElementById('tp-section-spec');
  if (!root) return;
  var API = root.getAttribute('data-api-base') || TP_API_BASE;
  var TID = root.getAttribute('data-ticket-id') || TP_TICKET_ID;
  var strip = document.getElementById('tp-spec-status-strip');
  var unrecEl = document.getElementById('tp-spec-unrecorded');
  var edgeEl = document.getElementById('tp-spec-edge');
  var docsEl = document.getElementById('tp-spec-docs');

  function esc(s) {{
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }}

  function wireDocEditor(details, path, readonly) {{
    var ta = details.querySelector('textarea');
    var status = details.querySelector('.tp-spec-doc-status');
    if (!ta) return;
    if (readonly) {{
      ta.disabled = true;
      if (status) status.textContent = 'read-only (archived)';
      return;
    }}
    var loaded = false;
    var timer = null;
    details.addEventListener('toggle', function() {{
      if (!details.open || loaded) return;
      if (status) status.textContent = 'loading…';
      fetch(API + '/tickets/' + encodeURIComponent(TID) + '/spec/doc?path=' + encodeURIComponent(path))
        .then(function(r) {{ return r.json().then(function(j) {{ return {{ok: r.ok, j: j}}; }}); }})
        .then(function(res) {{
          if (!res.ok) {{
            if (status) status.textContent = (res.j && res.j.error) || 'load failed';
            return;
          }}
          ta.value = res.j.content || '';
          loaded = true;
          if (res.j.readonly) {{
            ta.disabled = true;
            if (status) status.textContent = 'read-only (archived)';
            return;
          }}
          if (status) status.textContent = 'loaded';
          ta.addEventListener('input', function() {{
            clearTimeout(timer);
            if (status) status.textContent = 'saving…';
            timer = setTimeout(function() {{
              fetch(API + '/tickets/' + encodeURIComponent(TID) + '/spec/doc', {{
                method: 'PUT',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{path: path, content: ta.value}}),
              }}).then(function(r) {{
                if (status) status.textContent = r.ok ? 'saved' : 'save failed';
              }}).catch(function() {{
                if (status) status.textContent = 'save failed';
              }});
            }}, 1000);
          }});
        }})
        .catch(function() {{
          if (status) status.textContent = 'load failed';
        }});
    }});
  }}

  function renderPayload(data) {{
    var link = data.link || null;
    var lane = link ? (link.lane || '') : '';
    var change = link ? (link.change || '') : '';
    strip.innerHTML =
      '<span class="tp-spec-badge" data-status="' + esc(data.status) + '">' + esc(data.status) + '</span>' +
      (lane ? ' <span class="tp-spec-lane">lane ' + esc(lane) + '</span>' : '') +
      (change ? ' <code class="tp-spec-change">' + esc(change) + '</code>' : '') +
      '<div class="tp-spec-detail">' + esc(data.detail || '') + '</div>';

    // Unrecorded banner
    var unrec = data.unrecorded || [];
    if (unrec.length) {{
      unrecEl.style.display = '';
      var html = '<div class="tp-spec-unrecorded-title">Unrecorded OpenSpec change(s) on disk</div>';
      unrec.forEach(function(u, idx) {{
        html +=
          '<div class="tp-spec-unrecorded-row" data-name="' + esc(u.name) + '">' +
          '<code>' + esc(u.name) + '</code> ' +
          '<label>lane <select class="tp-spec-lane-sel" data-idx="' + idx + '">' +
          '<option value="A">A</option><option value="B" selected>B</option><option value="C">C</option>' +
          '</select></label> ' +
          '<button type="button" class="btn btn-primary btn-sm tp-spec-record-btn" data-idx="' + idx + '">' +
          'Record link on ticket</button></div>';
      }});
      unrecEl.innerHTML = html;
      unrecEl.querySelectorAll('.tp-spec-record-btn').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          var i = parseInt(btn.getAttribute('data-idx'), 10);
          var row = unrec[i];
          if (!row) return;
          var sel = unrecEl.querySelector('.tp-spec-lane-sel[data-idx="' + i + '"]');
          var L = sel ? sel.value : 'B';
          var content = L + ':' + row.name;
          fetch(API + '/tickets/' + encodeURIComponent(TID) + '/readiness/spec', {{
            method: 'PUT',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{content: content}}),
          }}).then(function() {{ window.location.reload(); }}).catch(console.error);
        }});
      }});
    }} else {{
      unrecEl.style.display = 'none';
      unrecEl.innerHTML = '';
    }}

    // Edge-state hints
    edgeEl.style.display = 'none';
    edgeEl.innerHTML = '';
    if (data.status === 'undeclared') {{
      edgeEl.style.display = '';
      edgeEl.innerHTML = '<p class="tp-empty">No OpenSpec change yet. Declare one with ' +
        '<code>tickets-cli.py spec &lt;project&gt; ' + esc(TID) + ' --lane A|B|C</code>.</p>';
    }} else if (data.status === 'linked_missing') {{
      edgeEl.style.display = '';
      edgeEl.innerHTML = '<p class="tp-spec-warn">The flag names a change with no directory on disk.</p>';
    }} else if (data.status === 'no_delta') {{
      edgeEl.style.display = '';
      var reason = (link && link.note) ? link.note : '(no reason recorded)';
      edgeEl.innerHTML = '<p class="tp-empty">Lane C — no spec delta. Reason: ' + esc(reason) + '</p>';
    }} else if (data.status === 'declared_invalid') {{
      edgeEl.style.display = '';
      edgeEl.innerHTML = '<p class="tp-spec-warn">Spec flag is unparseable. Raw content is shown in the status strip.</p>';
    }}

    // Documents
    docsEl.innerHTML = '';
    var ch = data.change;
    if (ch && ch.docs && ch.docs.length) {{
      ch.docs.forEach(function(path) {{
        var details = document.createElement('details');
        details.className = 'tp-spec-doc';
        details.setAttribute('data-path', path);
        details.innerHTML =
          '<summary><code>' + esc(path) + '</code> ' +
          '<span class="tp-spec-doc-status"></span></summary>' +
          '<textarea class="tp-editor tp-spec-doc-editor" rows="16" ' +
          'style="font-family:var(--font-mono);width:100%;" ' +
          'placeholder="Expand to load…"></textarea>';
        docsEl.appendChild(details);
        wireDocEditor(details, path, !!ch.archived);
      }});
    }}
  }}

  fetch(API + '/tickets/' + encodeURIComponent(TID) + '/spec')
    .then(function(r) {{ return r.json(); }})
    .then(renderPayload)
    .catch(function(err) {{
      strip.innerHTML = '<span class="tp-empty">Failed to load spec status.</span>';
      console.error(err);
    }});
}})();
</script>
'''


def _render_ticket_page(
    proj: dict, port: int, ticket_id: str, tab: str = "overview"
) -> str | None:
    """Render the full-page ticket view for /{project_id}/tickets/{ticket_id}?tab=.

    Returns None if the ticket is not found.
    Tabs: overview (default) | activity | runs | files | graph

    Lane C handoff note: the per-run detail component lives in
    _render_ticket_tab_runs() inside #tp-run-detail-panel. Extract it into
    _render_run_detail_component(run_id, run_dict, proj, port) when building
    the Kitchen side panel so both surfaces share the same markup.
    """
    import html as _h

    ticket = _get_ticket_json(proj["id"], ticket_id)
    if ticket is None:
        return None

    pid = _safe_attr(proj["id"])
    api_base = f"/{pid}/api"  # origin-relative — works through Tailscale Serve, port-forwards, etc.
    tid = _h.escape(ticket["id"])
    title = _h.escape(ticket["title"] or "")
    section = ticket.get("section", "Ideas")
    status = _h.escape(ticket.get("status", ""))
    priority = _h.escape(ticket.get("priority", "medium"))
    is_container = ticket.get("is_container", False)

    valid_tabs = ("overview", "spec", "activity", "runs", "files", "graph")
    if tab not in valid_tabs:
        tab = "overview"

    # Render the active tab body.
    if tab == "overview":
        tab_body = _render_ticket_tab_overview(ticket, proj, port)
    elif tab == "spec":
        tab_body = _render_ticket_tab_spec(ticket, proj, port)
    elif tab == "activity":
        tab_body = _render_ticket_tab_activity(ticket, proj, port)
    elif tab == "runs":
        tab_body = _render_ticket_tab_runs(ticket, proj, port)
    elif tab == "files":
        tab_body = _render_ticket_tab_files(ticket, proj, port)
    else:
        tab_body = _render_ticket_tab_graph(ticket, proj, port)

    # Build tab nav.
    def _tab_link(t: str, label: str) -> str:
        active_cls = " tp-tab-active" if t == tab else ""
        return (
            f'<a class="tp-tab{active_cls}" href="/{pid}/tickets/{tid}?tab={t}">'
            f"{label}</a>"
        )

    tabs_html = (
        _tab_link("overview", "Overview")
        + _tab_link("spec", "Spec")
        + _tab_link("activity", "Activity")
        + _tab_link("runs", "Runs")
        + _tab_link("files", "Files")
        + _tab_link("graph", "Graph")
    )

    # Readiness dots.
    criteria = ticket.get("acceptance_criteria", [])
    total_c = len(criteria)
    done_c = sum(1 for c in criteria if c.get("checked"))
    has_desc = bool(ticket.get("description", "").strip())
    has_reviewed = bool(ticket.get("readiness_flags", {}).get("reviewed"))
    container_badge = (
        '<span class="tp-container-badge">Container</span>' if is_container else ""
    )

    rail_css = gen.build_nav_rail_css()
    rail_html = gen.build_nav_rail_html()
    rail_js = gen.build_nav_rail_js()
    drawer_css = gen.build_settings_drawer_css()
    drawer_html = gen.build_settings_drawer_html(gen._svg_icon("x", 14))
    drawer_js = gen.build_settings_drawer_js()

    with _PROJECTS_CACHE_LOCK:
        projects_meta_json = json.dumps(
            [
                {"id": p["id"], "name": p.get("name", p["id"])}
                for p in _PROJECTS_CACHE.values()
            ]
        )

    # Serialise event kind maps to JS.
    event_labels_js = json.dumps(EVENT_KIND_LABELS)
    event_icons_js = json.dumps(EVENT_KIND_ICONS)
    event_groups_js = json.dumps(EVENT_KIND_GROUPS)
    event_group_colors_js = json.dumps(EVENT_GROUP_COLORS)

    return f'''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>{tid} — {title}</title>
{PWA_HEAD_TAGS}
<meta name="current-project" content="{pid}">
<meta name="edit-api" content="{api_base}">
<meta name="projects-list" content='{_safe_attr(projects_meta_json)}'>
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
body {{ background: var(--bg-page); color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  min-height: 100vh; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
/* Header */
.tp-header {{ display: flex; align-items: center; gap: 12px; padding: 8px 20px;
  border-bottom: 1px solid var(--border-subtle); background: var(--bg-surface); flex-wrap: wrap; }}
.tp-header-id {{ font-family: "SF Mono", Monaco, monospace; font-size: 13px;
  color: var(--accent); font-weight: 600; flex-shrink: 0; }}
.tp-header-title {{ font-size: 15px; font-weight: 600; flex: 1; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.tp-back {{ font-size: 12px; color: var(--text-secondary); flex-shrink: 0; }}
.tp-back:hover {{ color: var(--text-primary); }}
/* Meta strip */
.tp-meta-strip {{ display: flex; align-items: center; gap: 8px; padding: 6px 20px;
  border-bottom: 1px solid var(--border-subtle); background: var(--bg-surface);
  font-size: 12px; flex-wrap: wrap; }}
.tp-chip {{ display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px;
  border-radius: 12px; background: var(--bg-hover); border: 1px solid var(--border-default);
  color: var(--text-secondary); font-size: 11px; }}
.tp-chip.priority-high {{ border-color: rgba(239,68,68,0.3); color: var(--red); }}
.tp-chip.priority-medium {{ border-color: rgba(234,179,8,0.3); color: var(--yellow); }}
.tp-chip.priority-low {{ border-color: rgba(156,163,175,0.3); }}
.tp-container-badge {{ display: inline-flex; padding: 2px 8px; border-radius: 10px;
  font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
  background: rgba(59,130,246,0.15); color: var(--accent); border: 1px solid rgba(59,130,246,0.3); }}
/* Tab nav */
.tp-tabs {{ display: flex; gap: 0; padding: 0 20px;
  border-bottom: 1px solid var(--border-subtle); background: var(--bg-surface); }}
.tp-tab {{ padding: 10px 16px; font-size: 13px; font-weight: 500;
  color: var(--text-secondary); border-bottom: 2px solid transparent;
  text-decoration: none; transition: color 0.15s; }}
.tp-tab:hover {{ color: var(--text-primary); text-decoration: none; }}
.tp-tab-active {{ color: var(--text-primary); border-bottom-color: var(--accent); }}
/* Content area */
.tp-content {{ padding: 24px; max-width: 900px; }}
/* Sections */
.tp-section {{ background: var(--bg-surface); border: 1px solid var(--border-subtle);
  border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
.tp-section-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }}
.tp-section-header h3 {{ font-size: 13px; font-weight: 600; }}
/* Gate banner */
.tp-gate-banner {{ background: rgba(59,130,246,0.08); border: 1px solid rgba(59,130,246,0.2);
  border-radius: 6px; padding: 8px 14px; font-size: 12px; color: var(--text-secondary);
  margin-bottom: 16px; }}
/* Criteria */
.tp-criteria-list {{ list-style: none; padding: 0; }}
.tp-criterion {{ display: flex; align-items: center; gap: 8px; padding: 6px 0;
  border-bottom: 1px solid var(--border-subtle); font-size: 13px; }}
.tp-criterion:last-child {{ border-bottom: none; }}
.tp-criterion.checked .tp-crit-text {{ text-decoration: line-through; opacity: 0.6; }}
.tp-crit-check {{ flex-shrink: 0; cursor: pointer; }}
.tp-crit-text {{ flex: 1; }}
.tp-crit-ask-ai {{ opacity: 0; font-size: 10px; padding: 2px 6px; }}
.tp-criterion:hover .tp-crit-ask-ai {{ opacity: 0.7; }}
.tp-criteria-add {{ display: flex; gap: 8px; margin-top: 10px; }}
.tp-input {{ background: var(--bg-card); border: 1px solid var(--border-default);
  border-radius: 6px; padding: 6px 10px; font-size: 13px; color: var(--text-primary);
  flex: 1; }}
.tp-input:focus {{ outline: none; border-color: var(--accent); }}
/* Criteria pill */
.tp-crit-pill {{ display: inline-block; padding: 1px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 600; }}
.tp-crit-zero {{ background: var(--bg-hover); color: var(--text-tertiary); }}
.tp-crit-empty {{ background: var(--bg-hover); color: var(--text-secondary); }}
.tp-crit-progress {{ background: rgba(234,179,8,0.15); color: var(--yellow); }}
.tp-crit-done {{ background: rgba(34,197,94,0.15); color: var(--green); }}
/* Editor */
.tp-editor {{ width: 100%; min-height: 120px; background: var(--bg-card);
  border: 1px solid var(--border-default); border-radius: 6px; padding: 10px;
  font-size: 13px; color: var(--text-primary); resize: vertical; font-family: inherit; }}
.tp-editor:focus {{ outline: none; border-color: var(--accent); }}
/* Meta list */
.tp-meta-list {{ display: grid; grid-template-columns: 100px 1fr; gap: 8px; font-size: 13px; }}
.tp-meta-list dt {{ color: var(--text-secondary); font-weight: 500; }}
.tp-meta-list dd {{ color: var(--text-primary); }}
/* Children grid */
.tp-children-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 8px; }}
.tp-child-card {{ display: flex; flex-direction: column; gap: 4px; padding: 10px;
  background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 6px;
  text-decoration: none; color: var(--text-primary); transition: background 0.15s; }}
.tp-child-card:hover {{ background: var(--bg-hover); text-decoration: none; }}
.tp-child-id {{ font-family: "SF Mono", Monaco, monospace; font-size: 10px;
  color: var(--accent); }}
.tp-child-title {{ font-size: 12px; }}
.tp-child-section {{ font-size: 10px; color: var(--text-tertiary); }}
.tp-child-ideas {{ border-left: 3px solid var(--text-tertiary); }}
.tp-child-backlog {{ border-left: 3px solid var(--accent); }}
.tp-child-wip {{ border-left: 3px solid var(--yellow); }}
.tp-child-review {{ border-left: 3px solid #a855f7; }}
.tp-child-done {{ border-left: 3px solid var(--green); }}
/* Toggle */
.tp-toggle-label {{ display: flex; align-items: center; gap: 6px; cursor: pointer; font-size: 13px; }}
/* Activity feed — compact grid layout */
.tp-activity-toolbar {{ display: flex; flex-direction: column; gap: 8px;
  padding: 0 0 12px 0; margin-bottom: 8px;
  border-bottom: 1px solid var(--border-subtle); }}
.tp-act-filter-row {{ display: flex; align-items: center; gap: 12px;
  font-size: 11px; }}
.tp-act-filter-label {{ font-weight: 600; color: var(--text-tertiary);
  text-transform: uppercase; letter-spacing: 0.5px; min-width: 38px; }}
.tp-act-chip-row {{ display: flex; gap: 4px; flex-wrap: wrap;
  align-items: center; }}
.tp-act-chip {{ font-size: 10px; font-weight: 600; padding: 3px 8px;
  border-radius: 10px; border: 1px solid var(--border-default);
  background: transparent; color: var(--text-tertiary);
  cursor: pointer; font-family: inherit; letter-spacing: 0.3px;
  text-transform: uppercase; transition: all 0.1s; }}
.tp-act-chip:hover {{ border-color: var(--border-strong);
  color: var(--text-secondary); }}
.tp-act-chip.active {{ background: color-mix(in srgb, var(--chip-color) 22%, transparent);
  border-color: var(--chip-color); color: var(--text-primary); }}
.tp-act-chip-action {{ font-size: 10px; padding: 3px 8px; border-radius: 10px;
  border: 1px dashed var(--border-default); background: transparent;
  color: var(--text-tertiary); cursor: pointer; font-family: inherit;
  text-transform: lowercase; }}
.tp-act-chip-action:hover {{ color: var(--accent); border-color: var(--accent); }}
.tp-act-range {{ font-size: 10px; padding: 3px 10px; border-radius: 10px;
  border: 1px solid var(--border-default); background: transparent;
  color: var(--text-tertiary); cursor: pointer; font-family: inherit;
  font-variant-numeric: tabular-nums; }}
.tp-act-range:hover {{ border-color: var(--border-strong);
  color: var(--text-secondary); }}
.tp-act-range.active {{ background: rgba(59,130,246,0.15);
  border-color: var(--accent); color: var(--accent); }}
.tp-act-meta-row {{ display: flex; gap: 12px; align-items: center;
  font-size: 10px; color: var(--text-tertiary); }}
.tp-act-status {{ font-variant-numeric: tabular-nums; }}
.tp-act-poll-hint {{ margin-left: auto; }}

.tp-activity-grid {{ display: grid;
  grid-template-columns:
    [time]  minmax(110px, max-content)
    [badge] minmax(72px, max-content)
    [actor] minmax(60px, max-content)
    [body]  1fr
    [chev]  24px;
  row-gap: 0; column-gap: 12px;
  font-size: 12px; }}
.tp-act-row {{ display: contents; cursor: pointer; }}
.tp-act-row > * {{ padding: 6px 0;
  border-bottom: 1px solid var(--border-subtle);
  align-self: start; }}
.tp-act-row:hover > * {{ background: rgba(255,255,255,0.02); }}
.tp-act-row.expanded .tp-act-chevron {{ transform: rotate(90deg); }}
.tp-act-time {{ color: var(--text-tertiary); font-family: var(--font-mono, monospace);
  font-size: 11px; font-variant-numeric: tabular-nums; line-height: 1.3;
  white-space: nowrap; }}
.tp-act-time-rel {{ color: var(--text-secondary); display: block; font-size: 10px; }}
.tp-act-badge {{ font-size: 10px; font-weight: 600; padding: 2px 8px;
  border-radius: 4px; text-transform: uppercase; letter-spacing: 0.4px;
  background: color-mix(in srgb, var(--badge-color, #94a3b8) 18%, transparent);
  color: var(--badge-color, var(--text-secondary));
  border: 1px solid color-mix(in srgb, var(--badge-color, #94a3b8) 35%, transparent);
  display: inline-block; line-height: 1.2; align-self: center; }}
.tp-act-actor {{ color: var(--text-tertiary); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.4px; align-self: center;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  max-width: 160px; }}
.tp-act-actor.is-agent {{ color: var(--accent); text-transform: none;
  letter-spacing: 0; font-size: 11px; font-weight: 500; }}
.tp-act-actor.is-agent::before {{ content: "→ "; opacity: 0.6; }}
.tp-act-summary {{ color: var(--text-primary); min-width: 0;
  overflow-wrap: anywhere; }}
.tp-act-summary .tp-act-kind {{ color: var(--text-tertiary); font-size: 10px;
  margin-left: 6px; font-variant-numeric: tabular-nums; }}
.tp-act-chevron {{ color: var(--text-tertiary); font-size: 12px;
  text-align: center; transition: transform 0.15s; align-self: center; }}
.tp-act-detail {{ grid-column: time / -1;
  background: var(--bg-hover); border-bottom: 1px solid var(--border-subtle);
  padding: 10px 14px; font-size: 11px; }}
.tp-act-detail-header {{ display: flex; gap: 12px; align-items: center;
  color: var(--text-tertiary); margin-bottom: 8px; }}
.tp-act-detail-header .tp-act-detail-link {{ margin-left: auto;
  color: var(--accent); font-size: 11px; text-decoration: none; }}
.tp-act-detail-header .tp-act-detail-link:hover {{ text-decoration: underline; }}
.tp-act-detail-payload {{ background: var(--bg-card); border-radius: 4px;
  padding: 8px 10px; color: var(--text-secondary); font-family: var(--font-mono, monospace);
  font-size: 10px; white-space: pre-wrap; word-break: break-all;
  max-height: 240px; overflow-y: auto; }}
.tp-act-empty {{ grid-column: time / -1; padding: 24px; text-align: center;
  color: var(--text-tertiary); font-size: 12px; }}
.tp-activity-run-link {{ font-size: 11px; color: var(--accent); }}
/* Runs layout */
.tp-runs-layout {{ display: grid; grid-template-columns: 280px 1fr; gap: 16px; }}
.tp-runs-list {{ background: var(--bg-surface); border: 1px solid var(--border-subtle);
  border-radius: 8px; overflow: hidden; max-height: 600px; overflow-y: auto; }}
.tp-run-row {{ padding: 10px 14px; border-bottom: 1px solid var(--border-subtle);
  cursor: pointer; font-size: 12px; transition: background 0.15s; }}
.tp-run-row:hover {{ background: var(--bg-hover); }}
.tp-run-row.active {{ background: rgba(59,130,246,0.1); }}
.tp-run-row:last-child {{ border-bottom: none; }}
.tp-run-detail-panel {{ background: var(--bg-surface); border: 1px solid var(--border-subtle);
  border-radius: 8px; padding: 16px; overflow-y: auto; max-height: 600px; }}
.tp-run-detail-header {{ font-size: 13px; font-weight: 600; margin-bottom: 12px;
  padding-bottom: 8px; border-bottom: 1px solid var(--border-subtle); }}
.tp-run-section {{ margin-bottom: 16px; }}
.tp-run-section h4 {{ font-size: 12px; font-weight: 600; color: var(--text-secondary);
  margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
.tp-run-stdout {{ font-family: "SF Mono", Monaco, monospace; font-size: 11px;
  background: var(--bg-card); padding: 10px; border-radius: 6px;
  white-space: pre-wrap; max-height: 300px; overflow-y: auto; color: var(--text-primary); }}
.tp-run-chat {{ font-size: 12px; }}
.tp-chat-entry {{ margin-bottom: 10px; padding: 8px; border-radius: 6px; }}
.tp-chat-entry.user {{ background: rgba(59,130,246,0.1); }}
.tp-chat-entry.agent {{ background: var(--bg-card); }}
.tp-chat-entry.agent_marker {{ background: rgba(234,179,8,0.1); font-family: monospace;
  font-size: 11px; }}
.tp-chat-role {{ font-weight: 600; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.4px; color: var(--text-secondary); margin-bottom: 4px; }}
/* Files */
.tp-files-layout {{ display: flex; flex-direction: column; gap: 16px; }}
.tp-files-list {{ background: var(--bg-surface); border: 1px solid var(--border-subtle);
  border-radius: 8px; padding: 16px; }}
/* Graph placeholder */
.tp-graph-placeholder {{ display: flex; align-items: center; justify-content: center;
  min-height: 200px; }}
/* Needs-input panel */
.tp-ni-panel {{ background: var(--bg-surface); border: 1px solid rgba(59,130,246,0.3);
  border-radius: 8px; padding: 16px; margin-top: 16px; }}
.tp-ni-panel.tp-ni-propose {{ border-color: rgba(234,179,8,0.3); }}
.tp-ni-header {{ font-size: 13px; font-weight: 600; margin-bottom: 10px; }}
.tp-ni-prompt {{ font-size: 12px; color: var(--text-secondary); margin-bottom: 12px; }}
.tp-ni-textarea {{ width: 100%; min-height: 80px; background: var(--bg-card);
  border: 1px solid var(--border-default); border-radius: 6px; padding: 8px;
  font-size: 12px; color: var(--text-primary); resize: vertical; font-family: inherit; }}
.tp-ni-actions {{ display: flex; gap: 8px; margin-top: 8px; }}
.tp-propose-items {{ list-style: none; padding: 0; margin-bottom: 12px; }}
.tp-propose-item {{ display: flex; align-items: flex-start; gap: 8px; padding: 6px 0;
  border-bottom: 1px solid var(--border-subtle); font-size: 12px; }}
.tp-propose-item:last-child {{ border-bottom: none; }}
.tp-propose-label {{ font-size: 10px; text-transform: uppercase; color: var(--text-tertiary);
  font-weight: 600; width: 80px; flex-shrink: 0; margin-top: 2px; }}
/* Buttons */
.btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px;
  border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 500; border: 1px solid;
  transition: background 0.15s; }}
.btn-primary {{ background: rgba(59,130,246,0.15); border-color: rgba(59,130,246,0.3);
  color: var(--accent); }}
.btn-primary:hover {{ background: rgba(59,130,246,0.25); }}
.btn-ghost {{ background: transparent; border-color: var(--border-default);
  color: var(--text-secondary); }}
.btn-ghost:hover {{ background: var(--bg-hover); color: var(--text-primary); }}
.btn-sm {{ padding: 4px 10px; font-size: 11px; }}
/* Misc */
.tp-empty {{ color: var(--text-tertiary); font-size: 12px; font-style: italic; }}
.tp-empty-large {{ font-size: 14px; }}
.tp-activity-loading {{ padding: 20px; color: var(--text-tertiary); font-size: 12px; }}
.hidden {{ display: none !important; }}
{rail_css}
{drawer_css}
</style>
</head>
<body>
{rail_html}
<div class="tp-header">
  <a class="tp-back" href="/{pid}/">← Kanban</a>
  <span class="tp-header-id">{tid}</span>
  <button type="button" class="star-toggle tp-star" data-bookmark-toggle data-ticket-id="{
        tid
    }" data-testid="tp-star" title="Bookmark" aria-label="Bookmark" aria-pressed="false">{
        gen._svg_icon("star", 14)
    }</button>
  {container_badge}
  <span class="tp-header-title">{title}</span>
</div>
<div class="tp-meta-strip">
  <span class="tp-chip priority-{priority}">{priority}</span>
  <span class="tp-chip">{status}</span>
  <span class="tp-chip">{_h.escape(section)}</span>
  {'<span class="tp-chip" style="color:var(--green);">D</span>' if has_desc else ""}
  {
        '<span class="tp-chip tp-crit-pill tp-crit-done">C</span>'
        if total_c > 0 and done_c == total_c
        else f'<span class="tp-chip tp-crit-pill tp-crit-progress">{done_c}/{total_c}</span>'
        if total_c > 0
        else '<span class="tp-chip tp-crit-pill tp-crit-zero">C</span>'
    }
  {'<span class="tp-chip" style="color:var(--green);">L</span>' if has_reviewed else ""}
</div>
<div class="tp-tabs">
  {tabs_html}
</div>
<div class="tp-content">
  {tab_body}
</div>
{drawer_html}
<script>
var EVENT_KIND_LABELS = {event_labels_js};
var EVENT_KIND_ICONS  = {event_icons_js};
var EVENT_KIND_GROUPS = {event_groups_js};
var EVENT_GROUP_COLORS = {event_group_colors_js};
var TP_API_BASE = {json.dumps(api_base)};
var TP_TICKET_ID = {json.dumps(ticket["id"])};
var TP_PROJECT_ID = {json.dumps(proj["id"])};
var TP_PORT = {port};

// Bookmarks/Recents (I-43): record this visit. The rail's listener will
// touch /api/recents and refresh the section. Fired after the DOM is ready
// so the listener (installed by the rail script) is in place.
document.addEventListener('DOMContentLoaded', function(){{
  document.dispatchEvent(new CustomEvent('tt:ticket-opened',
    {{detail: {{ticketId: TP_TICKET_ID}}}}));
}});

// ── Relative timestamp ────────────────────────────────────────────
function relativeTime(iso) {{
  if (!iso) return '';
  var d = new Date(iso); var now = Date.now(); var diff = now - d.getTime();
  if (diff < 60000) return 'just now';
  if (diff < 3600000) return Math.floor(diff/60000) + 'm ago';
  if (diff < 86400000) return Math.floor(diff/3600000) + 'h ago';
  return Math.floor(diff/86400000) + 'd ago';
}}

// ── Criteria interactions ─────────────────────────────────────────
document.addEventListener('change', function(e) {{
  var chk = e.target.closest('.tp-crit-check');
  if (!chk) return;
  var idx = parseInt(chk.dataset.criterionIndex, 10);
  var ticketId = chk.dataset.ticketId;
  var body = chk.checked ? {{check_criteria: idx+1}} : {{uncheck_criteria: idx+1}};
  fetch(TP_API_BASE + '/tickets/' + encodeURIComponent(ticketId), {{
    method: 'PUT',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body),
  }}).catch(console.error);
}});

// ── Criteria add ──────────────────────────────────────────────────
var critInput = document.getElementById('tp-crit-input');
var critAddBtn = document.getElementById('tp-crit-add-btn');
function addCriterion() {{
  var text = critInput ? critInput.value.trim() : '';
  if (!text) return;
  fetch(TP_API_BASE + '/tickets/' + encodeURIComponent(TP_TICKET_ID), {{
    method: 'PUT',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{add_criteria: text}}),
  }}).then(function() {{
    critInput.value = '';
    window.location.reload();
  }}).catch(console.error);
}}
if (critAddBtn) critAddBtn.addEventListener('click', addCriterion);
if (critInput) critInput.addEventListener('keydown', function(e) {{
  if (e.key === 'Enter') {{ e.preventDefault(); addCriterion(); }}
}});

// ── Description auto-save ─────────────────────────────────────────
var descEditor = document.getElementById('tp-desc-editor');
if (descEditor) {{
  var descTimer = null;
  descEditor.addEventListener('input', function() {{
    clearTimeout(descTimer);
    descTimer = setTimeout(function() {{
      fetch(TP_API_BASE + '/tickets/' + encodeURIComponent(TP_TICKET_ID), {{
        method: 'PUT',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{description: descEditor.value}}),
      }}).catch(console.error);
    }}, 1000);
  }});
}}

// ── Container toggle ──────────────────────────────────────────────
var containerToggle = document.getElementById('tp-container-toggle');
if (containerToggle) {{
  containerToggle.addEventListener('change', function() {{
    fetch(TP_API_BASE + '/tickets/' + encodeURIComponent(TP_TICKET_ID), {{
      method: 'PUT',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{is_container: containerToggle.checked}}),
    }}).then(function() {{ window.location.reload(); }}).catch(console.error);
  }});
}}

// ── Activity feed ─────────────────────────────────────────────────
var activityFeed = document.getElementById('tp-activity-feed');
var activityPollTimer = null;
// Local cache so filter changes don't re-fetch from server.
var _activityEvents = [];
// Filter state, persisted across re-fetches within this session.
var _activityFilters = {{
  excludedGroups: new Set(),         // Set of group names hidden by user
  range: 'all',                       // '1h' | '24h' | '7d' | 'all'
  expandedIds: new Set(),             // Event IDs whose detail row is open
}};
var _RANGE_MS = {{ '1h': 3600000, '24h': 86400000, '7d': 604800000 }};

function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}

function _formatAbsoluteTime(iso) {{
  // "11:47:23" if today, else "May 11 11:47" — caller appends date prefix.
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return esc(iso);
  var hh = String(d.getHours()).padStart(2, '0');
  var mm = String(d.getMinutes()).padStart(2, '0');
  var ss = String(d.getSeconds()).padStart(2, '0');
  var now = new Date();
  var sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (sameDay) return hh + ':' + mm + ':' + ss;
  var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return months[d.getMonth()] + ' ' + d.getDate() + ' ' + hh + ':' + mm;
}}

function _eventSummary(ev) {{
  var p = ev.payload || {{}};
  if (ev.event_kind === 'run_started') return 'Run #' + (p.run_id||'') + ' started';
  if (ev.event_kind === 'run_succeeded') return 'Run #' + (p.run_id||'') + ' succeeded — ' + esc((p.summary||'').substring(0,80));
  if (ev.event_kind === 'run_failed') return 'Run #' + (p.run_id||'') + ' failed — ' + esc((p.error_class||p.error_message||'').substring(0,80));
  if (ev.event_kind === 'run_cancelled') return 'Run #' + (p.run_id||'') + ' cancelled';
  if (ev.event_kind === 'section_change') return esc(p.before||'?') + ' → ' + esc(p.after||'?');
  if (ev.event_kind === 'status_change') return esc(p.before||'?') + ' → ' + esc(p.after||'?');
  if (ev.event_kind === 'criteria_check') return 'criterion ' + (p.after ? 'checked' : 'unchecked');
  if (ev.event_kind === 'criteria_added') return '+ ' + esc(p.text||'criterion');
  if (ev.event_kind === 'field_changed') return esc(p.field||'?') + ' changed';
  if (ev.event_kind === 'handoff_recorded') return 'Handoff for run #' + (p.run_id||'');
  if (ev.event_kind === 'input_provided') return 'User responded (' + esc(p.kind||'text') + ')';
  if (ev.event_kind === 'agent_output') return esc((p.summary||'').substring(0,100));
  if (ev.event_kind === 'workspace_created') return esc(p.path||'?');
  if (ev.event_kind === 'pause_set') return 'paused — ' + esc(p.reason||'');
  if (ev.event_kind === 'pause_cleared') return 'resumed';
  if (ev.event_kind === 'ticket_created') {{
    var origin = p.origin || 'unknown';
    var msg;
    if (origin === 'seed') msg = 'imported from ' + esc(p.source_file || 'PRODUCT_BACKLOG.md');
    else if (origin === 'markdown_edit') msg = 'detected in ' + esc(p.source_file || 'PRODUCT_BACKLOG.md');
    else if (origin === 'seek') msg = 'discovered by Seek in ' + esc((p.source_file || '?') + ':' + (p.source_line || '?'));
    else if (origin === 'agent') msg = 'created by agent';
    else if (origin === 'human') msg = 'created by user';
    else if (origin === 'journey_gap') msg = 'opened from journey ' + esc(p.linked_journey || '?') + ' gap (run #' + esc(String(p.from_gap_run_id || '?')) + ')';
    else if (origin === 'backfill') msg = 'ticket existed before activity tracking (origin unknown)';
    else msg = 'origin: ' + esc(origin);
    if (p.draft) msg += ' (draft)';
    return msg;
  }}
  return '';
}}

var _RUN_LINK_KINDS = {{'run_started':1,'run_succeeded':1,'run_failed':1,'run_cancelled':1,'handoff_recorded':1,'agent_output':1}};

function _renderActivityRow(ev) {{
  var group = EVENT_KIND_GROUPS[ev.event_kind] || 'Other';
  var color = EVENT_GROUP_COLORS[group] || '#94a3b8';
  var summary = _eventSummary(ev);
  // Prefer the human-readable agent/workflow name when present; falls back to
  // the bare role label for human/system events.
  var actor;
  if (ev.actor_type === 'agent') actor = ev.actor_name || 'agent';
  else if (ev.actor_type === 'system') actor = 'system';
  else actor = 'human';
  var absT = _formatAbsoluteTime(ev.occurred_at);
  var relT = relativeTime(ev.occurred_at);
  var kind = esc(ev.event_kind);
  var expanded = _activityFilters.expandedIds.has(ev.id);

  var rowCls = 'tp-act-row' + (expanded ? ' expanded' : '');
  var html = '<div class="' + rowCls + '" data-event-id="' + esc(String(ev.id)) +
             '" data-group="' + esc(group) + '">' +
    '<div class="tp-act-time"><span class="tp-act-time-abs">' + esc(absT) + '</span>' +
      '<span class="tp-act-time-rel">' + esc(relT) + '</span></div>' +
    '<div><span class="tp-act-badge" style="--badge-color:' + color + '">' + esc(group) + '</span></div>' +
    '<div class="tp-act-actor' + (ev.actor_type === 'agent' && ev.actor_name ? ' is-agent' : '') +
      '" title="' + esc(actor) + (ev.actor_id ? ' (run #' + esc(String(ev.actor_id)) + ')' : '') + '">' +
      esc(actor) + '</div>' +
    '<div class="tp-act-summary">' + (summary || '<span style="opacity:0.5">(no detail)</span>') +
      '<span class="tp-act-kind">' + kind + '</span></div>' +
    '<div class="tp-act-chevron">▸</div>';
  if (expanded) {{
    var runLinkHtml = '';
    if (_RUN_LINK_KINDS[ev.event_kind] && ev.run_id) {{
      runLinkHtml = '<a class="tp-act-detail-link" href="/{pid}/tickets/{
        tid
    }?tab=runs"' +
        ' onclick="sessionStorage.setItem(&apos;tp-select-run&apos;,&apos;' + ev.run_id + '&apos;)">' +
        'View run #' + esc(String(ev.run_id)) + ' →</a>';
    }}
    var payloadJson = '';
    try {{ payloadJson = JSON.stringify(ev.payload || {{}}, null, 2); }} catch(e) {{ payloadJson = String(ev.payload); }}
    html += '<div class="tp-act-detail">' +
      '<div class="tp-act-detail-header">' +
        '<span>event: <strong>' + kind + '</strong></span>' +
        '<span>actor: ' + esc(actor) +
          (ev.actor_type === 'agent' && ev.actor_name ? ' (' + esc(ev.actor_type) + ')' : '') +
          (ev.actor_id ? ' run #' + esc(String(ev.actor_id)) : '') + '</span>' +
        '<span>at: ' + esc(ev.occurred_at) + '</span>' +
        runLinkHtml +
      '</div>' +
      '<pre class="tp-act-detail-payload">' + esc(payloadJson) + '</pre>' +
    '</div>';
  }}
  html += '</div>';
  return html;
}}

function _applyActivityFilters(events) {{
  var now = Date.now();
  var maxAge = _RANGE_MS[_activityFilters.range];
  var excluded = _activityFilters.excludedGroups;
  return events.filter(function(ev) {{
    var group = EVENT_KIND_GROUPS[ev.event_kind] || 'Other';
    if (excluded.has(group)) return false;
    if (maxAge != null) {{
      var d = new Date(ev.occurred_at).getTime();
      if (isNaN(d) || (now - d) > maxAge) return false;
    }}
    return true;
  }});
}}

function _renderActivityFeed() {{
  if (!activityFeed) return;
  var filtered = _applyActivityFilters(_activityEvents);
  var statusEl = document.getElementById('tp-act-status');
  if (statusEl) {{
    statusEl.textContent = filtered.length + ' of ' + _activityEvents.length + ' events';
  }}
  if (filtered.length === 0) {{
    activityFeed.innerHTML = '<div class="tp-act-empty">' +
      (_activityEvents.length === 0 ? 'No activity yet.' : 'No events match the current filters.') +
      '</div>';
  }} else {{
    activityFeed.innerHTML = filtered.map(_renderActivityRow).join('');
  }}
}}

function loadActivity() {{
  if (!activityFeed) return;
  fetch(TP_API_BASE + '/tickets/' + encodeURIComponent(TP_TICKET_ID) + '/activity?limit=50')
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      _activityEvents = data.events || [];
      _renderActivityFeed();
      _checkNeedsInputRun();
    }})
    .catch(function() {{ /* ignore */ }});
}}

// ── Activity filter wiring ────────────────────────────────────────
(function() {{
  var typeChipRow = document.getElementById('tp-act-type-chips');
  var rangeChipRow = document.getElementById('tp-act-range-chips');
  var toggleAllBtn = document.getElementById('tp-act-toggle-all');
  if (!typeChipRow || !rangeChipRow) return;

  // Type chip clicks toggle exclusion membership for that group.
  typeChipRow.addEventListener('click', function(e) {{
    var btn = e.target.closest('.tp-act-chip');
    if (!btn) return;
    var grp = btn.dataset.group;
    if (_activityFilters.excludedGroups.has(grp)) {{
      _activityFilters.excludedGroups.delete(grp);
      btn.classList.add('active');
    }} else {{
      _activityFilters.excludedGroups.add(grp);
      btn.classList.remove('active');
    }}
    _renderActivityFeed();
  }});

  // "all" link: if anything is excluded, include everything; otherwise exclude all.
  if (toggleAllBtn) toggleAllBtn.addEventListener('click', function() {{
    var chips = typeChipRow.querySelectorAll('.tp-act-chip');
    if (_activityFilters.excludedGroups.size === 0) {{
      // Currently all on → turn all off
      chips.forEach(function(c) {{
        _activityFilters.excludedGroups.add(c.dataset.group);
        c.classList.remove('active');
      }});
    }} else {{
      _activityFilters.excludedGroups.clear();
      chips.forEach(function(c) {{ c.classList.add('active'); }});
    }}
    _renderActivityFeed();
  }});

  // Range chip clicks: single-select.
  rangeChipRow.addEventListener('click', function(e) {{
    var btn = e.target.closest('.tp-act-range');
    if (!btn) return;
    rangeChipRow.querySelectorAll('.tp-act-range').forEach(function(b) {{ b.classList.remove('active'); }});
    btn.classList.add('active');
    _activityFilters.range = btn.dataset.range;
    _renderActivityFeed();
  }});

  // Row click expands/collapses detail. Using event delegation on the feed.
  if (activityFeed) {{
    activityFeed.addEventListener('click', function(e) {{
      // Don't toggle when clicking inside the detail panel itself (e.g. View run link)
      if (e.target.closest('.tp-act-detail')) return;
      var row = e.target.closest('.tp-act-row');
      if (!row) return;
      var id = parseInt(row.dataset.eventId, 10);
      if (isNaN(id)) return;
      if (_activityFilters.expandedIds.has(id)) _activityFilters.expandedIds.delete(id);
      else _activityFilters.expandedIds.add(id);
      _renderActivityFeed();
    }});
  }}
}})();
function startActivityPoll() {{
  if (!activityFeed) return;
  loadActivity();
  activityPollTimer = setInterval(function() {{
    if (document.hasFocus()) loadActivity();
  }}, 5000);
}}
function stopActivityPoll() {{
  if (activityPollTimer) {{ clearInterval(activityPollTimer); activityPollTimer = null; }}
}}

// ── Needs-input panel ─────────────────────────────────────────────
var niPanel = document.getElementById('tp-ni-panel');
function _checkNeedsInputRun() {{
  fetch(TP_API_BASE + '/runs?ticket=' + encodeURIComponent(TP_TICKET_ID) + '&limit=1')
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      var runs = data.runs || [];
      if (!runs.length || runs[0].status !== 'needs_input') {{
        if (niPanel) niPanel.classList.add('hidden');
        return;
      }}
      var run = runs[0];
      _renderNiPanel(run);
    }}).catch(function() {{}});
}}
function _renderNiPanel(run) {{
  if (!niPanel) return;
  var kind = run.needs_input_kind || 'text';
  var promptText = '';
  try {{ var p = JSON.parse(run.needs_input_prompt || '{{}}');
    promptText = p.ask || JSON.stringify(p.propose || p, null, 2); }} catch(e) {{}}
  niPanel.classList.remove('hidden', 'tp-ni-propose');
  if (kind === 'propose') niPanel.classList.add('tp-ni-propose');

  if (kind === 'text') {{
    niPanel.innerHTML =
      '<div class="tp-ni-header">Agent is asking for input</div>' +
      '<div class="tp-ni-prompt" id="tp-ni-prompt-text">' + esc(promptText) + '</div>' +
      '<textarea class="tp-ni-textarea" id="tp-ni-text-reply" data-testid="tp-ni-text-reply" placeholder="Your reply..." rows="4"></textarea>' +
      '<div class="tp-ni-actions">' +
        '<button class="btn btn-primary" id="tp-ni-send" data-testid="tp-ni-send" data-run-id="' + run.id + '" data-kind="text">Send</button>' +
        '<button class="btn btn-ghost" id="tp-ni-cancel">Cancel</button>' +
      '</div>';
  }} else {{
    // Propose kind — build merge UI.
    var proposeData = {{}};
    try {{ proposeData = JSON.parse(run.needs_input_prompt || '{{}}').propose || {{}}; }} catch(e) {{}}
    var items = '';
    if (proposeData.description) {{
      items += '<li class="tp-propose-item"><input type="checkbox" name="tp-propose-desc" checked> <span class="tp-propose-label">Description</span><span>' + esc(proposeData.description) + '</span></li>';
    }}
    (proposeData.add_criteria || []).forEach(function(c, i) {{
      items += '<li class="tp-propose-item"><input type="checkbox" name="tp-propose-add-crit" data-idx="'+i+'" checked> <span class="tp-propose-label">+ Criterion</span><span>' + esc(c) + '</span></li>';
    }});
    (proposeData.remove_criteria || []).forEach(function(c, i) {{
      items += '<li class="tp-propose-item"><input type="checkbox" name="tp-propose-rm-crit" data-idx="'+i+'" checked> <span class="tp-propose-label">- Criterion</span><span>' + esc(c) + '</span></li>';
    }});
    (proposeData.add_tags || []).forEach(function(t, i) {{
      items += '<li class="tp-propose-item"><input type="checkbox" name="tp-propose-add-tag" data-idx="'+i+'" checked> <span class="tp-propose-label">+ Tag</span><span>' + esc(t) + '</span></li>';
    }});
    (proposeData.remove_tags || []).forEach(function(t, i) {{
      items += '<li class="tp-propose-item"><input type="checkbox" name="tp-propose-rm-tag" data-idx="'+i+'" checked> <span class="tp-propose-label">- Tag</span><span>' + esc(t) + '</span></li>';
    }});
    niPanel.innerHTML =
      '<div class="tp-ni-header">Agent is proposing changes</div>' +
      '<ul class="tp-propose-items" id="tp-propose-items">' + items + '</ul>' +
      '<div class="tp-ni-actions">' +
        '<button class="btn btn-primary" id="tp-ni-send" data-testid="tp-ni-send" data-run-id="' + run.id + '" data-kind="propose" data-propose=&apos;' + JSON.stringify(proposeData).replace(/'/g,"&#39;") + '&apos;>Apply selected</button>' +
        '<button class="btn btn-ghost" id="tp-ni-reject-all">Reject all</button>' +
      '</div>';
    var rejectAll = document.getElementById('tp-ni-reject-all');
    if (rejectAll) {{
      rejectAll.addEventListener('click', function() {{
        _sendNiResponse(run.id, 'propose', {{}}, proposeData);
      }});
    }}
  }}

  var sendBtn = document.getElementById('tp-ni-send');
  if (sendBtn) {{
    sendBtn.addEventListener('click', function() {{
      var runId = parseInt(sendBtn.dataset.runId, 10);
      var k = sendBtn.dataset.kind;
      if (k === 'text') {{
        var reply = (document.getElementById('tp-ni-text-reply') || {{}}).value || '';
        if (!reply.trim()) return;
        _sendNiResponse(runId, 'text', reply, null);
      }} else {{
        // Collect checked items.
        var proposeData = JSON.parse(sendBtn.dataset.propose || '{{}}');
        var accepted = {{}};
        var descChk = document.querySelector('input[name="tp-propose-desc"]');
        if (descChk && descChk.checked && proposeData.description) accepted.description = proposeData.description;
        var addCritChecked = Array.from(document.querySelectorAll('input[name="tp-propose-add-crit"]:checked'))
          .map(function(el) {{ return proposeData.add_criteria[parseInt(el.dataset.idx,10)]; }}).filter(Boolean);
        if (addCritChecked.length) accepted.add_criteria = addCritChecked;
        var rmCritChecked = Array.from(document.querySelectorAll('input[name="tp-propose-rm-crit"]:checked'))
          .map(function(el) {{ return proposeData.remove_criteria[parseInt(el.dataset.idx,10)]; }}).filter(Boolean);
        if (rmCritChecked.length) accepted.remove_criteria = rmCritChecked;
        var addTagChecked = Array.from(document.querySelectorAll('input[name="tp-propose-add-tag"]:checked'))
          .map(function(el) {{ return proposeData.add_tags[parseInt(el.dataset.idx,10)]; }}).filter(Boolean);
        if (addTagChecked.length) accepted.add_tags = addTagChecked;
        var rmTagChecked = Array.from(document.querySelectorAll('input[name="tp-propose-rm-tag"]:checked'))
          .map(function(el) {{ return proposeData.remove_tags[parseInt(el.dataset.idx,10)]; }}).filter(Boolean);
        if (rmTagChecked.length) accepted.remove_tags = rmTagChecked;
        _sendNiResponse(runId, 'propose', accepted, proposeData);
      }}
    }});
  }}
  var cancelBtn = document.getElementById('tp-ni-cancel');
  if (cancelBtn) {{ cancelBtn.addEventListener('click', function() {{ niPanel.classList.add('hidden'); }}); }}
}}

function _sendNiResponse(runId, kind, responseOrAccepted, proposeData) {{
  var body = kind === 'text' ?
    {{kind: 'text', response: responseOrAccepted}} :
    {{kind: 'propose', accepted: responseOrAccepted}};
  fetch(TP_API_BASE + '/runs/' + runId + '/respond', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body),
  }}).then(function() {{
    if (niPanel) niPanel.classList.add('hidden');
    loadActivity();
  }}).catch(console.error);
}}

// ── Runs list ─────────────────────────────────────────────────────
var runsListEl = document.getElementById('tp-runs-list');
var selectedRunId = null;
function loadRunsList() {{
  if (!runsListEl) return;
  fetch(TP_API_BASE + '/runs?ticket=' + encodeURIComponent(TP_TICKET_ID) + '&limit=20')
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      var runs = data.runs || [];
      if (!runs.length) {{
        runsListEl.innerHTML = '<div class="tp-empty tp-activity-loading">No runs yet.</div>';
        return;
      }}
      runsListEl.innerHTML = runs.map(function(r) {{
        var cls = 'tp-run-row' + (r.id === selectedRunId ? ' active' : '');
        var statusColor = r.status === 'succeeded' ? 'var(--green)' :
          r.status === 'failed' ? 'var(--red)' : 'var(--text-secondary)';
        return '<div class="' + cls + '" data-run-id="' + r.id + '">' +
          '<span style="color:' + statusColor + '">' + esc(r.status) + '</span> ' +
          '<span style="color:var(--text-secondary);font-size:11px;">#' + r.id + '</span>' +
          '<div style="color:var(--text-tertiary);font-size:11px;margin-top:2px;">' +
            esc((r.summary||'').substring(0,60)) + '</div>' +
          '</div>';
      }}).join('');
      // Auto-select from sessionStorage (set by activity "View run" link).
      var autoSelect = sessionStorage.getItem('tp-select-run');
      if (autoSelect) {{ sessionStorage.removeItem('tp-select-run'); selectRun(parseInt(autoSelect, 10)); }}
      else if (!selectedRunId && runs.length) selectRun(runs[0].id);
    }}).catch(function() {{}});
}}
function selectRun(runId) {{
  selectedRunId = runId;
  // Update active state.
  document.querySelectorAll('.tp-run-row').forEach(function(el) {{
    el.classList.toggle('active', parseInt(el.dataset.runId, 10) === runId);
  }});
  fetch(TP_API_BASE + '/runs/' + runId)
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      renderRunDetail(data.run || data, data.events || [], TP_API_BASE);
    }}).catch(function() {{}});
}}
document.addEventListener('click', function(e) {{
  var row = e.target.closest('.tp-run-row');
  if (row && row.dataset.runId) {{ selectRun(parseInt(row.dataset.runId, 10)); }}
}});
// ── Duration formatter (shared with Kitchen) ──────────────────────
function _fmtDuration(ms) {{
  if (!ms) return '';
  var s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  var m = Math.floor(s / 60); var rs = s % 60;
  if (m < 60) return m + 'm ' + rs + 's';
  var h = Math.floor(m / 60); var rm = m % 60;
  return h + 'h ' + rm + 'm';
}}
{_render_run_detail_js_fn("TP_API_BASE")}
if (runsListEl) loadRunsList();

// ── Files tab ─────────────────────────────────────────────────────
var filesListEl = document.getElementById('tp-attachments-list');
if (filesListEl) {{
  fetch(TP_API_BASE + '/tickets/' + encodeURIComponent(TP_TICKET_ID) + '/attachments')
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      var atts = data.attachments || [];
      if (!atts.length) {{
        filesListEl.innerHTML = '<div class="tp-empty">No attachments yet.</div>';
      }} else {{
        filesListEl.innerHTML = atts.map(function(a) {{
          return '<div style="padding:6px;font-size:12px;">' + esc(a.filename||a.id) + '</div>';
        }}).join('');
      }}
    }}).catch(function() {{ filesListEl.innerHTML = '<div class="tp-empty">Attachments unavailable.</div>'; }});
}}

// ── Start activity poll if on activity tab ────────────────────────
if (activityFeed) startActivityPoll();

// ── Ask AI button ─────────────────────────────────────────────────
document.addEventListener('click', function(e) {{
  var btn = e.target.closest('.tp-crit-ask-ai');
  if (!btn) return;
  var idx = parseInt(btn.dataset.index, 10);
  var li = btn.closest('.tp-criterion');
  var text = li ? (li.querySelector('.tp-crit-text') || {{}}).textContent || '' : '';
  var prompt = 'Help me satisfy this acceptance criterion for ticket ' +
    TP_TICKET_ID + ': ' + text;
  navigator.clipboard.writeText(prompt).catch(function() {{}});
  btn.textContent = 'Copied';
  setTimeout(function() {{ btn.textContent = 'Ask AI'; }}, 1500);
}});
</script>
<script>{rail_js}</script>
<script>{drawer_js}</script>
</body>
</html>'''


def _aggregate_kitchen_state() -> dict:
    """Aggregate Kitchen state across all registered projects.

    Returns the shape consumed by /api/kitchen and _render_kitchen_view:

      {
        "buckets": {
          "needs_me": [...], "running": [...], "ready_to_delegate": [...],
          "paused": [...], "failed": [...],
        },
        "projects": [{id, name, counts: {wip, review, blocked, running, needs_me}}, ...],
      }

    Each item is {project_id, project_name, ticket_id, title, section, status,
                  automation_mode, latest_run_status, pause_reason, eligibility_reasons?}.
    Subjects appear in at most one bucket, with priority needs_me > running >
    ready_to_delegate > paused > failed.
    """
    from actions import eligibility as _elig

    with _PROJECTS_CACHE_LOCK:
        # Skip projects with watched=false. Default (missing key) is true so
        # the aggregator includes everything by default.
        projects = [p for p in _PROJECTS_CACHE.values() if p.get("watched", True)]

    buckets = {
        k: [] for k in ("needs_me", "running", "ready_to_delegate", "paused", "failed")
    }
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
                    "SELECT automation_mode, pause_reason FROM automation_subjects "
                    "WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ?",
                    (pid, tid),
                ).fetchone()
                mode = am_row["automation_mode"] if am_row else "manual"
                pause_reason = am_row["pause_reason"] if am_row else None

                latest = conn.execute(
                    "SELECT id, status, duration_ms, exit_code, metadata_json "
                    "FROM runs WHERE project_id = ? AND subject_type='ticket' AND subject_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (pid, tid),
                ).fetchone()
                run_status = latest["status"] if latest else None
                run_id = latest["id"] if latest else None
                run_duration_ms = latest["duration_ms"] if latest else None
                run_exit_code = latest["exit_code"] if latest else None
                # Extract agent/workflow name from metadata_json.
                run_agent_name = None
                if latest and latest["metadata_json"]:
                    try:
                        _rm = json.loads(latest["metadata_json"])
                        run_agent_name = _rm.get("workflow_name")
                    except Exception:
                        pass

                if run_status in ("preparing", "running"):
                    counts["running"] += 1
                if run_status == "needs_input":
                    counts["needs_me"] += 1

                base_item = {
                    "project_id": pid,
                    "project_name": pname,
                    "ticket_id": tid,
                    "title": t["title"],
                    "section": t["section"],
                    "status": t["status"],
                    "automation_mode": mode,
                    "latest_run_status": run_status,
                    "pause_reason": pause_reason,
                    "run_id": run_id,
                    "agent_name": run_agent_name,
                    "duration_ms": run_duration_ms,
                    "exit_code": run_exit_code,
                }

                # Bucket assignment with single-priority placement.
                if run_status == "needs_input":
                    buckets["needs_me"].append(base_item)
                elif run_status in ("preparing", "running"):
                    buckets["running"].append(base_item)
                elif run_status in ("failed", "stalled"):
                    buckets["failed"].append(base_item)
                elif mode == "paused":
                    buckets["paused"].append(base_item)
                elif mode == "auto":
                    # Eligibility check — only show in Ready To Delegate when
                    # there's no active run AND DCSTL gates pass.
                    try:
                        er = _elig(conn, pid, "ticket", tid)
                        if er.eligible:
                            buckets["ready_to_delegate"].append(base_item)
                    except Exception:
                        pass

            project_summaries.append(
                {
                    "id": pid,
                    "name": pname,
                    "path": proj.get("path", ""),
                    "counts": counts,
                }
            )

        conn.close()

    # Sort each bucket: most-recent activity first by ticket id desc
    # (proxy for recency without joining timestamps; cheap, deterministic).
    for k in buckets:
        buckets[k].sort(key=lambda x: (x["project_id"], x["ticket_id"]))

    return {"buckets": buckets, "projects": project_summaries}


def _render_kitchen_view(port: int) -> str:
    """Render the Kitchen attention feed — cross-project work surface.

    Delegates to ``kitchen_feed.build_attention_feed`` for the payload and
    ``kitchen_view.render_attention_feed`` for the HTML. Both modules are
    new, self-contained, and have no awareness of serve.py state — we pass
    in the project list, pause flag, and the rail JS / PWA head tags here.
    """
    with _PROJECTS_CACHE_LOCK:
        projects = list(_PROJECTS_CACHE.values())
    with _db_lock:
        conn = get_db()
        init_db(conn)
        state = kitchen_feed.build_attention_feed(
            conn,
            projects,
            is_paused=_kitchen.is_paused(),
        )
        conn.close()
    return kitchen_view.render_attention_feed(
        state,
        port=port,
        rail_css=gen.build_nav_rail_css(),
        rail_html=gen.build_nav_rail_html(),
        rail_js=gen.build_nav_rail_js(),
        pwa_head_tags=PWA_HEAD_TAGS,
    )


def _aggregate_workflows_state() -> dict:
    """Return one row per workflow with the projects it's linked to.

    Post-migration-16 model: workflows are canonical singletons. Each row
    carries a `links` list — one entry per project the workflow applies to,
    with that project's individual enabled flag.
    """
    with _PROJECTS_CACHE_LOCK:
        projects = list(_PROJECTS_CACHE.values())
    proj_name_by_id = {p["id"]: p.get("name", p["id"]) for p in projects}

    workflows: list[dict] = []
    with _db_lock:
        conn = get_db()
        init_db(conn)
        rows = conn.execute(
            "SELECT * FROM workflows ORDER BY system DESC, name"
        ).fetchall()
        run_count_rows = conn.execute(
            "SELECT workflow_id, COUNT(*) AS cnt FROM workflow_runs GROUP BY workflow_id"
        ).fetchall()
        link_rows = conn.execute(
            "SELECT workflow_id, project_id, enabled FROM workflow_projects ORDER BY project_id"
        ).fetchall()
        conn.close()
    runs_by_wf = {r["workflow_id"]: r["cnt"] for r in run_count_rows}
    links_by_wf: dict[str, list[dict]] = {}
    for lr in link_rows:
        links_by_wf.setdefault(lr["workflow_id"], []).append(
            {
                "project_id": lr["project_id"],
                "project_name": proj_name_by_id.get(lr["project_id"], lr["project_id"]),
                "enabled": int(lr["enabled"]),
            }
        )

    total_projects = len(projects)
    for row in rows:
        d = _serialize_workflow(dict(row))
        d["scope"] = "system" if d.get("system") else "user"
        d["scope_label"] = "System" if d.get("system") else "User"
        d["links"] = links_by_wf.get(d["id"], [])
        d["link_count"] = len(d["links"])
        d["enabled_link_count"] = sum(1 for l in d["links"] if l["enabled"])
        d["applies_to_all"] = d["link_count"] == total_projects and total_projects > 0
        d["run_count"] = runs_by_wf.get(d["id"], 0)
        d["step_count"] = len(d.get("steps") or [])
        workflows.append(d)

    return {
        "workflows": workflows,
        "projects": [{"id": p["id"], "name": p.get("name", p["id"])} for p in projects],
    }


def _render_workflows_view(port: int) -> str:
    """Render the global Workflows page — three tabs (Workflows | Agents | Endpoints) with inline edit."""
    rail_css = gen.build_nav_rail_css()
    rail_html = gen.build_nav_rail_html()
    rail_js = gen.build_nav_rail_js()
    drawer_css = gen.build_settings_drawer_css()
    drawer_html = gen.build_settings_drawer_html(gen._svg_icon("x", 14))
    drawer_js = gen.build_settings_drawer_js()

    state = _aggregate_workflows_state()
    workflows = state["workflows"]
    projects = state["projects"]
    projects_meta_json = json.dumps(projects)
    agents = _list_workflow_agents()

    from endpoints import list_endpoints as _list_endpoints

    _ep_conn = get_db()
    endpoints = _list_endpoints(_ep_conn)
    _ep_conn.close()

    def _scope_badge(scope: str, label: str) -> str:
        cls = {"system": "wf-scope-system", "user": "wf-scope-user"}.get(
            scope, "wf-scope-user"
        )
        return f'<span class="wf-scope-badge {cls}">{_html.escape(label)}</span>'

    def _applies_to_html(wf: dict) -> str:
        links = wf.get("links") or []
        if not links:
            return '<span class="wf-applies-empty">no projects</span>'
        if wf.get("applies_to_all"):
            return f'<span class="wf-applies-all">All {len(links)} projects</span>'
        pills = []
        for l in links[:3]:
            cls = "wf-applies-pill" + ("" if l["enabled"] else " disabled")
            title = "enabled" if l["enabled"] else "disabled in this project"
            pills.append(
                f'<span class="{cls}" title="{title}">{_html.escape(l["project_name"])}</span>'
            )
        if len(links) > 3:
            pills.append(f'<span class="wf-applies-more">+{len(links) - 3}</span>')
        return "".join(pills)

    # Resolve which project paths are actually present on this machine — used to
    # pick a "healthy" target for the per-workflow advanced editor link. Without
    # this, workflows linked to macOS-only projects on a WSL session (or vice
    # versa) send the user to a kanban whose dashboard doesn't exist and 404s.
    with _PROJECTS_CACHE_LOCK:
        cache_snapshot = list(_PROJECTS_CACHE.values())
    healthy_pids = {
        p["id"]
        for p in cache_snapshot
        if p.get("path") and Path(os.path.expanduser(p["path"])).is_dir()
    }

    def _pick_manage_target(wf: dict) -> str:
        link_pids = [l["project_id"] for l in (wf.get("links") or [])]
        for pid in link_pids:
            if pid in healthy_pids:
                return pid
        if link_pids:
            return link_pids[0]
        for p in projects:
            if p["id"] in healthy_pids:
                return p["id"]
        return projects[0]["id"] if projects else ""

    # Trigger / on_success sentence translator — used to render plain English
    # under each workflow row so users can tell at a glance what fires it.
    from trigger_describe import (
        describe_on_success,
        describe_trigger,
    )

    def _build_row_html(wf: dict) -> str:
        applies_html = _applies_to_html(wf)
        target_pid = _pick_manage_target(wf)
        advanced_href = (
            f"/{_safe_attr(target_pid)}/kanban?bounce=1" if target_pid else ""
        )
        linked_pids = ",".join(l["project_id"] for l in (wf.get("links") or []))
        wf_id = wf["id"]
        wf_id_attr = _safe_attr(wf_id)
        is_system = bool(wf.get("system"))
        enabled_checked = " checked" if int(wf.get("enabled") or 0) else ""
        steps_list = wf.get("steps") or []
        step_count = len(steps_list)
        run_count = wf.get("run_count", 0)
        step_label = f"{step_count} step{'s' if step_count != 1 else ''}"
        run_label = f"{run_count} run{'s' if run_count != 1 else ''}"

        trigger_sentence = describe_trigger(wf.get("trigger_json"))
        effect_sentence = describe_on_success(wf.get("on_success_json"))
        is_manual = trigger_sentence.startswith("Manual run only")

        if steps_list:
            steps_summary = "".join(
                f"<li>{i + 1}. {_html.escape(str((s or {}).get('agent') or (s or {}).get('agent_id') or (s or {}).get('name') or 'step'))}</li>"
                for i, s in enumerate(steps_list)
            )
        else:
            # Zero-step workflows are pure rules — the trigger + effect ARE
            # the logic. Make that explicit so the panel doesn't look empty.
            steps_summary = '<li class="wf-edit-empty">Pure rule — no agent step. Logic lives entirely in Trigger and Effects below.</li>'

        sys_note = (
            '<div class="wf-edit-note">'
            "System workflow — body is read-only. You can toggle Enabled "
            "or click <strong>Duplicate</strong> to create an editable copy "
            "in one of your projects."
            "</div>"
            if is_system
            else ""
        )
        del_btn = (
            '<button class="wf-edit-delete" disabled title="System workflows can\'t be deleted; duplicate to create an editable copy.">Delete</button>'
            if is_system
            else f'<button class="wf-edit-delete" data-id="{wf_id_attr}">Delete</button>'
        )
        dup_btn = (
            f'<button class="wf-edit-duplicate" data-id="{wf_id_attr}">Duplicate</button>'
            if is_system
            else ""
        )
        (
            ""
            if is_system
            else (
                f'<a class="wf-edit-advanced" href="{_html.escape(advanced_href)}">Open in project to edit steps →</a>'
                if advanced_href
                else ""
            )
        )

        # Live-match badge: hidden by default; JS fetches /preview and fills.
        # Manual workflows show a static "manual" tag instead so the row
        # doesn't promise auto-fire counts that won't materialise.
        if is_manual:
            match_badge = '<span class="wf-match wf-match-manual" title="Manual run only">manual</span>'
        else:
            match_badge = f'<span class="wf-match wf-match-loading" data-wf-match="{wf_id_attr}" title="Live count of tickets that match this trigger right now">…</span>'

        # Trigger and effect lines — primary readable content of the row.
        trigger_html = f'<div class="wf-trigger">{_html.escape(trigger_sentence)}</div>'
        effect_html = (
            f'<div class="wf-effect">{_html.escape(effect_sentence)}</div>'
            if effect_sentence
            else ""
        )

        # Meta strip: enabled count, steps, runs, applies-to.
        meta_parts = [
            f'<span class="wf-meta-item">{step_label}</span>',
            f'<span class="wf-meta-item">{run_label}</span>',
            f'<span class="wf-meta-item">{wf["enabled_link_count"]}/{wf["link_count"]} projects on</span>',
        ]
        meta_strip = (
            '<div class="wf-meta">'
            + " · ".join(meta_parts)
            + f'  <span class="wf-applies">{applies_html}</span></div>'
        )

        return (
            f'<div class="wf-row-wrap" data-scope="{wf["scope"]}" data-projects="{_safe_attr(linked_pids)}" data-wf-id="{wf_id_attr}" data-system="{1 if is_system else 0}" data-testid="wf-row-{wf_id_attr}" data-trigger-json="{_safe_attr(json.dumps(wf.get("trigger_json") or {}))}" data-on-success-json="{_safe_attr(json.dumps(wf.get("on_success_json") or {}))}">'
            f'  <div class="wf-row">'
            f'    <div class="wf-main">'
            f'      <div class="wf-name-line">'
            f'        <span class="wf-name">{_html.escape(wf.get("name", "Unnamed"))}</span>'
            f"        {match_badge}"
            f"      </div>"
            f"      {trigger_html}"
            f"      {effect_html}"
            f"      {meta_strip}"
            f"    </div>"
            f'    <div class="wf-cell wf-actions">'
            f'      <label class="wf-edit-switch wf-row-switch" title="Enable or disable across all linked projects"><input type="checkbox" data-field="enabled"{enabled_checked} data-wf-toggle="{wf_id_attr}"><span class="wf-edit-slider"></span></label>'
            f'      <button class="wf-edit-toggle" data-id="{wf_id_attr}">Edit</button>'
            f"    </div>"
            f"  </div>"
            f'  <div class="wf-edit-panel" data-id="{wf_id_attr}" hidden>'
            f"    {sys_note}"
            f'    <div class="wf-edit-row"><label>Name</label><input type="text" data-field="name" value="{_safe_attr(wf.get("name", ""))}"{" readonly" if is_system else ""}></div>'
            f'    <div class="wf-edit-row"><label>Description</label><textarea data-field="description" rows="2"{" readonly" if is_system else ""}>{_html.escape(wf.get("description", ""))}</textarea></div>'
            f'    <div class="wf-edit-row"><label>Enabled</label>'
            f'      <label class="wf-edit-switch"><input type="checkbox" data-field="enabled"{enabled_checked}><span class="wf-edit-slider"></span></label>'
            f"    </div>"
            f'    <div class="wf-edit-row wf-edit-rules" data-wf-rules-mount="{wf_id_attr}">'
            f"      <label>Rules</label>"
            f'      <div class="wf-rules-editor" data-wf-id="{wf_id_attr}" data-system="{1 if is_system else 0}">'
            f'        <div class="wf-rules-loading">Loading editor…</div>'
            f"      </div>"
            f"    </div>"
            f'    <div class="wf-edit-row"><label>Steps</label><ul class="wf-edit-steps">{steps_summary}</ul></div>'
            f'    <div class="wf-edit-actions">'
            f'      <button class="wf-edit-save" data-id="{wf_id_attr}">Save</button>'
            f"      {dup_btn}"
            f"      {del_btn}"
            f'      <span class="wf-edit-msg"></span>'
            f"    </div>"
            f"  </div>"
            f"</div>"
        )

    if not workflows:
        rows_html = '<div class="wf-empty">No workflows yet. Open a project and add one from the Workflows panel.</div>'
    else:
        # Group rows by scope. Scope is a *grouping* now, not a filter, so
        # there's no toolbar to hide rows — users see everything organised
        # under headers and use project filters to narrow further.
        sys_rows = [_build_row_html(wf) for wf in workflows if wf["scope"] == "system"]
        usr_rows = [_build_row_html(wf) for wf in workflows if wf["scope"] == "user"]

        groups = []
        if sys_rows:
            groups.append(
                '<div class="wf-group" data-scope="system">'
                f'  <h3 class="wf-group-header">System workflows <span class="wf-group-count">{len(sys_rows)}</span><span class="wf-group-hint">Built-in rules; body read-only, duplicate to customize</span></h3>'
                f'  <div class="wf-group-body">{"".join(sys_rows)}</div>'
                "</div>"
            )
        if usr_rows:
            groups.append(
                '<div class="wf-group" data-scope="user">'
                f'  <h3 class="wf-group-header">User workflows <span class="wf-group-count">{len(usr_rows)}</span><span class="wf-group-hint">Created by you; fully editable</span></h3>'
                f'  <div class="wf-group-body">{"".join(usr_rows)}</div>'
                "</div>"
            )
        rows_html = "".join(groups) or '<div class="wf-empty">No workflows yet.</div>'

    # Build the Agents tab rows. Custom agents only — discovered agents need a
    # project context; the global tab keeps it simple.
    cli_endpoints = [ep for ep in endpoints if ep.endpoint_type == "cli"]
    if not agents:
        agent_rows_html = '<div class="wf-empty">No custom agents yet. Click "+ New Agent" to create one.</div>'
    else:
        agent_parts = []
        for a in agents:
            aid = a["id"]
            aid_attr = _safe_attr(aid)
            aname = a.get("name") or aid
            a.get("command") or ""
            args_raw = a.get("args") or "[]"
            try:
                args_parsed = (
                    json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                )
                if isinstance(args_parsed, list):
                    ", ".join(str(x) for x in args_parsed)
                else:
                    str(args_raw)
            except (json.JSONDecodeError, TypeError):
                str(args_raw)
            sys_prompt = a.get("system_prompt") or ""
            ag_is_system = bool(a.get("system"))
            ag_ro = " readonly" if ag_is_system else ""
            ag_type_class = "ag-type-system" if ag_is_system else "ag-type-custom"
            ag_type_label = "system" if ag_is_system else "custom"
            ag_sys_note = (
                '<div class="wf-edit-note">'
                "System agent — body is read-only. The definition lives in "
                "<code>workflows_seed.py</code>; edit there and restart serve.py."
                "</div>"
                if ag_is_system
                else ""
            )
            ag_del_btn = (
                '<button class="ag-edit-delete" disabled title="System agents can\'t be deleted; edit workflows_seed.py to remove.">Delete</button>'
                if ag_is_system
                else f'<button class="ag-edit-delete" data-id="{aid_attr}">Delete</button>'
            )
            # Build endpoint dropdown — default shows CLI-only options
            current_ep_id = a.get("endpoint_id") or ""
            ep_options = '<option value=""></option>'
            for ep in cli_endpoints:
                sel = " selected" if ep.id == current_ep_id else ""
                ep_options += f'<option value="{_safe_attr(ep.id)}"{sel}>{_html.escape(ep.name)}</option>'
            # Endpoint dropdown is always editable — endpoint binding is orthogonal to agent identity.
            # System agent persona fields (name, system_prompt) stay locked via ag_ro above.
            endpoint_field = (
                f'<div class="wf-edit-row">'
                f"  <label>Endpoint</label>"
                f'  <select data-field="endpoint_id" class="agent-endpoint-select">'
                f"    {ep_options}"
                f"  </select>"
                f'  <label class="show-all-toggle" style="display:flex;align-items:center;gap:6px;margin-top:4px;font-size:12px;color:var(--text-secondary);cursor:pointer;">'
                f'    <input type="checkbox" class="show-all-endpoints">'
                f"    Show non-executable types"
                f"  </label>"
                f"</div>"
            )
            # Build ag-cmd summary: show endpoint name or a warning if none set.
            ep_lookup = {ep.id: ep.name for ep in cli_endpoints}
            if current_ep_id and current_ep_id in ep_lookup:
                ag_cmd_summary = ep_lookup[current_ep_id]
            else:
                ag_cmd_summary = "⚠ no endpoint"
            agent_parts.append(
                f'<div class="ag-row-wrap" data-agent-id="{aid_attr}" data-system="{1 if ag_is_system else 0}" data-testid="ag-row-{aid_attr}">'
                f'  <div class="ag-row">'
                f'    <div class="ag-main">'
                f'      <div class="ag-name">{_html.escape(aname)}</div>'
                f'      <div class="ag-cmd">{_html.escape(ag_cmd_summary)}</div>'
                f"    </div>"
                f'    <div class="ag-cell"><span class="ag-type {ag_type_class}">{ag_type_label}</span></div>'
                f'    <div class="ag-cell"><button class="ag-edit-toggle" data-id="{aid_attr}">Edit</button></div>'
                f"  </div>"
                f'  <div class="ag-edit-panel" data-id="{aid_attr}" hidden>'
                f"    {ag_sys_note}"
                f'    <div class="wf-edit-row"><label>Name</label><input type="text" data-field="name" value="{_safe_attr(aname)}"{ag_ro}></div>'
                f'    <div class="wf-edit-row"><label>System prompt</label><textarea data-field="system_prompt" rows="4"{ag_ro}>{_html.escape(sys_prompt)}</textarea></div>'
                f"    {endpoint_field}"
                f'    <div class="wf-edit-actions">'
                f'      <button class="ag-edit-save" data-id="{aid_attr}">Save</button>'
                f"      {ag_del_btn}"
                f'      <span class="wf-edit-msg"></span>'
                f"    </div>"
                f"  </div>"
                f"</div>"
            )
        agent_rows_html = "".join(agent_parts)

    # Build the Endpoints tab rows.
    _ENDPOINT_TYPES = ("cli", "anthropic_api", "openai_api", "gemini_api", "ssh_cli")
    if not endpoints:
        endpoint_rows_html = '<div class="wf-empty">No endpoints yet. Click "+ New Endpoint" to create one.</div>'
    else:
        ep_parts = []
        for ep in endpoints:
            eid = ep.id
            eid_attr = _safe_attr(eid)
            ep_is_system = bool(ep.system)
            ep_ro = " readonly" if ep_is_system else ""
            ep_sys_note = (
                '<div class="wf-edit-note">'
                "System endpoint — body is read-only. The definition lives in "
                "<code>workflows_seed.py</code>; edit there and restart serve.py."
                "</div>"
                if ep_is_system
                else ""
            )
            ep_del_btn = (
                '<button class="ep-edit-delete" disabled title="System endpoints can\'t be deleted.">Delete</button>'
                if ep_is_system
                else f'<button class="ep-edit-delete" data-id="{eid_attr}">Delete</button>'
            )
            type_options = "".join(
                f'<option value="{t}"{" selected" if t == ep.endpoint_type else ""}>{t}'
                + (" ⚠ not executable in phase 1" if t != "cli" else "")
                + "</option>"
                for t in _ENDPOINT_TYPES
            )
            mode_options = (
                f'<option value="template"{" selected" if ep.prompt_mode == "template" else ""}>template</option>'
                f'<option value="stdin"{" selected" if ep.prompt_mode == "stdin" else ""}>stdin (reserved)</option>'
            )
            ep_type_class = "ag-type-system" if ep_is_system else "ag-type-custom"
            ep_type_label = "system" if ep_is_system else "custom"
            ep_parts.append(
                f'<div class="ag-row-wrap" data-ep-id="{eid_attr}" data-system="{1 if ep_is_system else 0}" data-testid="ep-row-{eid_attr}">'
                f'  <div class="ag-row">'
                f'    <div class="ag-main">'
                f'      <div class="ag-name">{_html.escape(ep.name)}</div>'
                f'      <div class="ag-cmd">{_html.escape(ep.endpoint_type)}{(" · " + _html.escape(ep.command)) if ep.command else ""}</div>'
                f"    </div>"
                f'    <div class="ag-cell"><span class="ag-type {ep_type_class}">{ep_type_label}</span></div>'
                f'    <div class="ag-cell"><button class="ep-edit-toggle" data-id="{eid_attr}">Edit</button></div>'
                f"  </div>"
                f'  <div class="ep-edit-panel" data-id="{eid_attr}" hidden>'
                f"    {ep_sys_note}"
                f'    <div class="wf-edit-row"><label>Name</label><input type="text" data-field="name" value="{_safe_attr(ep.name)}"{ep_ro}></div>'
                f'    <div class="wf-edit-row"><label>Type</label><select data-field="endpoint_type"{ep_ro}>{type_options}</select></div>'
                f'    <div class="wf-edit-row"><label>Command</label><input type="text" data-field="command" value="{_safe_attr(ep.command or "")}"{ep_ro}></div>'
                f'    <div class="wf-edit-row"><label>Args</label><input type="text" data-field="args" value="{_safe_attr(json.dumps(ep.args))}" placeholder="JSON array of strings"{ep_ro}></div>'
                f'    <div class="wf-edit-row"><label>Prompt mode</label><select data-field="prompt_mode"{ep_ro}>{mode_options}</select></div>'
                f'    <div class="wf-edit-row"><label>Timeout (s)</label><input type="number" data-field="timeout_s" value="{ep.timeout_s}"{ep_ro}></div>'
                f'    <div class="wf-edit-actions">'
                f'      <button class="ag-edit-save ep-edit-save" data-id="{eid_attr}"{" disabled" if ep_is_system else ""}>Save</button>'
                f"      {ep_del_btn}"
                f'      <span class="wf-edit-msg"></span>'
                f"    </div>"
                f"  </div>"
                f"</div>"
            )
        endpoint_rows_html = "".join(ep_parts)

    return f"""<!doctype html>
<html data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Workflows — Ticket Takeaway</title>
{PWA_HEAD_TAGS}
<meta name="projects-list" content='{_safe_attr(projects_meta_json)}'>
<script>
(function () {{
  var t = localStorage.getItem('tt-theme') || 'system';
  var dark = t === 'dark' || (t === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
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
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg-page); color: var(--text-primary); font: 14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; }}
.wf-page {{ max-width: 1100px; margin: 0 auto; padding: 24px 20px 60px; }}
.wf-header {{ display: flex; align-items: center; gap: 16px; padding: 8px 20px; border-bottom: 1px solid var(--border-subtle); background: var(--bg-surface); }}
.wf-header h1 {{ margin: 0; font-size: 16px; font-weight: 600; }}
.wf-header-sub {{ color: var(--text-tertiary); font-size: 12px; }}
.wf-tabs {{ display: flex; gap: 4px; margin-bottom: 14px; border-bottom: 1px solid var(--border-subtle); }}
.wf-tab {{ background: none; border: none; border-bottom: 2px solid transparent; color: var(--text-secondary); padding: 8px 16px; font-size: 13px; font-weight: 600; cursor: pointer; font-family: inherit; }}
.wf-tab:hover {{ color: var(--text-primary); }}
.wf-tab.active {{ color: var(--text-primary); border-bottom-color: var(--accent); }}
.wf-tab-pane {{ display: none; }}
.wf-tab-pane.active {{ display: block; }}
.wf-pane-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; gap: 12px; }}
.wf-pane-header h2 {{ margin: 0; font-size: 13px; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; }}
.wf-toolbar {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; padding-top: 4px; }}
.wf-toolbar-projects {{ margin-bottom: 14px; }}
.wf-toolbar-label {{ font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-tertiary); margin-right: 4px; min-width: 60px; }}
.wf-filter, .wf-proj-filter {{ font-size: 11px; padding: 4px 10px; border-radius: 999px; border: 1px solid var(--border-subtle); background: var(--bg-surface); color: var(--text-secondary); cursor: pointer; font-family: inherit; }}
.wf-filter:hover, .wf-proj-filter:hover {{ border-color: var(--border-default); color: var(--text-primary); }}
.wf-filter.active {{ background: var(--accent); border-color: var(--accent); color: white; }}
.wf-proj-filter.active {{ background: rgba(34,197,94,0.18); border-color: rgba(34,197,94,0.45); color: #4ade80; }}
.wf-proj-filter[data-project=""].active {{ background: var(--bg-hover); border-color: var(--border-default); color: var(--text-primary); }}
.wf-list {{ display: flex; flex-direction: column; gap: 18px; }}
.wf-group {{ background: var(--bg-surface); border: 1px solid var(--border-subtle); border-radius: 8px; overflow: hidden; }}
.wf-group-header {{ margin: 0; padding: 10px 14px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px; color: var(--text-secondary); display: flex; align-items: center; gap: 10px; border-bottom: 1px solid var(--border-subtle); background: var(--bg-page); }}
.wf-group-count {{ display: inline-block; min-width: 22px; padding: 1px 7px; border-radius: 999px; background: var(--bg-card); color: var(--text-tertiary); font-size: 10px; font-weight: 600; text-align: center; letter-spacing: 0; }}
.wf-group-hint {{ margin-left: auto; font-weight: 400; text-transform: none; letter-spacing: 0; color: var(--text-tertiary); font-size: 11px; }}
.wf-group-body {{ display: flex; flex-direction: column; }}
.wf-row-wrap {{ border-bottom: 1px solid var(--border-subtle); }}
.wf-row-wrap:last-child {{ border-bottom: 0; }}
.wf-row {{ display: grid; grid-template-columns: 1fr auto; gap: 16px; padding: 12px 16px; align-items: flex-start; font-size: 13px; }}
.wf-row:hover {{ background: rgba(255,255,255,0.025); }}
[data-theme="light"] .wf-row:hover {{ background: rgba(0,0,0,0.02); }}
.wf-main {{ min-width: 0; display: flex; flex-direction: column; gap: 4px; }}
.wf-name-line {{ display: flex; align-items: center; gap: 10px; }}
.wf-name {{ font-weight: 600; color: var(--text-primary); font-size: 14px; }}
.wf-trigger {{ color: var(--text-secondary); font-size: 12.5px; line-height: 1.45; }}
.wf-effect {{ color: var(--text-tertiary); font-size: 12px; line-height: 1.45; font-style: italic; }}
.wf-meta {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 4px; color: var(--text-tertiary); font-size: 11px; }}
.wf-meta-item {{ font-variant-numeric: tabular-nums; }}
.wf-applies {{ display: inline-flex; flex-wrap: wrap; gap: 4px; align-items: center; min-width: 0; margin-left: 4px; }}
.wf-applies-pill {{ font-size: 10px; padding: 1px 7px; border-radius: 999px; background: rgba(34,197,94,0.14); color: #4ade80; border: 1px solid rgba(34,197,94,0.32); white-space: nowrap; }}
.wf-applies-pill.disabled {{ background: rgba(107,114,128,0.12); color: var(--text-tertiary); border-color: var(--border-subtle); text-decoration: line-through; opacity: 0.7; }}
.wf-applies-more {{ font-size: 10px; padding: 1px 7px; color: var(--text-tertiary); }}
.wf-applies-all {{ font-size: 10px; color: #6b9eff; font-weight: 600; padding: 1px 7px; border-radius: 999px; background: rgba(59,130,246,0.12); }}
.wf-applies-empty {{ font-size: 10px; color: var(--text-tertiary); font-style: italic; }}
.wf-cell {{ font-size: 12px; color: var(--text-secondary); }}
.wf-num {{ font-variant-numeric: tabular-nums; color: var(--text-tertiary); }}
.wf-actions {{ display: flex; align-items: center; gap: 10px; }}
.wf-row-switch {{ flex: 0 0 auto; }}
.wf-match {{ display: inline-flex; align-items: center; gap: 4px; font-size: 10px; padding: 1px 8px; border-radius: 999px; font-weight: 600; letter-spacing: 0.2px; white-space: nowrap; }}
.wf-match-loading {{ background: var(--bg-card); color: var(--text-tertiary); border: 1px solid var(--border-subtle); }}
.wf-match-zero {{ background: var(--bg-card); color: var(--text-tertiary); border: 1px solid var(--border-subtle); }}
.wf-match-some {{ background: rgba(34,197,94,0.14); color: #4ade80; border: 1px solid rgba(34,197,94,0.32); }}
.wf-match-manual {{ background: rgba(168,85,247,0.14); color: #c084fc; border: 1px solid rgba(168,85,247,0.32); }}
.wf-empty {{ padding: 32px 20px; color: var(--text-tertiary); text-align: center; font-style: italic; font-size: 13px; }}
.wf-edit-toggle, .ag-edit-toggle, .ep-edit-toggle {{ font-size: 11px; padding: 4px 10px; border-radius: 6px; border: 1px solid var(--border-default); background: var(--bg-surface); color: var(--text-secondary); cursor: pointer; font-family: inherit; }}
.wf-edit-toggle:hover, .ag-edit-toggle:hover, .ep-edit-toggle:hover {{ border-color: var(--accent); color: var(--accent); }}
.wf-edit-toggle.active, .ag-edit-toggle.active, .ep-edit-toggle.active {{ background: var(--accent); border-color: var(--accent); color: white; }}
.wf-edit-panel, .ag-edit-panel, .ep-edit-panel {{ padding: 14px 18px 16px; background: rgba(255,255,255,0.02); border-top: 1px solid var(--border-subtle); display: flex; flex-direction: column; gap: 10px; }}
.wf-edit-panel[hidden], .ag-edit-panel[hidden], .ep-edit-panel[hidden] {{ display: none; }}
[data-theme="light"] .wf-edit-panel, [data-theme="light"] .ag-edit-panel, [data-theme="light"] .ep-edit-panel {{ background: rgba(0,0,0,0.02); }}
.wf-edit-row {{ display: grid; grid-template-columns: 110px 1fr; gap: 12px; align-items: start; }}
.wf-edit-row label {{ font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.4px; padding-top: 6px; }}
.wf-edit-row input[type="text"], .wf-edit-row textarea {{ width: 100%; background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-default); border-radius: 6px; padding: 6px 10px; font-size: 13px; font-family: inherit; }}
.wf-edit-row textarea {{ resize: vertical; min-height: 60px; }}
.wf-edit-row input[type="text"]:focus, .wf-edit-row textarea:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }}
.wf-edit-switch {{ position: relative; display: inline-block; width: 36px; height: 20px; }}
.wf-edit-switch input {{ opacity: 0; width: 0; height: 0; }}
.wf-edit-slider {{ position: absolute; cursor: pointer; inset: 0; background: var(--border-default); border-radius: 20px; transition: 0.15s; }}
.wf-edit-slider::before {{ position: absolute; content: ""; height: 14px; width: 14px; left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: 0.15s; }}
.wf-edit-switch input:checked + .wf-edit-slider {{ background: var(--accent); }}
.wf-edit-switch input:checked + .wf-edit-slider::before {{ transform: translateX(16px); }}
.wf-edit-steps {{ list-style: none; padding: 0; margin: 0; font-size: 12px; color: var(--text-secondary); display: flex; flex-direction: column; gap: 3px; }}
.wf-edit-steps .wf-edit-empty {{ color: var(--text-tertiary); font-style: italic; }}
.wf-cond-list {{ list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 4px; }}
.wf-cond-item {{ display: flex; align-items: baseline; gap: 8px; padding: 4px 8px; background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 6px; font-size: 12px; }}
.wf-cond-item.wf-cond-neg {{ border-color: rgba(168,85,247,0.32); background: rgba(168,85,247,0.06); }}
.wf-cond-label {{ color: var(--text-secondary); font-weight: 500; }}
.wf-cond-value {{ color: var(--text-primary); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; padding: 1px 6px; border-radius: 4px; background: var(--bg-page); border: 1px solid var(--border-subtle); }}
.wf-cond-empty {{ font-size: 12px; color: var(--text-tertiary); font-style: italic; padding: 4px 0; }}
.wf-edit-advanced {{ font-size: 12px; color: var(--accent); text-decoration: none; padding-left: 122px; }}
.wf-edit-advanced:hover {{ text-decoration: underline; }}
/* Rules editor — Apple-mail style attribute / operation / value rows */
.wf-rules-editor {{ display: flex; flex-direction: column; gap: 8px; }}
.wf-rules-loading {{ font-size: 12px; color: var(--text-tertiary); font-style: italic; }}
.wf-rules-mode {{ font-size: 12px; color: var(--text-secondary); }}
.wf-rules-mode-sel {{ background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-default); border-radius: 6px; padding: 2px 6px; font-family: inherit; font-size: 12px; }}
.wf-rules-list {{ display: flex; flex-direction: column; gap: 6px; }}
.wf-rule-row {{ display: flex; align-items: center; gap: 6px; padding: 6px 8px; background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 6px; }}
.wf-rule-row select, .wf-rule-row input[type="text"], .wf-rule-row input[type="number"] {{ background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-default); border-radius: 4px; padding: 3px 6px; font-size: 12px; font-family: inherit; }}
.wf-rule-row select[multiple] {{ min-width: 140px; padding: 2px 4px; }}
.wf-rule-attr, .wf-rule-verb {{ min-width: 140px; }}
.wf-rule-op {{ min-width: 110px; }}
.wf-rule-value-wrap {{ flex: 1; }}
.wf-rule-value {{ width: 100%; }}
.wf-rule-novalue {{ flex: 1; color: var(--text-tertiary); font-size: 12px; }}
.wf-rule-del {{ width: 22px; height: 22px; padding: 0; line-height: 1; border: 1px solid var(--border-subtle); background: transparent; color: var(--text-tertiary); border-radius: 4px; cursor: pointer; font-size: 14px; }}
.wf-rule-del:hover {{ border-color: #ef4444; color: #ef4444; }}
.wf-rules-add {{ align-self: flex-start; padding: 4px 12px; font-size: 12px; border-radius: 6px; border: 1px dashed var(--border-default); background: transparent; color: var(--text-secondary); cursor: pointer; font-family: inherit; }}
.wf-rules-add:hover {{ border-color: var(--accent); color: var(--accent); border-style: solid; }}
.wf-rules-action-header {{ font-size: 12px; color: var(--text-secondary); margin-top: 6px; }}
.wf-rules-applyto {{ background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-default); border-radius: 6px; padding: 2px 6px; font-family: inherit; font-size: 12px; }}
.wf-rules-advisory {{ font-size: 11.5px; padding: 6px 10px; border-radius: 6px; border: 1px solid transparent; }}
.wf-rules-advisory.wf-rules-ok {{ background: rgba(34,197,94,0.08); border-color: rgba(34,197,94,0.25); color: rgba(34,197,94,0.95); }}
.wf-rules-advisory.wf-rules-warn {{ background: rgba(234,179,8,0.10); border-color: rgba(234,179,8,0.30); color: rgba(234,179,8,0.95); }}
.wf-rules-advisory.wf-rules-empty, .wf-rules-advisory.wf-rules-manual {{ background: var(--bg-card); border-color: var(--border-subtle); color: var(--text-tertiary); }}
.wf-rules-readonly select, .wf-rules-readonly input, .wf-rules-readonly button {{ opacity: 0.6; cursor: not-allowed; }}
.wf-edit-note {{ font-size: 11px; color: var(--text-tertiary); padding: 8px 10px; background: rgba(168,85,247,0.08); border: 1px solid rgba(168,85,247,0.2); border-radius: 6px; }}
.wf-edit-actions {{ display: flex; align-items: center; gap: 10px; padding-left: 122px; padding-top: 4px; }}
.wf-edit-save, .ag-edit-save {{ padding: 6px 14px; font-size: 12px; border-radius: 6px; border: 1px solid var(--accent); background: var(--accent); color: white; cursor: pointer; font-family: inherit; font-weight: 600; }}
.wf-edit-save:hover, .ag-edit-save:hover {{ filter: brightness(1.1); }}
.wf-edit-delete, .ag-edit-delete, .ep-edit-delete {{ padding: 6px 14px; font-size: 12px; border-radius: 6px; border: 1px solid #ef4444; background: transparent; color: #ef4444; cursor: pointer; font-family: inherit; }}
.wf-edit-delete:hover:not(:disabled), .ag-edit-delete:hover, .ep-edit-delete:hover:not(:disabled) {{ background: rgba(239,68,68,0.1); }}
.wf-edit-delete:disabled, .ep-edit-delete:disabled {{ opacity: 0.45; cursor: not-allowed; }}
.wf-edit-duplicate {{ padding: 6px 14px; font-size: 12px; border-radius: 6px; border: 1px solid var(--border-default); background: transparent; color: var(--text-secondary); cursor: pointer; font-family: inherit; }}
.wf-edit-duplicate:hover {{ border-color: var(--accent); color: var(--accent); }}
.wf-edit-msg {{ font-size: 11px; color: var(--text-tertiary); }}
.wf-edit-msg.ok {{ color: #4ade80; }}
.wf-edit-msg.err {{ color: #ef4444; }}
.ag-row-wrap {{ border-bottom: 1px solid var(--border-subtle); }}
.ag-row-wrap:last-child {{ border-bottom: 0; }}
.ag-row {{ display: grid; grid-template-columns: 1fr 90px 70px; gap: 12px; padding: 10px 14px; align-items: center; font-size: 13px; }}
.ag-row:hover {{ background: rgba(255,255,255,0.03); }}
.ag-main {{ min-width: 0; }}
.ag-name {{ font-weight: 600; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.ag-cmd {{ color: var(--text-tertiary); font-size: 11px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.ag-cell {{ font-size: 12px; color: var(--text-secondary); }}
.ag-type {{ font-size: 10px; padding: 2px 7px; border-radius: 999px; font-weight: 600; letter-spacing: 0.3px; text-transform: uppercase; }}
.ag-type-custom {{ background: rgba(59,130,246,0.18); color: #6b9eff; }}
.wf-pane-btn {{ padding: 6px 14px; font-size: 12px; border-radius: 6px; border: 1px solid var(--accent); background: transparent; color: var(--accent); cursor: pointer; font-family: inherit; font-weight: 600; }}
.wf-pane-btn:hover {{ background: rgba(59,130,246,0.1); }}
</style>
<style>{rail_css}</style>
<style>{drawer_css}</style>
</head>
<body>
{rail_html}
{drawer_html}
<header class="wf-header">
  <h1>Workflows</h1>
  <span class="wf-header-sub">Cross-project — system defaults, global rules, and per-project workflows.</span>
</header>
<div class="wf-page">
  <div class="wf-tabs" data-testid="wf-tabs">
    <button class="wf-tab active" data-tab="workflows" data-testid="wf-tab-workflows">Workflows</button>
    <button class="wf-tab" data-tab="agents" data-testid="wf-tab-agents">Agents</button>
    <button class="wf-tab" data-tab="endpoints" data-testid="wf-tab-endpoints">Endpoints</button>
  </div>

  <div class="wf-tab-pane active" data-tab-pane="workflows">
    <div class="wf-toolbar" data-testid="wf-filter-bar">
      <span class="wf-toolbar-label">Projects</span>
      <button class="wf-proj-filter active" data-project="" data-testid="wf-proj-all">All projects</button>
      {"".join(f'<button class="wf-proj-filter" data-project="{_safe_attr(p["id"])}" data-testid="wf-proj-{_safe_attr(p["id"])}">{_html.escape(p["name"])} <span class="wf-num">{sum(1 for w in workflows if any(l["project_id"] == p["id"] for l in (w.get("links") or [])))}</span></button>' for p in projects)}
    </div>
    <div class="wf-list" data-testid="wf-list">{rows_html}</div>
  </div>

  <div class="wf-tab-pane" data-tab-pane="agents">
    <div class="wf-pane-header">
      <h2>Custom agents</h2>
      <button class="wf-pane-btn" id="ag-new-btn" data-testid="ag-new-btn">+ New Agent</button>
    </div>
    <div class="ag-new-form" id="ag-new-form" hidden>
      <div class="wf-edit-panel">
        <div class="wf-edit-row"><label>ID</label><input type="text" data-field="id" placeholder="lowercase-with-dashes"></div>
        <div class="wf-edit-row"><label>Name</label><input type="text" data-field="name"></div>
        <div class="wf-edit-row"><label>System prompt</label><textarea data-field="system_prompt" rows="4"></textarea></div>
        <div class="wf-edit-actions">
          <button class="ag-edit-save" id="ag-new-save">Create</button>
          <button class="wf-edit-delete" id="ag-new-cancel">Cancel</button>
          <span class="wf-edit-msg" id="ag-new-msg"></span>
        </div>
      </div>
    </div>
    <div class="wf-list" data-testid="ag-list" id="ag-list">{agent_rows_html}</div>
  </div>

  <div class="wf-tab-pane" data-tab-pane="endpoints">
    <div class="wf-pane-header">
      <h2>Model endpoints</h2>
      <button class="wf-pane-btn" id="ep-new-btn" data-testid="ep-new-btn">+ New Endpoint</button>
    </div>
    <div class="ag-new-form" id="ep-new-form" hidden>
      <div class="wf-edit-panel">
        <div class="wf-edit-row"><label>ID</label><input type="text" data-field="id" placeholder="lowercase-with-dashes"></div>
        <div class="wf-edit-row"><label>Name</label><input type="text" data-field="name"></div>
        <div class="wf-edit-row"><label>Type</label>
          <select data-field="endpoint_type">
            <option value="cli" selected>cli</option>
            <option value="anthropic_api">anthropic_api ⚠ not executable in phase 1</option>
            <option value="openai_api">openai_api ⚠ not executable in phase 1</option>
            <option value="gemini_api">gemini_api ⚠ not executable in phase 1</option>
            <option value="ssh_cli">ssh_cli ⚠ not executable in phase 1</option>
          </select>
        </div>
        <div class="wf-edit-row"><label>Command</label><input type="text" data-field="command" value="claude"></div>
        <div class="wf-edit-row"><label>Args</label><input type="text" data-field="args" placeholder='JSON array e.g. ["-p"]'></div>
        <div class="wf-edit-row"><label>Prompt mode</label>
          <select data-field="prompt_mode">
            <option value="template" selected>template</option>
            <option value="stdin">stdin (reserved)</option>
          </select>
        </div>
        <div class="wf-edit-row"><label>Timeout (s)</label><input type="number" data-field="timeout_s" value="120"></div>
        <div class="wf-edit-actions">
          <button class="ag-edit-save" id="ep-new-save">Create</button>
          <button class="wf-edit-delete" id="ep-new-cancel">Cancel</button>
          <span class="wf-edit-msg" id="ep-new-msg"></span>
        </div>
      </div>
    </div>
    <div class="wf-list" data-testid="ep-list" id="ep-list">{endpoint_rows_html}</div>
  </div>
</div>
<script>
(function() {{
  var projectSelection = new Set();

  function applyFilters() {{
    document.querySelectorAll('[data-tab-pane="workflows"] .wf-row-wrap').forEach(function(row) {{
      var rowProjects = (row.dataset.projects || '').split(',').filter(Boolean);
      var projOk;
      if (projectSelection.size === 0) {{
        projOk = true;
      }} else {{
        projOk = rowProjects.some(function(pid) {{ return projectSelection.has(pid); }});
      }}
      row.style.display = projOk ? '' : 'none';
    }});
    // Hide whole groups whose rows are all filtered out
    document.querySelectorAll('[data-tab-pane="workflows"] .wf-group').forEach(function(g) {{
      var visible = Array.from(g.querySelectorAll('.wf-row-wrap')).some(function(r) {{ return r.style.display !== 'none'; }});
      g.style.display = visible ? '' : 'none';
    }});
  }}

  document.querySelectorAll('.wf-proj-filter').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var pid = btn.dataset.project;
      if (pid === '') {{
        projectSelection.clear();
        document.querySelectorAll('.wf-proj-filter').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
      }} else {{
        if (projectSelection.has(pid)) {{
          projectSelection.delete(pid);
          btn.classList.remove('active');
        }} else {{
          projectSelection.add(pid);
          btn.classList.add('active');
        }}
        var allBtn = document.querySelector('.wf-proj-filter[data-project=""]');
        if (allBtn) allBtn.classList.toggle('active', projectSelection.size === 0);
      }}
      applyFilters();
    }});
  }});

  // Tab switching
  document.querySelectorAll('.wf-tab').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var tab = btn.dataset.tab;
      document.querySelectorAll('.wf-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.querySelectorAll('.wf-tab-pane').forEach(p => p.classList.toggle('active', p.dataset.tabPane === tab));
    }});
  }});

  function setMsg(el, text, kind) {{
    if (!el) return;
    el.textContent = text || '';
    el.classList.remove('ok', 'err');
    if (kind) el.classList.add(kind);
  }}

  function parseArgs(raw) {{
    var t = (raw || '').trim();
    if (!t) return [];
    if (t.startsWith('[')) {{
      try {{ var arr = JSON.parse(t); return Array.isArray(arr) ? arr : []; }}
      catch (e) {{ return []; }}
    }}
    return t.split(',').map(s => s.trim()).filter(Boolean);
  }}

  // Workflow row Edit toggle + Save + Delete
  document.querySelectorAll('.wf-edit-toggle').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      var panel = document.querySelector('.wf-edit-panel[data-id="' + id + '"]');
      if (!panel) return;
      var open = !panel.hasAttribute('hidden');
      if (open) {{
        panel.setAttribute('hidden', '');
        btn.classList.remove('active');
        btn.textContent = 'Edit';
      }} else {{
        panel.removeAttribute('hidden');
        btn.classList.add('active');
        btn.textContent = 'Close';
      }}
    }});
  }});

  document.querySelectorAll('.wf-edit-save').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      var panel = document.querySelector('.wf-edit-panel[data-id="' + id + '"]');
      var msg = panel.querySelector('.wf-edit-msg');
      var wrap = document.querySelector('.wf-row-wrap[data-wf-id="' + id + '"]');
      var isSystem = wrap && wrap.dataset.system === '1';
      var body = {{}};
      if (!isSystem) {{
        body.name = panel.querySelector('input[data-field="name"]').value;
        body.description = panel.querySelector('textarea[data-field="description"]').value;
        // Serialize the rules editor (trigger + on_success) for non-system rows
        var editor = panel.querySelector('.wf-rules-editor[data-rules-mounted="1"]');
        if (editor) {{
          var serialised = serialiseRulesEditor(editor);
          body.trigger_json = serialised.trigger_json;
          body.on_success_json = serialised.on_success_json;
        }}
      }}
      body.enabled = panel.querySelector('input[data-field="enabled"]').checked ? 1 : 0;
      setMsg(msg, 'Saving…');
      fetch('/api/workflow/workflows/' + encodeURIComponent(id), {{
        method: 'PUT',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(body)
      }}).then(r => r.json().then(d => ({{status: r.status, data: d}})))
        .then(({{status, data}}) => {{
          if (status >= 200 && status < 300) {{
            setMsg(msg, 'Saved', 'ok');
            setTimeout(() => location.reload(), 600);
          }} else {{
            setMsg(msg, (data && data.error) || 'Failed', 'err');
          }}
        }})
        .catch(e => setMsg(msg, String(e), 'err'));
    }});
  }});

  // ─────────────────────────────────────────────────────────────────────
  // Workflow rules editor — Apple-style attribute/operation/value rows.
  // Mounts on demand when the user expands an Edit panel. Catalog is
  // fetched once and cached.
  // ─────────────────────────────────────────────────────────────────────
  var _wfCatalog = null;
  function getCatalog() {{
    if (_wfCatalog) return Promise.resolve(_wfCatalog);
    return fetch('/api/workflow/catalog').then(r => r.json()).then(c => {{
      _wfCatalog = c; return c;
    }});
  }}

  function attrByKey(catalog, key) {{
    return catalog.attributes.find(function(a) {{ return a.key === key; }});
  }}

  // Map a single predicate node back to (attrKey, opKey, value)
  function predicateToRow(catalog, p) {{
    if (!p || typeof p !== 'object' || !p.kind) return null;
    var attrKey = catalog.predicate_to_attribute[p.kind];
    if (!attrKey) return null;
    var attr = attrByKey(catalog, attrKey);
    if (!attr) return null;
    var op = attr.filter_ops.find(function(o) {{ return o.predicate_kind === p.kind; }});
    if (!op) return null;
    // Extract value: predicates use various key names — value, values, field, flag
    var value;
    if ('values' in p) value = p.values;
    else if ('value' in p) value = p.value;
    else if ('field' in p) value = p.field;
    else if ('flag' in p) value = p.flag;
    return {{attr: attrKey, op: op.key, value: value}};
  }}

  function effectsToRows(catalog, on_success) {{
    var rows = [];
    if (!on_success || typeof on_success !== 'object') return rows;
    Object.keys(on_success).forEach(function(k) {{
      if (k === 'apply_to') return;
      var attrKey = catalog.effect_to_attribute[k];
      if (!attrKey) return;
      var attr = attrByKey(catalog, attrKey);
      if (!attr) return;
      var op = attr.action_ops.find(function(o) {{ return o.on_success_key === k; }});
      if (!op) return;
      var v = on_success[k];
      // set_readiness_content uses a {{flag, from}} shape — keep flag as the value control
      if (k === 'set_readiness_content' && v && typeof v === 'object') {{
        v = v.flag;
      }}
      rows.push({{attr: attrKey, op: op.key, value: v}});
    }});
    return rows;
  }}

  // Build the value-control DOM element for a given control kind
  function buildValueControl(catalog, controlKind, currentValue) {{
    var el;
    var opt = catalog.options || {{}};
    if (controlKind === 'none') {{
      el = document.createElement('span');
      el.className = 'wf-rule-novalue';
      el.textContent = '';
    }} else if (controlKind === 'section_select') {{
      el = document.createElement('select');
      (opt.sections || []).forEach(function(s) {{
        var o = document.createElement('option'); o.value = s; o.textContent = s; el.appendChild(o);
      }});
      el.value = currentValue || '';
    }} else if (controlKind === 'section_multi_select') {{
      el = document.createElement('select');
      el.multiple = true; el.size = 4;
      (opt.sections || []).forEach(function(s) {{
        var o = document.createElement('option'); o.value = s; o.textContent = s; el.appendChild(o);
      }});
      var vs = Array.isArray(currentValue) ? currentValue : (currentValue ? [currentValue] : []);
      Array.from(el.options).forEach(function(o) {{ o.selected = vs.indexOf(o.value) !== -1; }});
    }} else if (controlKind === 'status_select') {{
      el = document.createElement('select');
      (opt.statuses || []).forEach(function(s) {{
        var o = document.createElement('option'); o.value = s; o.textContent = s; el.appendChild(o);
      }});
      el.value = currentValue || '';
    }} else if (controlKind === 'status_multi_select') {{
      el = document.createElement('select');
      el.multiple = true; el.size = 4;
      (opt.statuses || []).forEach(function(s) {{
        var o = document.createElement('option'); o.value = s; o.textContent = s; el.appendChild(o);
      }});
      var vs = Array.isArray(currentValue) ? currentValue : (currentValue ? [currentValue] : []);
      Array.from(el.options).forEach(function(o) {{ o.selected = vs.indexOf(o.value) !== -1; }});
    }} else if (controlKind === 'spec_status_multi_select') {{
      el = document.createElement('select');
      el.multiple = true; el.size = 4;
      (opt.spec_statuses || []).forEach(function(s) {{
        var o = document.createElement('option'); o.value = s; o.textContent = s; el.appendChild(o);
      }});
      var vs = Array.isArray(currentValue) ? currentValue : (currentValue ? [currentValue] : []);
      Array.from(el.options).forEach(function(o) {{ o.selected = vs.indexOf(o.value) !== -1; }});
    }} else if (controlKind === 'automation_mode_select') {{
      el = document.createElement('select');
      (opt.automation_modes || []).forEach(function(s) {{
        var o = document.createElement('option'); o.value = s; o.textContent = s; el.appendChild(o);
      }});
      el.value = currentValue || 'auto';
    }} else if (controlKind === 'priority_select') {{
      el = document.createElement('select');
      (opt.priorities || []).forEach(function(s) {{
        var o = document.createElement('option'); o.value = s; o.textContent = s; el.appendChild(o);
      }});
      el.value = currentValue || 'medium';
    }} else if (controlKind === 'bool_select') {{
      el = document.createElement('select');
      [['1', 'yes'], ['0', 'no']].forEach(function(pair) {{
        var o = document.createElement('option'); o.value = pair[0]; o.textContent = pair[1]; el.appendChild(o);
      }});
      el.value = (currentValue === true || currentValue === 1 || currentValue === '1') ? '1' : '0';
    }} else if (controlKind === 'flag_select') {{
      el = document.createElement('select');
      (opt.flags || []).forEach(function(f) {{
        var o = document.createElement('option'); o.value = f.key; o.textContent = f.label; el.appendChild(o);
      }});
      el.value = currentValue || 'D';
    }} else if (controlKind === 'field_select') {{
      el = document.createElement('select');
      (opt.fields || []).forEach(function(s) {{
        var o = document.createElement('option'); o.value = s; o.textContent = s; el.appendChild(o);
      }});
      el.value = currentValue || 'description';
    }} else if (controlKind === 'tag_input' || controlKind === 'tag_multi_input') {{
      el = document.createElement('input');
      el.type = 'text';
      el.placeholder = 'comma-separated tags';
      var vs = Array.isArray(currentValue) ? currentValue.join(', ') : (currentValue || '');
      el.value = vs;
    }} else if (controlKind === 'number_input') {{
      el = document.createElement('input');
      el.type = 'number'; el.min = '0';
      el.value = (currentValue !== undefined && currentValue !== null) ? String(currentValue) : '1';
    }} else {{
      el = document.createElement('input');
      el.type = 'text';
      el.value = currentValue || '';
    }}
    el.classList.add('wf-rule-value');
    return el;
  }}

  function readValueControl(controlKind, el) {{
    if (controlKind === 'none') return null;
    if (controlKind === 'section_multi_select' || controlKind === 'status_multi_select' || controlKind === 'spec_status_multi_select') {{
      return Array.from(el.selectedOptions).map(function(o) {{ return o.value; }});
    }}
    if (controlKind === 'tag_input' || controlKind === 'tag_multi_input') {{
      return splitCSV(el.value);
    }}
    if (controlKind === 'number_input') {{
      var n = parseInt(el.value, 10);
      return isFinite(n) ? n : 0;
    }}
    if (controlKind === 'bool_select') {{
      return el.value === '1' ? 1 : 0;
    }}
    return el.value;
  }}

  // Build a single filter row: [Attribute] [Operation] [Value] [×]
  function buildFilterRow(catalog, current) {{
    current = current || {{attr: null, op: null, value: null}};
    var row = document.createElement('div');
    row.className = 'wf-rule-row wf-rule-filter';
    // Attribute select
    var attrSel = document.createElement('select');
    attrSel.className = 'wf-rule-attr';
    var pHolder = document.createElement('option');
    pHolder.value = ''; pHolder.textContent = 'Choose attribute…';
    attrSel.appendChild(pHolder);
    catalog.attributes.forEach(function(a) {{
      if (!a.filter_ops || a.filter_ops.length === 0) return;
      var o = document.createElement('option'); o.value = a.key; o.textContent = a.label;
      attrSel.appendChild(o);
    }});
    if (current.attr) attrSel.value = current.attr;
    var opSel = document.createElement('select');
    opSel.className = 'wf-rule-op';
    var valWrap = document.createElement('span');
    valWrap.className = 'wf-rule-value-wrap';
    var del = document.createElement('button');
    del.className = 'wf-rule-del'; del.type = 'button'; del.textContent = '×';
    del.title = 'Remove condition';
    del.addEventListener('click', function() {{ row.remove(); }});

    function rebuildOps() {{
      var attr = attrByKey(catalog, attrSel.value);
      opSel.innerHTML = '';
      if (!attr) {{ valWrap.innerHTML = ''; return; }}
      attr.filter_ops.forEach(function(op) {{
        var o = document.createElement('option');
        o.value = op.key; o.textContent = op.label;
        opSel.appendChild(o);
      }});
      if (current.op) {{ opSel.value = current.op; current.op = null; }}
      rebuildValue();
    }}
    function rebuildValue() {{
      var attr = attrByKey(catalog, attrSel.value);
      if (!attr) {{ valWrap.innerHTML = ''; return; }}
      var op = attr.filter_ops.find(function(o) {{ return o.key === opSel.value; }});
      if (!op) {{ valWrap.innerHTML = ''; return; }}
      valWrap.innerHTML = '';
      var v = current.value; current.value = null;
      var ctrl = buildValueControl(catalog, op.value_control, v);
      valWrap.appendChild(ctrl);
    }}
    attrSel.addEventListener('change', function() {{ current.op = null; current.value = null; rebuildOps(); }});
    opSel.addEventListener('change', rebuildValue);
    rebuildOps();
    row.appendChild(attrSel);
    row.appendChild(opSel);
    row.appendChild(valWrap);
    row.appendChild(del);
    return row;
  }}

  // Build a single action row: [Verb] [Value] [×] — verbs are flat across attributes
  function buildActionRow(catalog, current) {{
    current = current || {{attr: null, op: null, value: null}};
    var row = document.createElement('div');
    row.className = 'wf-rule-row wf-rule-action';
    var verbSel = document.createElement('select');
    verbSel.className = 'wf-rule-verb';
    var pHolder = document.createElement('option');
    pHolder.value = ''; pHolder.textContent = 'Choose action…';
    verbSel.appendChild(pHolder);
    // Build flat verb list: "Attribute - operation label" → encoded "attr|op"
    catalog.attributes.forEach(function(a) {{
      if (!a.action_ops || a.action_ops.length === 0) return;
      a.action_ops.forEach(function(op) {{
        var o = document.createElement('option');
        o.value = a.key + '|' + op.key;
        o.textContent = a.label + ' — ' + op.label;
        verbSel.appendChild(o);
      }});
    }});
    if (current.attr && current.op) verbSel.value = current.attr + '|' + current.op;
    var valWrap = document.createElement('span');
    valWrap.className = 'wf-rule-value-wrap';
    var del = document.createElement('button');
    del.className = 'wf-rule-del'; del.type = 'button'; del.textContent = '×';
    del.title = 'Remove action';
    del.addEventListener('click', function() {{ row.remove(); }});

    function rebuildValue() {{
      var parts = (verbSel.value || '').split('|');
      if (parts.length !== 2) {{ valWrap.innerHTML = ''; return; }}
      var attr = attrByKey(catalog, parts[0]);
      var op = attr && attr.action_ops.find(function(o) {{ return o.key === parts[1]; }});
      if (!op) {{ valWrap.innerHTML = ''; return; }}
      valWrap.innerHTML = '';
      var v = current.value; current.value = null;
      var ctrl = buildValueControl(catalog, op.value_control, v);
      valWrap.appendChild(ctrl);
    }}
    verbSel.addEventListener('change', function() {{ current.value = null; rebuildValue(); }});
    rebuildValue();
    row.appendChild(verbSel);
    row.appendChild(valWrap);
    row.appendChild(del);
    return row;
  }}

  function mountRulesEditor(container, catalog, triggerJson, onSuccessJson, isSystem) {{
    container.innerHTML = '';
    container.dataset.rulesMounted = '1';

    // Match-mode header: "If [all|any] of the following conditions are met:"
    var modeWrap = document.createElement('div');
    modeWrap.className = 'wf-rules-mode';
    modeWrap.innerHTML = 'If <select class="wf-rules-mode-sel" data-testid="wf-rules-mode"><option value="all_of">all</option><option value="any_of">any</option></select> of the following conditions are met:';
    var initialMode = (triggerJson && triggerJson.any_of) ? 'any_of' : 'all_of';
    modeWrap.querySelector('select').value = initialMode;

    // Filter rows
    var filterList = document.createElement('div');
    filterList.className = 'wf-rules-list wf-rules-filters';
    filterList.dataset.testid = 'wf-rules-filters';
    var filterPredicates = [];
    if (triggerJson && (triggerJson.all_of || triggerJson.any_of)) {{
      filterPredicates = triggerJson.all_of || triggerJson.any_of;
    }} else if (triggerJson && triggerJson.kind) {{
      filterPredicates = [triggerJson];
    }}
    filterPredicates.forEach(function(p) {{
      var row = predicateToRow(catalog, p);
      if (row) filterList.appendChild(buildFilterRow(catalog, row));
    }});

    var addCondBtn = document.createElement('button');
    addCondBtn.type = 'button';
    addCondBtn.className = 'wf-rules-add';
    addCondBtn.textContent = '+ Add condition';
    addCondBtn.dataset.testid = 'wf-rules-add-condition';
    addCondBtn.addEventListener('click', function() {{
      filterList.appendChild(buildFilterRow(catalog, null));
    }});

    // Action rows
    var actionHeader = document.createElement('div');
    actionHeader.className = 'wf-rules-action-header';
    var applyToSel = document.createElement('select');
    applyToSel.className = 'wf-rules-applyto';
    applyToSel.dataset.testid = 'wf-rules-applyto';
    catalog.apply_to_targets.forEach(function(t) {{
      var o = document.createElement('option'); o.value = t.key; o.textContent = t.label; applyToSel.appendChild(o);
    }});
    var initialApplyTo = (onSuccessJson && onSuccessJson.apply_to) || 'self';
    applyToSel.value = initialApplyTo;
    actionHeader.appendChild(document.createTextNode('Then perform these actions on '));
    actionHeader.appendChild(applyToSel);
    actionHeader.appendChild(document.createTextNode(':'));

    var actionList = document.createElement('div');
    actionList.className = 'wf-rules-list wf-rules-actions';
    actionList.dataset.testid = 'wf-rules-actions';
    var actionRows = effectsToRows(catalog, onSuccessJson || {{}});
    actionRows.forEach(function(r) {{ actionList.appendChild(buildActionRow(catalog, r)); }});

    var addActBtn = document.createElement('button');
    addActBtn.type = 'button';
    addActBtn.className = 'wf-rules-add';
    addActBtn.textContent = '+ Add action';
    addActBtn.dataset.testid = 'wf-rules-add-action';
    addActBtn.addEventListener('click', function() {{
      actionList.appendChild(buildActionRow(catalog, null));
    }});

    // Linter advisory (populated on demand via /api/workflow/lint)
    var advisory = document.createElement('div');
    advisory.className = 'wf-rules-advisory';
    advisory.dataset.testid = 'wf-rules-advisory';

    container.appendChild(modeWrap);
    container.appendChild(filterList);
    container.appendChild(addCondBtn);
    container.appendChild(actionHeader);
    container.appendChild(actionList);
    container.appendChild(addActBtn);
    container.appendChild(advisory);

    if (isSystem) {{
      // Disable every editable element; surface read-only banner.
      container.classList.add('wf-rules-readonly');
      container.querySelectorAll('select, input, button.wf-rules-add, button.wf-rule-del')
        .forEach(function(el) {{ el.disabled = true; }});
    }}

    refreshAdvisory(container);
    container.addEventListener('change', function() {{ refreshAdvisory(container); }});
  }}

  function serialiseRulesEditor(container) {{
    var mode = container.querySelector('.wf-rules-mode-sel').value;
    var catalog = _wfCatalog;
    var filterRows = container.querySelectorAll('.wf-rule-filter');
    var predicates = [];
    filterRows.forEach(function(row) {{
      var attrKey = row.querySelector('.wf-rule-attr').value;
      var opKey = row.querySelector('.wf-rule-op').value;
      if (!attrKey || !opKey) return;
      var attr = attrByKey(catalog, attrKey);
      if (!attr) return;
      var op = attr.filter_ops.find(function(o) {{ return o.key === opKey; }});
      if (!op) return;
      var ctrl = row.querySelector('.wf-rule-value');
      var val = ctrl ? readValueControl(op.value_control, ctrl) : null;
      var pred = {{kind: op.predicate_kind}};
      // Map value back to predicate's value field shape based on kind
      if (op.predicate_kind === 'section_in' || op.predicate_kind === 'parent_section_not_in' ||
          op.predicate_kind === 'children_all_status_in' || op.predicate_kind === 'children_any_status_in' ||
          op.predicate_kind === 'spec_status_in') {{
        pred.values = Array.isArray(val) ? val : (val ? [val] : []);
      }} else if (op.predicate_kind === 'has_field') {{
        pred.field = val;
      }} else if (op.predicate_kind === 'flag_set' || op.predicate_kind === 'lacks_readiness_flag') {{
        pred.flag = val;
      }} else if (op.predicate_kind === 'has_tag' || op.predicate_kind === 'lacks_tag' || op.predicate_kind === 'has_all_tags') {{
        pred.value = Array.isArray(val) ? val : (val ? [val] : []);
      }} else if (op.value_control !== 'none') {{
        pred.value = val;
      }}
      predicates.push(pred);
    }});

    var trigger = predicates.length === 0
      ? null
      : (predicates.length === 1 ? predicates[0] : {{[mode]: predicates}});

    var actionRows = container.querySelectorAll('.wf-rule-action');
    var on_success = {{}};
    var applyTo = container.querySelector('.wf-rules-applyto').value;
    if (applyTo && applyTo !== 'self') on_success.apply_to = applyTo;
    actionRows.forEach(function(row) {{
      var verb = row.querySelector('.wf-rule-verb').value || '';
      var parts = verb.split('|');
      if (parts.length !== 2) return;
      var attr = attrByKey(catalog, parts[0]);
      var op = attr && attr.action_ops.find(function(o) {{ return o.key === parts[1]; }});
      if (!op) return;
      var ctrl = row.querySelector('.wf-rule-value');
      var val = ctrl ? readValueControl(op.value_control, ctrl) : null;
      // set_readiness_content takes a {{flag, from}} object; reconstruct.
      if (op.on_success_key === 'set_readiness_content') {{
        var fromSpec = (op.extra && op.extra.from) || 'stdout';
        on_success[op.on_success_key] = {{flag: val, from: fromSpec}};
      }} else if (op.on_success_key === 'clear_readiness_flag') {{
        on_success[op.on_success_key] = val;
      }} else if (op.on_success_key === 'set_summary_oneliner' || op.on_success_key === 'accept_ticket') {{
        on_success[op.on_success_key] = true;
      }} else if (Array.isArray(val) && (op.on_success_key === 'add_tags' || op.on_success_key === 'remove_tags')) {{
        on_success[op.on_success_key] = val;
      }} else {{
        on_success[op.on_success_key] = val;
      }}
    }});
    return {{trigger_json: trigger, on_success_json: Object.keys(on_success).length ? on_success : null}};
  }}

  function refreshAdvisory(container) {{
    var advisory = container.querySelector('.wf-rules-advisory');
    if (!advisory) return;
    var serialised = serialiseRulesEditor(container);
    fetch('/api/workflow/lint', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(serialised)
    }}).then(r => r.json()).then(function(d) {{
      advisory.className = 'wf-rules-advisory wf-rules-' + (d.status || 'empty');
      advisory.textContent = d.message || '';
    }}).catch(function() {{}});
  }}

  // Mount editor whenever an Edit panel is opened
  document.querySelectorAll('.wf-edit-toggle').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      var panel = document.querySelector('.wf-edit-panel[data-id="' + id + '"]');
      if (!panel) return;
      var editor = panel.querySelector('.wf-rules-editor');
      if (!editor) return;
      if (editor.dataset.rulesMounted === '1') return;
      var wrap = document.querySelector('.wf-row-wrap[data-wf-id="' + id + '"]');
      var isSystem = wrap && wrap.dataset.system === '1';
      var triggerJson = null;
      var onSuccessJson = null;
      try {{
        triggerJson = wrap && wrap.dataset.triggerJson ? JSON.parse(wrap.dataset.triggerJson) : null;
      }} catch (e) {{}}
      try {{
        onSuccessJson = wrap && wrap.dataset.onSuccessJson ? JSON.parse(wrap.dataset.onSuccessJson) : null;
      }} catch (e) {{}}
      getCatalog().then(function(cat) {{
        mountRulesEditor(editor, cat, triggerJson, onSuccessJson, isSystem);
      }});
    }});
  }});

  document.querySelectorAll('.wf-edit-delete').forEach(function(btn) {{
    if (btn.disabled) return;
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      if (!confirm('Delete workflow "' + id + '"?')) return;
      var panel = document.querySelector('.wf-edit-panel[data-id="' + id + '"]');
      var msg = panel ? panel.querySelector('.wf-edit-msg') : null;
      setMsg(msg, 'Deleting…');
      fetch('/api/workflow/workflows/' + encodeURIComponent(id), {{method: 'DELETE'}})
        .then(r => r.json().then(d => ({{status: r.status, data: d}})))
        .then(({{status, data}}) => {{
          if (status >= 200 && status < 300) {{
            setMsg(msg, 'Deleted', 'ok');
            setTimeout(() => location.reload(), 400);
          }} else {{
            setMsg(msg, (data && data.error) || 'Failed', 'err');
          }}
        }})
        .catch(e => setMsg(msg, String(e), 'err'));
    }});
  }});

  // Duplicate a workflow (system or user). Asks the user which project to
  // own the new copy when there's more than one option, defaults to the
  // source's first link (server picks if no project_id is sent).
  document.querySelectorAll('.wf-edit-duplicate').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      var panel = document.querySelector('.wf-edit-panel[data-id="' + id + '"]');
      var msg = panel ? panel.querySelector('.wf-edit-msg') : null;
      var wrap = document.querySelector('.wf-row-wrap[data-wf-id="' + id + '"]');
      var linkedPids = (wrap && wrap.dataset.projects ? wrap.dataset.projects.split(',').filter(Boolean) : []);
      var allProjects = [];
      try {{
        var metaEl = document.querySelector('meta[name="projects-list"]');
        if (metaEl) {{
          var parsed = JSON.parse(metaEl.getAttribute('content') || '[]');
          if (Array.isArray(parsed)) allProjects = parsed.map(function(p) {{ return p.id; }}).filter(Boolean);
        }}
      }} catch (e) {{ /* fall through to linked-only */ }}
      var candidates = linkedPids.length ? linkedPids : allProjects;
      var targetPid = candidates[0] || '';
      if (candidates.length > 1) {{
        var prompt = 'Duplicate into which project?\\n  ' + candidates.join('\\n  ') + '\\n(blank = ' + targetPid + ')';
        var picked = window.prompt(prompt, targetPid);
        if (picked === null) return;
        targetPid = (picked || targetPid).trim();
      }}
      setMsg(msg, 'Duplicating…');
      fetch('/api/workflow/workflows/' + encodeURIComponent(id) + '/duplicate', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(targetPid ? {{project_id: targetPid}} : {{}})
      }}).then(r => r.json().then(d => ({{status: r.status, data: d}})))
        .then(({{status, data}}) => {{
          if (status >= 200 && status < 300) {{
            setMsg(msg, 'Duplicated → ' + (data.id || ''), 'ok');
            setTimeout(() => location.reload(), 600);
          }} else {{
            setMsg(msg, (data && data.error) || 'Failed', 'err');
          }}
        }})
        .catch(e => setMsg(msg, String(e), 'err'));
    }});
  }});

  // Row-level Enabled toggle (the small switch outside the edit panel) — saves
  // immediately with no Edit panel round-trip. Server also mirrors into
  // workflow_projects so the dispatcher actually sees the change.
  document.querySelectorAll('input[data-wf-toggle]').forEach(function(input) {{
    input.addEventListener('change', function() {{
      var id = input.dataset.wfToggle;
      var enabled = input.checked ? 1 : 0;
      var wrap = document.querySelector('.wf-row-wrap[data-wf-id="' + id + '"]');
      // Also reflect in the in-panel checkbox so they don't get out of sync
      var inPanel = document.querySelector('.wf-edit-panel[data-id="' + id + '"] input[data-field="enabled"]');
      if (inPanel) inPanel.checked = !!enabled;
      input.disabled = true;
      fetch('/api/workflow/workflows/' + encodeURIComponent(id), {{
        method: 'PUT',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{enabled: enabled}})
      }}).then(r => r.json().then(d => ({{status: r.status, data: d}})))
        .then(({{status, data}}) => {{
          input.disabled = false;
          if (status < 200 || status >= 300) {{
            // Revert UI on error
            input.checked = !enabled;
            if (inPanel) inPanel.checked = !enabled;
            console.error('toggle failed', data);
          }}
        }})
        .catch(e => {{ input.disabled = false; input.checked = !enabled; console.error(e); }});
    }});
  }});

  // Live-match preview: for every non-manual row, fetch how many tickets
  // currently match the trigger and surface the count inline. Cached for the
  // page lifetime; users see fresh numbers on reload.
  function loadPreviews() {{
    var badges = document.querySelectorAll('.wf-match[data-wf-match]');
    badges.forEach(function(badge) {{
      var id = badge.dataset.wfMatch;
      fetch('/api/workflow/workflows/' + encodeURIComponent(id) + '/preview')
        .then(r => r.ok ? r.json() : null)
        .then(function(d) {{
          if (!d) return;
          badge.classList.remove('wf-match-loading');
          var n = d.count || 0;
          if (n === 0) {{
            badge.textContent = '0 match';
            badge.classList.add('wf-match-zero');
            badge.title = 'No tickets currently match this trigger';
          }} else {{
            badge.textContent = n + ' match' + (n === 1 ? '' : 'es');
            badge.classList.add('wf-match-some');
            var tip = (d.samples || []).map(function(s) {{ return s.id + ' ' + (s.title || ''); }}).join('\\n');
            if (tip) badge.title = tip;
          }}
        }})
        .catch(function() {{ /* leave the dot in loading state */ }});
    }});
  }}
  loadPreviews();

  // Agent row Edit toggle + Save + Delete
  document.querySelectorAll('.ag-edit-toggle').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      var panel = document.querySelector('.ag-edit-panel[data-id="' + id + '"]');
      if (!panel) return;
      var open = !panel.hasAttribute('hidden');
      if (open) {{
        panel.setAttribute('hidden', '');
        btn.classList.remove('active');
        btn.textContent = 'Edit';
      }} else {{
        panel.removeAttribute('hidden');
        btn.classList.add('active');
        btn.textContent = 'Close';
      }}
    }});
  }});

  document.querySelectorAll('.ag-edit-save').forEach(function(btn) {{
    // Skip endpoint save buttons — they have their own handler below
    if (btn.classList.contains('ep-edit-save')) return;
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      var panel = document.querySelector('.ag-edit-panel[data-id="' + id + '"]');
      var msg = panel.querySelector('.wf-edit-msg');
      // Confirm when saving with a non-CLI endpoint
      var epSelect = panel.querySelector('select[data-field="endpoint_id"]');
      if (epSelect && epSelect.value) {{
        var selectedOpt = epSelect.options[epSelect.selectedIndex];
        if (selectedOpt && selectedOpt.textContent.includes('not implemented')) {{
          if (!confirm('This endpoint type cannot execute in phase 1. ' +
                       'The agent will fail on next run. Continue?')) {{
            return;
          }}
        }}
      }}
      var body = {{
        name: panel.querySelector('input[data-field="name"]').value,
        system_prompt: panel.querySelector('textarea[data-field="system_prompt"]').value,
        endpoint_id: epSelect ? (epSelect.value || null) : null
      }};
      setMsg(msg, 'Saving…');
      fetch('/api/workflow/agents/' + encodeURIComponent(id), {{
        method: 'PUT',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(body)
      }}).then(r => r.json().then(d => ({{status: r.status, data: d}})))
        .then(({{status, data}}) => {{
          if (status >= 200 && status < 300) {{
            setMsg(msg, 'Saved', 'ok');
            setTimeout(() => location.reload(), 600);
          }} else {{
            setMsg(msg, (data && data.error) || 'Failed', 'err');
          }}
        }})
        .catch(e => setMsg(msg, String(e), 'err'));
    }});
  }});

  document.querySelectorAll('.ag-edit-delete').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      if (!confirm('Delete agent "' + id + '"?')) return;
      var panel = document.querySelector('.ag-edit-panel[data-id="' + id + '"]');
      var msg = panel ? panel.querySelector('.wf-edit-msg') : null;
      setMsg(msg, 'Deleting…');
      fetch('/api/workflow/agents/' + encodeURIComponent(id), {{method: 'DELETE'}})
        .then(r => r.json().then(d => ({{status: r.status, data: d}})))
        .then(({{status, data}}) => {{
          if (status >= 200 && status < 300) {{
            setMsg(msg, 'Deleted', 'ok');
            setTimeout(() => location.reload(), 400);
          }} else {{
            setMsg(msg, (data && data.error) || 'Failed', 'err');
          }}
        }})
        .catch(e => setMsg(msg, String(e), 'err'));
    }});
  }});

  // Show-all endpoints toggle — re-fetches /api/endpoints and repopulates the
  // per-agent endpoint dropdown, adding non-CLI options with a ⚠ warning label.
  document.querySelectorAll('.show-all-endpoints').forEach(function(cb) {{
    cb.addEventListener('change', async function() {{
      var select = cb.closest('.wf-edit-row').querySelector('select');
      if (!select) return;
      var current = select.value;
      try {{
        var r = await fetch('/api/endpoints');
        var data = await r.json();
        var allEndpoints = data.endpoints || [];
        select.innerHTML = '<option value=""></option>';
        allEndpoints.forEach(function(ep) {{
          if (!cb.checked && ep.endpoint_type !== 'cli') return;
          var label = ep.endpoint_type === 'cli'
            ? ep.name
            : (ep.name + ' ⚠ execution not implemented');
          var opt = document.createElement('option');
          opt.value = ep.id;
          opt.textContent = label;
          if (ep.id === current) opt.selected = true;
          select.appendChild(opt);
        }});
      }} catch (e) {{
        console.error('Failed to fetch endpoints', e);
      }}
    }});
  }});

  // New Agent form
  var newBtn = document.getElementById('ag-new-btn');
  var newForm = document.getElementById('ag-new-form');
  var newSave = document.getElementById('ag-new-save');
  var newCancel = document.getElementById('ag-new-cancel');
  var newMsg = document.getElementById('ag-new-msg');
  if (newBtn && newForm) {{
    newBtn.addEventListener('click', function() {{
      newForm.hidden = !newForm.hidden;
    }});
  }}
  if (newCancel) {{
    newCancel.addEventListener('click', function() {{
      newForm.hidden = true;
      setMsg(newMsg, '');
    }});
  }}
  if (newSave) {{
    newSave.addEventListener('click', function() {{
      var body = {{
        id: newForm.querySelector('input[data-field="id"]').value.trim(),
        name: newForm.querySelector('input[data-field="name"]').value.trim(),
        system_prompt: newForm.querySelector('textarea[data-field="system_prompt"]').value
      }};
      if (!body.id) {{ setMsg(newMsg, 'ID is required', 'err'); return; }}
      if (!body.name) body.name = body.id;
      setMsg(newMsg, 'Creating…');
      fetch('/api/workflow/agents', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(body)
      }}).then(r => r.json().then(d => ({{status: r.status, data: d}})))
        .then(({{status, data}}) => {{
          if (status >= 200 && status < 300) {{
            setMsg(newMsg, 'Created', 'ok');
            setTimeout(() => location.reload(), 500);
          }} else {{
            setMsg(newMsg, (data && data.error) || 'Failed', 'err');
          }}
        }})
        .catch(e => setMsg(newMsg, String(e), 'err'));
    }});
  }}

  // Endpoint row Edit toggle
  document.querySelectorAll('.ep-edit-toggle').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      var panel = document.querySelector('.ep-edit-panel[data-id="' + id + '"]');
      if (!panel) return;
      var open = !panel.hasAttribute('hidden');
      if (open) {{
        panel.setAttribute('hidden', '');
        btn.classList.remove('active');
        btn.textContent = 'Edit';
      }} else {{
        panel.removeAttribute('hidden');
        btn.classList.add('active');
        btn.textContent = 'Close';
      }}
    }});
  }});

  // Endpoint row Save
  document.querySelectorAll('.ep-edit-save').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      var panel = document.querySelector('.ep-edit-panel[data-id="' + id + '"]');
      var msg = panel.querySelector('.wf-edit-msg');
      var argsRaw = panel.querySelector('input[data-field="args"]').value.trim();
      var argsVal;
      try {{
        argsVal = argsRaw ? JSON.parse(argsRaw) : [];
      }} catch (e) {{
        argsVal = parseArgs(argsRaw);
      }}
      var body = {{
        name: panel.querySelector('input[data-field="name"]').value,
        endpoint_type: panel.querySelector('select[data-field="endpoint_type"]').value,
        command: panel.querySelector('input[data-field="command"]').value,
        args: argsVal,
        prompt_mode: panel.querySelector('select[data-field="prompt_mode"]').value,
        timeout_s: parseInt(panel.querySelector('input[data-field="timeout_s"]').value, 10) || 120
      }};
      setMsg(msg, 'Saving…');
      fetch('/api/endpoints/' + encodeURIComponent(id), {{
        method: 'PUT',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(body)
      }}).then(r => r.json().then(d => ({{status: r.status, data: d}})))
        .then(({{status, data}}) => {{
          if (status >= 200 && status < 300) {{
            setMsg(msg, 'Saved', 'ok');
            setTimeout(() => location.reload(), 600);
          }} else {{
            setMsg(msg, (data && data.error) || 'Failed', 'err');
          }}
        }})
        .catch(e => setMsg(msg, String(e), 'err'));
    }});
  }});

  // Endpoint row Delete
  document.querySelectorAll('.ep-edit-delete').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var id = btn.dataset.id;
      if (!confirm('Delete endpoint "' + id + '"?')) return;
      var panel = document.querySelector('.ep-edit-panel[data-id="' + id + '"]');
      var msg = panel ? panel.querySelector('.wf-edit-msg') : null;
      setMsg(msg, 'Deleting…');
      fetch('/api/endpoints/' + encodeURIComponent(id), {{method: 'DELETE'}})
        .then(r => r.json().then(d => ({{status: r.status, data: d}})))
        .then(({{status, data}}) => {{
          if (status >= 200 && status < 300) {{
            setMsg(msg, 'Deleted', 'ok');
            setTimeout(() => location.reload(), 400);
          }} else {{
            setMsg(msg, (data && data.error) || 'Failed', 'err');
          }}
        }})
        .catch(e => setMsg(msg, String(e), 'err'));
    }});
  }});

  // New Endpoint form
  var epNewBtn = document.getElementById('ep-new-btn');
  var epNewForm = document.getElementById('ep-new-form');
  var epNewSave = document.getElementById('ep-new-save');
  var epNewCancel = document.getElementById('ep-new-cancel');
  var epNewMsg = document.getElementById('ep-new-msg');
  if (epNewBtn && epNewForm) {{
    epNewBtn.addEventListener('click', function() {{
      epNewForm.hidden = !epNewForm.hidden;
    }});
  }}
  if (epNewCancel) {{
    epNewCancel.addEventListener('click', function() {{
      epNewForm.hidden = true;
      setMsg(epNewMsg, '');
    }});
  }}
  if (epNewSave) {{
    epNewSave.addEventListener('click', function() {{
      var argsRaw = epNewForm.querySelector('input[data-field="args"]').value.trim();
      var argsVal;
      try {{
        argsVal = argsRaw ? JSON.parse(argsRaw) : [];
      }} catch (e) {{
        argsVal = parseArgs(argsRaw);
      }}
      var body = {{
        id: epNewForm.querySelector('input[data-field="id"]').value.trim(),
        name: epNewForm.querySelector('input[data-field="name"]').value.trim(),
        endpoint_type: epNewForm.querySelector('select[data-field="endpoint_type"]').value,
        command: epNewForm.querySelector('input[data-field="command"]').value.trim() || 'claude',
        args: argsVal,
        prompt_mode: epNewForm.querySelector('select[data-field="prompt_mode"]').value,
        timeout_s: parseInt(epNewForm.querySelector('input[data-field="timeout_s"]').value, 10) || 120
      }};
      if (!body.id) {{ setMsg(epNewMsg, 'ID is required', 'err'); return; }}
      if (!body.name) body.name = body.id;
      setMsg(epNewMsg, 'Creating…');
      fetch('/api/endpoints', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(body)
      }}).then(r => r.json().then(d => ({{status: r.status, data: d}})))
        .then(({{status, data}}) => {{
          if (status >= 200 && status < 300) {{
            setMsg(epNewMsg, 'Created', 'ok');
            setTimeout(() => location.reload(), 500);
          }} else {{
            setMsg(epNewMsg, (data && data.error) || 'Failed', 'err');
          }}
        }})
        .catch(e => setMsg(epNewMsg, String(e), 'err'));
    }});
  }}
}})();
</script>
<script>{rail_js}</script>
<script>{drawer_js}</script>
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
    gear_icon = gen._svg_icon("settings", 14)
    gen._svg_icon("panel-left", 12)  # repurposed as fold indicator
    for proj in projects:
        pid = proj["id"]
        pid_attr = _safe_attr(pid)
        name = _safe_attr(proj.get("name", pid))
        raw_path = proj.get("path", "")
        display_path = _safe_attr(raw_path.replace(str(Path.home()), "~"))
        counts = counts_by_project.get(pid, {})
        wip = counts.get("WIP", 0)
        backlog = counts.get("Backlog", 0)
        review = counts.get("For Review", 0)
        path_exists = Path(os.path.expanduser(raw_path)).is_dir() if raw_path else False
        active = bool(proj.get("active", True))
        warn_html = (
            "" if path_exists else '<div class="proj-card-warn">Path not found</div>'
        )
        card_classes = "proj-card" + ("" if path_exists else " path-missing")

        cards_html += f'''
        <div class="{card_classes}" data-pid="{pid_attr}" data-path="{_safe_attr(raw_path)}" data-testid="proj-card-{pid_attr}">
          <div class="proj-card-main" data-pid="{pid_attr}">
            <div class="proj-card-info">
              <div class="proj-card-row">
                <div class="proj-card-name">{name}</div>
                {warn_html}
              </div>
              <div class="proj-card-path">{display_path}</div>
            </div>
            <div class="proj-card-counts">
              <span class="count-wip">{wip} WIP</span>
              <span class="count-backlog">{backlog} Backlog</span>
              <span class="count-review">{review} Review</span>
            </div>
            <button class="proj-card-gear" data-pid="{pid_attr}" title="Settings" aria-label="Settings for {name}">{gear_icon}</button>
          </div>
          <div class="proj-card-settings" data-pid="{pid_attr}" hidden>
            <div class="pcs-section">
              <div class="pcs-row"><label>Name</label><input type="text" data-field="name" value="{name}"></div>
              <div class="pcs-row"><label>Path</label><input type="text" data-field="path" value="{_safe_attr(raw_path)}"></div>
              <div class="pcs-row"><label>ID</label><input type="text" value="{pid_attr}" readonly class="pcs-readonly"></div>
              <div class="pcs-row"><label>Active</label>
                <label class="settings-toggle-switch">
                  <input type="checkbox" data-field="active"{" checked" if active else ""}>
                  <span class="settings-toggle-slider"></span>
                </label>
              </div>
              <div class="pcs-actions">
                <button class="pcs-save" data-pid="{pid_attr}">Save</button>
                <span class="pcs-msg"></span>
              </div>
            </div>
            <div class="pcs-section">
              <div class="pcs-section-title">Managed Files</div>
              <div class="pcs-managed-files" data-pid="{pid_attr}"></div>
            </div>
            <div class="pcs-section pcs-danger">
              <button class="pcs-remove" data-pid="{pid_attr}">Remove Project</button>
              <div class="pcs-hint">Removes from registry only. Files and tickets are not deleted.</div>
            </div>
          </div>
        </div>'''

    rail_css = gen.build_nav_rail_css()
    rail_html = gen.build_nav_rail_html()
    rail_js = gen.build_nav_rail_js()
    drawer_css = gen.build_settings_drawer_css()
    drawer_html = gen.build_settings_drawer_html(gen._svg_icon("x", 14))
    drawer_js = gen.build_settings_drawer_js()

    projects_meta_json = json.dumps(
        [{"id": p["id"], "name": p.get("name", p["id"])} for p in projects]
    )

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Ticket Takeaway</title>
{PWA_HEAD_TAGS}
<meta name="projects-list" content='{_safe_attr(projects_meta_json)}'>
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
body {{ background: var(--bg-page); color: var(--text-primary); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
.picker-header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 8px 20px; border-bottom: 1px solid var(--border-subtle); background: var(--bg-surface); }}
.picker-header h1 {{ font-size: 16px; font-weight: 600; }}
.picker-body {{ padding: 32px; }}
.header .count {{ color: var(--text-tertiary); font-size: 13px; }}
.grid {{ display: flex; flex-direction: column; gap: 8px; max-width: 1100px; }}
.proj-card {{ display: block; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 8px; text-decoration: none; color: inherit; transition: border-color 0.15s, background 0.15s; }}
.proj-card:hover {{ border-color: var(--accent); background: var(--bg-hover); }}
.proj-card-name {{ font-size: 14px; font-weight: 600; color: var(--text-primary); }}
.proj-card-path {{ font-size: 12px; color: var(--text-tertiary); font-family: monospace; }}
.proj-card-counts {{ display: flex; gap: 12px; font-size: 12px; }}
.count-wip {{ color: #f59e0b; }} .count-backlog {{ color: #3b82f6; }} .count-review {{ color: #ec4899; }}
.proj-card-warn {{ color: #ef4444; font-size: 11px; }}
.add-card {{ display: flex; align-items: center; justify-content: center; background: transparent; border: 2px dashed var(--border-default); border-radius: 10px; padding: 20px; min-height: 110px; cursor: pointer; transition: border-color 0.15s; color: var(--text-tertiary); }}
.add-card:hover {{ border-color: var(--accent); }}
.add-card-inner {{ text-align: center; }}
.add-card-plus {{ font-size: 24px; color: var(--text-tertiary); }}
.add-card-label {{ font-size: 13px; margin-top: 4px; }}
/* Add-Project modal: shown as a centred overlay above the project grid. */
.add-modal-backdrop {{ display: none; position: fixed; inset: 0; z-index: 900; background: rgba(0,0,0,0.6); backdrop-filter: blur(4px); align-items: flex-start; justify-content: center; padding: 64px 20px 20px; overflow-y: auto; }}
.add-modal-backdrop.visible {{ display: flex; }}
.add-form {{ position: relative; background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 12px; padding: 24px 28px; width: 520px; max-width: 100%; box-shadow: 0 12px 40px rgba(0,0,0,0.5); }}
.add-form-title {{ font-size: 16px; font-weight: 600; color: var(--text-primary); margin: 0 0 18px; }}
.add-form-close {{ position: absolute; top: 10px; right: 12px; background: none; border: none; color: var(--text-tertiary); font-size: 22px; line-height: 1; cursor: pointer; padding: 4px 8px; border-radius: 4px; }}
.add-form-close:hover {{ background: var(--bg-hover); color: var(--text-primary); }}
.add-form label {{ display: block; color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; margin-top: 14px; }}
.add-form label:first-of-type {{ margin-top: 0; }}
.add-form input {{ width: 100%; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 6px; padding: 8px 12px; color: var(--text-primary); font-size: 14px; }}
.add-form input:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }}
.add-form .btn {{ display: inline-block; margin-top: 16px; padding: 8px 20px; background: rgba(59,130,246,0.15); border: 1px solid rgba(59,130,246,0.3); color: var(--accent); border-radius: 6px; cursor: pointer; font-size: 13px; }}
.add-form .btn:hover {{ background: rgba(59,130,246,0.25); }}
.add-form .error {{ color: #ef4444; font-size: 12px; margin-top: 8px; display: none; }}
.browse-btn {{ display: inline-block; padding: 8px 16px; background: rgba(59,130,246,0.12); border: 1px solid rgba(59,130,246,0.3); color: var(--accent); border-radius: 6px; cursor: pointer; font-size: 13px; font-family: inherit; }}
.browse-btn:hover {{ background: rgba(59,130,246,0.22); }}
.path-display {{ font-family: monospace; font-size: 13px; color: var(--text-secondary); padding: 8px 0 0; min-height: 20px; }}
.picker-overlay {{ display: none; position: fixed; inset: 0; z-index: 1100; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px); align-items: center; justify-content: center; }}
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
/* Per-project expandable settings — single-row list layout */
.proj-card {{ position: relative; padding: 0; overflow: hidden; }}
.proj-card-main {{
  padding: 12px 18px; cursor: pointer; display: flex; align-items: center; gap: 18px;
}}
.proj-card-info {{ flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 3px; }}
.proj-card-row {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
.proj-card-row .proj-card-warn {{ margin-left: 6px; }}
.proj-card-counts {{ flex-shrink: 0; }}
/* Path-missing rows: dim only the info + counts so the warning + gear stay
   readable. The expanded settings panel always renders at full opacity, so
   the user can fix the broken path without squinting. */
.proj-card.path-missing {{
  border-style: dashed; border-color: rgba(239, 68, 68, 0.4);
}}
.proj-card.path-missing .proj-card-info,
.proj-card.path-missing .proj-card-counts {{ opacity: 0.55; }}
.proj-card.path-missing .proj-card-warn {{ opacity: 1; font-weight: 600; }}
.proj-card.path-missing .proj-card-gear {{ opacity: 0.85; }}
/* Path missing → row click is a no-op (gear still expands settings). */
.proj-card.path-missing .proj-card-main {{ cursor: default; }}
.proj-card.path-missing:hover {{ background: var(--bg-card); border-color: rgba(239, 68, 68, 0.4); }}
.proj-card-gear {{ background: none; border: none; color: var(--text-tertiary); cursor: pointer; padding: 4px; border-radius: 4px; line-height: 0; opacity: 0.6; transition: opacity 0.15s, color 0.15s, background 0.15s; }}
.proj-card-gear:hover {{ opacity: 1; color: var(--text-primary); background: var(--bg-hover); }}
.proj-card-gear svg {{ width: 14px; height: 14px; pointer-events: none; }}
.proj-card.expanded {{ border-color: var(--accent); }}
.proj-card-settings {{ border-top: 1px solid var(--border-subtle); padding: 14px 20px 16px; background: var(--bg-page); }}
.pcs-section {{ margin-bottom: 14px; }}
.pcs-section:last-child {{ margin-bottom: 0; }}
.pcs-section-title {{ font-size: 11px; font-weight: 700; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }}
.pcs-row {{ display: flex; align-items: center; gap: 10px; padding: 4px 0; }}
.pcs-row label {{ font-size: 12px; color: var(--text-secondary); width: 56px; flex-shrink: 0; }}
.pcs-row input[type="text"] {{ font-size: 11px; padding: 4px 8px; border-radius: 5px; flex: 1; border: 1px solid var(--border-default); background: var(--bg-card); color: var(--text-primary); font-family: monospace; outline: none; min-width: 0; }}
.pcs-row input[type="text"]:focus {{ border-color: var(--accent); }}
.pcs-readonly {{ opacity: 0.7; cursor: not-allowed; }}
.pcs-actions {{ display: flex; align-items: center; gap: 10px; margin-top: 8px; }}
.pcs-save {{ font-size: 11px; padding: 5px 14px; border-radius: 5px; border: 1px solid rgba(59,130,246,0.3); background: rgba(59,130,246,0.12); color: var(--accent); cursor: pointer; font-family: inherit; }}
.pcs-save:hover {{ background: rgba(59,130,246,0.22); }}
.pcs-msg {{ font-size: 11px; }}
.pcs-msg.ok {{ color: #22c55e; }}
.pcs-msg.err {{ color: #ef4444; }}
.pcs-managed-files {{ display: flex; flex-direction: column; gap: 4px; }}
.pcs-mf-row {{ display: flex; align-items: center; gap: 8px; font-size: 11px; padding: 3px 0; color: var(--text-secondary); }}
.pcs-mf-dot {{ width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }}
.pcs-mf-dot.exists {{ background: #22c55e; }}
.pcs-mf-dot.missing {{ background: var(--border-default); }}
.pcs-mf-path {{ font-family: monospace; color: var(--text-primary); }}
.pcs-mf-badge {{ font-size: 9px; padding: 1px 5px; border-radius: 3px; background: var(--bg-hover); color: var(--text-tertiary); }}
.pcs-mf-desc {{ color: var(--text-tertiary); font-size: 10px; margin-left: auto; text-align: right; }}
.pcs-danger {{ padding-top: 10px; border-top: 1px dashed var(--border-subtle); }}
.pcs-remove {{ font-size: 11px; padding: 5px 14px; border-radius: 5px; border: 1px solid rgba(239,68,68,0.3); background: rgba(239,68,68,0.08); color: #ef4444; cursor: pointer; font-family: inherit; }}
.pcs-remove:hover {{ background: rgba(239,68,68,0.18); }}
.pcs-hint {{ font-size: 10px; color: var(--text-tertiary); margin-top: 6px; }}
.picker-new-btn {{ padding: 6px 14px; font-size: 13px; font-weight: 600; border-radius: 6px; border: 1px solid rgba(59,130,246,0.4); background: rgba(59,130,246,0.15); color: var(--accent); cursor: pointer; font-family: inherit; }}
.picker-new-btn:hover {{ background: rgba(59,130,246,0.25); }}
</style>
<style>{rail_css}</style>
<style>{drawer_css}</style>
</head>
<body>
{rail_html}
{drawer_html}
<header class="picker-header">
  <h1>Projects</h1>
  <div style="display:flex;align-items:center;gap:14px;">
    <span class="count">{len(projects)} project{"s" if len(projects) != 1 else ""} registered</span>
    <button class="picker-new-btn" id="newProjectBtn" data-testid="add-project-card">+ New</button>
  </div>
</header>
<div class="picker-body">
<div class="grid">
  {cards_html}
</div>
<div id="add-modal-backdrop" class="add-modal-backdrop" data-testid="add-project-modal">
  <form id="add-form" class="add-form" data-testid="add-project-form">
    <button type="button" class="add-form-close" id="add-form-close" aria-label="Close" data-testid="add-project-close">&times;</button>
    <h2 class="add-form-title">Add a Project</h2>
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
</div>
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
        path: pathHidden.value
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

  // Add-Project modal: open/close helpers used by + New, ?new=1, switcher.
  var addBackdrop = document.getElementById('add-modal-backdrop');
  function openAddModal() {{
    if (!addBackdrop) return;
    addBackdrop.classList.add('visible');
    // Defer focus so the input is selectable after the overlay paints.
    setTimeout(function() {{
      var nm = document.querySelector('#add-form [name="name"]');
      if (nm) nm.focus();
    }}, 0);
  }}
  function closeAddModal() {{
    if (!addBackdrop) return;
    addBackdrop.classList.remove('visible');
    // Strip ?new=1 so refreshes don't re-open.
    if (new URLSearchParams(window.location.search).get('new') === '1') {{
      var url = new URL(window.location.href);
      url.searchParams.delete('new');
      history.replaceState({{}}, '', url.pathname + (url.search || ''));
    }}
  }}

  // "+ New" button opens the modal
  var newBtn = document.getElementById('newProjectBtn');
  if (newBtn) {{
    newBtn.addEventListener('click', function() {{ openAddModal(); }});
  }}

  // Close button + click-outside + Escape
  var closeBtn = document.getElementById('add-form-close');
  if (closeBtn) closeBtn.addEventListener('click', function() {{ closeAddModal(); }});
  if (addBackdrop) {{
    addBackdrop.addEventListener('click', function(e) {{
      if (e.target === addBackdrop) closeAddModal();
    }});
  }}
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape' && addBackdrop && addBackdrop.classList.contains('visible')) {{
      // Don't close if folder picker is the active layer
      var pickerOpen = document.getElementById('folder-picker').classList.contains('visible');
      if (!pickerOpen) closeAddModal();
    }}
  }});

  // Auto-open when ?new=1 is in the URL (e.g. from the rail switcher's "Add new project")
  if (new URLSearchParams(window.location.search).get('new') === '1') {{
    openAddModal();
  }}

  // Project card click → navigate to project's kanban (skip when clicking
  // the gear, the expanded settings panel, or when the path is missing —
  // navigating there would just dead-end on a missing project directory).
  document.querySelectorAll('.proj-card-main').forEach(function(main) {{
    main.addEventListener('click', function(e) {{
      if (e.target.closest('.proj-card-gear')) return;
      var card = main.closest('.proj-card');
      if (card && card.classList.contains('path-missing')) return;
      var pid = main.getAttribute('data-pid');
      if (pid) window.location.href = '/' + pid + '/kanban';
    }});
  }});

  // Gear click → toggle expanded settings panel
  document.querySelectorAll('.proj-card-gear').forEach(function(gear) {{
    gear.addEventListener('click', function(e) {{
      e.stopPropagation();
      var pid = gear.getAttribute('data-pid');
      var card = gear.closest('.proj-card');
      if (!card) return;
      var panel = card.querySelector('.proj-card-settings');
      if (!panel) return;
      var nowOpen = panel.hasAttribute('hidden');
      if (nowOpen) {{
        panel.removeAttribute('hidden');
        card.classList.add('expanded');
        loadManagedFilesFor(pid, card);
      }} else {{
        panel.setAttribute('hidden', '');
        card.classList.remove('expanded');
      }}
    }});
  }});

  function loadManagedFilesFor(pid, card) {{
    var container = card.querySelector('.pcs-managed-files');
    if (!container) return;
    fetch('/' + pid + '/api/managed-files')
      .then(function(r) {{ return r.json(); }})
      .then(function(files) {{
        while (container.firstChild) container.removeChild(container.firstChild);
        files.forEach(function(f) {{
          var row = document.createElement('div');
          row.className = 'pcs-mf-row';
          var dot = document.createElement('span');
          dot.className = 'pcs-mf-dot ' + (f.exists ? 'exists' : 'missing');
          dot.title = f.exists ? 'Exists' : 'Not created yet';
          var pathEl = document.createElement('span');
          pathEl.className = 'pcs-mf-path';
          pathEl.textContent = f.path;
          row.appendChild(dot);
          row.appendChild(pathEl);
          if (f.gitignored) {{
            var badge = document.createElement('span');
            badge.className = 'pcs-mf-badge';
            badge.textContent = '.gitignored';
            row.appendChild(badge);
          }}
          var desc = document.createElement('span');
          desc.className = 'pcs-mf-desc';
          desc.textContent = f.description;
          row.appendChild(desc);
          container.appendChild(row);
        }});
      }})
      .catch(function() {{}});
  }}

  // Save button per card
  document.querySelectorAll('.pcs-save').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var pid = btn.getAttribute('data-pid');
      var card = btn.closest('.proj-card');
      var panel = card.querySelector('.proj-card-settings');
      var msg = panel.querySelector('.pcs-msg');
      var nameVal = panel.querySelector('[data-field="name"]').value;
      var pathVal = panel.querySelector('[data-field="path"]').value;
      var activeVal = panel.querySelector('[data-field="active"]').checked;
      msg.textContent = '';
      msg.className = 'pcs-msg';
      fetch('/api/projects/' + pid, {{
        method: 'PUT',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ name: nameVal, path: pathVal, active: activeVal }})
      }}).then(function(r) {{ return r.json().then(function(j) {{ return {{ok: r.ok, data: j}}; }}); }})
      .then(function(res) {{
        if (res.ok) {{
          msg.textContent = 'Saved';
          msg.className = 'pcs-msg ok';
          var nameEl = card.querySelector('.proj-card-name');
          if (nameEl) nameEl.textContent = nameVal;
        }} else {{
          msg.textContent = (res.data && res.data.error) || 'Failed';
          msg.className = 'pcs-msg err';
        }}
      }}).catch(function() {{
        msg.textContent = 'Network error';
        msg.className = 'pcs-msg err';
      }});
    }});
  }});

  // Remove button per card
  document.querySelectorAll('.pcs-remove').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var pid = btn.getAttribute('data-pid');
      if (!confirm('Remove project "' + pid + '"? Tickets and files will not be deleted.')) return;
      fetch('/api/projects/' + pid, {{ method: 'DELETE' }})
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
          if (data && data.ok) window.location.reload();
          else alert((data && data.error) || 'Failed to remove');
        }});
    }});
  }});
}})();
</script>
</div>
<script>{rail_js}</script>
<script>{drawer_js}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# Module-level state — shared across all handlers in this process.
# Tracks send-keys timestamps per pane for the per-pane rate limiter.
_PANE_SEND_RATE: dict[str, list[float]] = {}


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
        self.send_header(
            "Access-Control-Allow-Origin", f"http://localhost:{SERVER_PORT}"
        )
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

    def _send_typed_error(self, exc: Exception) -> None:
        """Map a typed AppError to its HTTP status; fall back to 500 otherwise.

        Use this in handlers wrapping an actions.py call that may raise
        TicketNotFoundError, ValidationError, ConflictError, etc. The body
        always includes both ``code`` and ``error`` fields so JS callers can
        branch on the code rather than parse messages.
        """
        if isinstance(exc, AppError):
            self._send_json(
                {"code": exc.code, "error": str(exc) or exc.code},
                status=exc.http_status,
            )
        else:
            self._send_json(
                {"code": "internal_error", "error": str(exc) or "Internal error"},
                status=500,
            )

    def _read_body(self) -> dict:
        length = min(int(self.headers.get("Content-Length", 0)), 1_048_576)  # 1 MB cap
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _check_send_keys_rate(self, pane_addr: str) -> bool:
        """Token-bucket rate limiter for send-keys: max PANE_SEND_KEYS_RATE_PER_S per pane per second."""
        import time as _t

        from constants import PANE_SEND_KEYS_RATE_PER_S

        now = _t.time()
        bucket = _PANE_SEND_RATE.setdefault(pane_addr, [])
        # Drop timestamps older than 1 second
        bucket[:] = [t for t in bucket if now - t < 1.0]
        if len(bucket) >= PANE_SEND_KEYS_RATE_PER_S:
            return False
        bucket.append(now)
        return True

    # ── PWA static assets ───────────────────────────────────────────
    # SW scope must be the site root for it to intercept project-scoped
    # navigations, so manifest/sw/icons all live above the routing layer.
    _STATIC_DIR = Path(__file__).parent / "static"
    _STATIC_ROUTES: ClassVar[dict[str, tuple[str, str]]] = {
        "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
        "/sw.js": ("sw.js", "application/javascript"),
        "/icon.svg": ("icon.svg", "image/svg+xml"),
        "/icon-180.png": ("icon-180.png", "image/png"),
        "/icon-192.png": ("icon-192.png", "image/png"),
        "/icon-512.png": ("icon-512.png", "image/png"),
    }

    def _try_send_static(self, remainder: str) -> bool:
        """Serve a PWA static asset if remainder matches. Returns True if handled."""
        entry = self._STATIC_ROUTES.get(remainder)
        if entry is None:
            return False
        filename, content_type = entry
        path = self._STATIC_DIR / filename
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return True
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Service worker must register at root scope, even when sw.js is fetched
        # via a path prefix from a future reverse proxy.
        if remainder == "/sw.js":
            self.send_header("Service-Worker-Allowed", "/")
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Origin", f"http://localhost:{SERVER_PORT}"
        )
        self.send_header(
            "Access-Control-Allow-Methods", "GET, PUT, POST, DELETE, OPTIONS"
        )
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        proj, remainder = _resolve_project_from_path(path)

        # ── Global routes (proj is None) ────────────────────────────
        if proj is None:
            # PWA static assets — must be served at root for the service worker
            # to claim site-wide scope. Returns True if remainder was handled.
            if self._try_send_static(remainder):
                return

            # Root: redirect to Projects (the picker is the cross-project landing).
            if remainder == "/" or remainder == "":
                self.send_response(302)
                self.send_header("Location", "/projects")
                self.end_headers()
                return

            # Kitchen — cross-project work surface (was at "/" prior to /kitchen move)
            if remainder == "/kitchen":
                html = _render_kitchen_view(SERVER_PORT)
                self._send_html(html)
                return

            # Workflows — cross-project list with scope badges (system / global / project)
            if remainder == "/workflows":
                html = _render_workflows_view(SERVER_PORT)
                self._send_html(html)
                return

            # Project picker
            if remainder == "/projects":
                html = _render_project_picker(SERVER_PORT)
                self._send_html(html)
                return

            # Kitchen JSON aggregation (M2)
            if remainder == "/api/kitchen":
                state = _aggregate_kitchen_state()
                state["paused"] = _kitchen.is_paused()
                self._send_json(state)
                return

            # Kitchen control surface (M6) — pause / resume / state.
            if remainder == "/api/kitchen/state":
                self._send_json(
                    {
                        "paused": _kitchen.is_paused(),
                        "active_runs": len(_kitchen.active_runs_snapshot()),
                    }
                )
                return

            # Attention feed payload — drives the redesigned /kitchen page.
            # Polled every ~5s by the client to refresh chip counts + items.
            if remainder == "/api/kitchen/feed":
                with _PROJECTS_CACHE_LOCK:
                    projects = list(_PROJECTS_CACHE.values())
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    feed = kitchen_feed.build_attention_feed(
                        conn,
                        projects,
                        is_paused=_kitchen.is_paused(),
                    )
                    conn.close()
                self._send_json(feed)
                return

            # Global activity feed — drives kanban Follow mode. Polled ~2s by
            # boards with Follow enabled. since_id absent -> cursor init only.
            if remainder.split("?", 1)[0] == "/api/activity/feed":
                query = urlparse(self.path).query
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                try:
                    since_id = int(params["since_id"]) if "since_id" in params else None
                except ValueError:
                    since_id = None
                try:
                    limit = int(params.get("limit", "100"))
                except ValueError:
                    limit = 100
                with _PROJECTS_CACHE_LOCK:
                    projects = list(_PROJECTS_CACHE.values())
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    feed = _actions_get_activity_feed(
                        conn, since_id=since_id, limit=limit, projects=projects
                    )
                    conn.close()
                self._send_json(feed)
                return

            # Visual preview of the attention feed with stub data — lets us
            # verify the populated layout when the live DB is empty.
            if remainder == "/kitchen/demo":
                self._send_html(
                    kitchen_view.render_attention_feed(
                        _DEMO_KITCHEN_STATE,
                        port=SERVER_PORT,
                        rail_css=gen.build_nav_rail_css(),
                        rail_html=gen.build_nav_rail_html(),
                        rail_js=gen.build_nav_rail_js(),
                        pwa_head_tags=PWA_HEAD_TAGS,
                    )
                )
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
                    result.append(
                        {
                            "id": p["id"],
                            "name": p.get("name", p["id"]),
                            "path": p.get("path", ""),
                            "active": p.get("active", True),
                            "ticket_counts": {
                                "wip": c.get("WIP", 0),
                                "backlog": c.get("Backlog", 0),
                                "review": c.get("For Review", 0),
                            },
                        }
                    )
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
                    self._send_json(
                        {"error": "path must be within home directory"}, 400
                    )
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
                self._send_json(
                    {"path": display, "absolute": str(resolved), "dirs": dirs}
                )
                return

            # Phase 3A: condition catalog (project-agnostic)
            if remainder == "/api/workflow-conditions/catalog":
                catalog = []
                for kind, entry in _conditions.CONDITION_CATALOG.items():
                    catalog.append(
                        {
                            "kind": kind,
                            "label": entry.get("label", kind),
                            "params": entry.get("params", []),
                        }
                    )
                self._send_json({"conditions": catalog})
                return

            # Global settings (theme/feedbacks) — same data on every page,
            # so unprefixed /api/settings + /api/feedbacks/status work even
            # when there's no project context.
            if remainder == "/api/settings":
                self._send_json(_get_all_settings())
                return
            if remainder == "/api/feedbacks/status":
                self._send_json(_detect_feedbacks())
                return

            # Global workflow agents — custom agents are project-agnostic
            # (workflow_agents has no project_id column). Used by /workflows page.
            if remainder == "/api/workflow/agents":
                custom = _list_workflow_agents()
                for a in custom:
                    a["source"] = "custom"
                    a["editable"] = True
                self._send_json({"agents": custom})
                return

            # Global workflows list — all workflows across all projects.
            if remainder == "/api/workflow/workflows":
                self._send_json({"workflows": _list_workflows()})
                return

            # Unified attribute catalog for the workflow editor — exposes
            # every filterable attribute with its filter ops + matching
            # action ops. UI walks this to render the (Attribute, Operation,
            # Value) row dropdowns. See conditions.ui_catalog().
            if remainder == "/api/workflow/catalog":
                from conditions import ui_catalog

                self._send_json(ui_catalog())
                return

            # Linter: given a candidate trigger_json + on_success_json, report
            # whether at least one action mutates an attribute the trigger
            # reads (closed-loop principle). UI calls this on edit and on save.
            if remainder == "/api/workflow/lint":
                # Body via query string for GET — but most callers POST below.
                # Fall through to POST handler.
                self._send_json({"error": "Use POST /api/workflow/lint"}, 405)
                return

            # Live preview: how many tickets currently match this workflow's
            # trigger? Returns count + sample tickets across all linked projects.
            # Sized for inline display on the /workflows page — capped to keep
            # the response under ~5 KB even on large boards.
            m = re.match(
                r"^/api/workflow/workflows/([a-z0-9][a-z0-9_:.%-]*)/preview$", remainder
            )
            if m:
                workflow_id = m.group(1)
                wf = _get_workflow(workflow_id)
                if not wf:
                    self._send_json({"error": "Workflow not found"}, 404)
                    return
                preview = _preview_workflow_matches(wf)
                self._send_json(preview)
                return

            # GET /api/endpoints — list all configured model endpoints
            if remainder == "/api/endpoints":
                from endpoints import list_endpoints

                conn = get_db()
                init_db(conn)
                eps = [vars(ep) for ep in list_endpoints(conn)]
                conn.close()
                self._send_json({"endpoints": eps})
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

        # Legacy /settings route — redirect to dashboard (settings now live in the drawer).
        if remainder == "/settings":
            self.send_response(302)
            self.send_header("Location", f"/{proj['id']}/")
            self.end_headers()
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

        # Lane B: Full-page ticket view — GET /{project_id}/tickets/{ticket_id}?tab=
        m = re.match(r"^/tickets/([A-Za-z0-9_-]+)$", remainder)
        if m:
            ticket_id = m.group(1)
            qs = parse_qs(urlparse(self.path).query)
            tab = (qs.get("tab", ["overview"])[0] or "overview").strip()
            html = _render_ticket_page(proj, SERVER_PORT, ticket_id, tab)
            if html is None:
                self._send_json({"error": "Ticket not found"}, 404)
                return
            self._send_html(html)
            return

        # Serve dashboard HTML
        if remainder == "/" or remainder == "/index.html" or remainder == "/kanban":
            html_path = (
                Path(os.path.expanduser(proj.get("path", "")))
                / "docs"
                / "sdlc-dashboard.html"
            )
            if html_path.exists():
                html = html_path.read_text(encoding="utf-8")
                # Inject edit-api meta tag if not present
                if '<meta name="edit-api"' not in html:
                    idx = html.find('<meta name="gen-ts"')
                    if idx != -1:
                        with _PROJECTS_CACHE_LOCK:
                            proj_list = [
                                {"id": p["id"], "name": p.get("name", p["id"])}
                                for p in _PROJECTS_CACHE.values()
                            ]
                        projects_json = json.dumps(proj_list)
                        injection = (
                            f'<meta name="edit-api" content="http://localhost:{SERVER_PORT}/{_safe_attr(proj["id"])}/api">\n'
                            f'<meta name="current-project" content="{_safe_attr(proj["id"])}">\n'
                            f"<meta name=\"projects-list\" content='{_safe_attr(projects_json)}'>\n"
                        )
                        html = html[:idx] + injection + html[idx:]
                # Inject nav rail if absent (e.g. dashboard was generated before
                # the rail feature shipped; without this, stale HTML files
                # render menuless until the project is regenerated).
                if 'id="navRail"' not in html and "</body>" in html:
                    rail_inject = (
                        f"<style>{gen.build_nav_rail_css()}</style>\n"
                        f"{gen.build_nav_rail_html()}\n"
                        f"<script>{gen.build_nav_rail_js()}</script>\n"
                    )
                    html = html.replace("</body>", f"{rail_inject}</body>", 1)
                self._send_html(html)
            else:
                self._send_json(
                    {"error": "Dashboard not generated yet. Run generate.py first."},
                    404,
                )
            return

        # JSON tickets API
        if remainder == "/api/tickets":
            project_id = proj["id"]
            conn = get_db()
            init_db(conn)
            rows = conn.execute(
                "SELECT id FROM tickets WHERE project_id = ? ORDER BY sort_order ASC",
                (project_id,),
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

        # Spec tab payload
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/spec$", remainder)
        if m:
            payload = _spec_tab_payload(proj, m.group(1))
            if payload is None:
                self._send_json({"error": "Ticket not found"}, 404)
            else:
                self._send_json(payload)
            return

        # Spec document read
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/spec/doc$", remainder)
        if m:
            query = urlparse(self.path).query
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            rel_path = unquote(params.get("path", "") or "")
            if not rel_path:
                self._send_json({"error": "path query parameter required"}, 400)
                return
            body, status = _spec_doc_read(proj, m.group(1), rel_path)
            self._send_json(body, status)
            return

        # Bookmarks + Recents (I-43)
        if remainder == "/api/bookmarks":
            with _db_lock:
                conn = get_db()
                init_db(conn)
                items = _actions_list_bookmarks(conn, proj["id"])
                conn.close()
            self._send_json({"bookmarks": items})
            return

        if remainder == "/api/recents":
            with _db_lock:
                conn = get_db()
                init_db(conn)
                items = _actions_list_recents(conn, proj["id"])
                conn.close()
            self._send_json({"recents": items})
            return

        # All tags in this project (with counts)
        if remainder == "/api/tags":
            project_id = proj["id"]
            conn = get_db()
            init_db(conn)
            rows = conn.execute(
                "SELECT tag, COUNT(*) AS cnt FROM ticket_tags WHERE project_id = ? GROUP BY tag ORDER BY tag",
                (project_id,),
            ).fetchall()
            conn.close()
            self._send_json(
                {"tags": [{"tag": r["tag"], "count": r["cnt"]} for r in rows]}
            )
            return

        # Branch overview: all remote branches + linked tickets
        if remainder == "/api/branches/overview":
            project_id = proj["id"]
            project_path = os.path.expanduser(proj.get("path", ""))
            # Get remote branches
            remote_branches = []
            try:
                result = subprocess.run(
                    ["git", "branch", "-r", "--list", "origin/*"],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        name = line.strip()
                        if " -> " in name or not name:
                            continue
                        short = (
                            name.replace("origin/", "", 1)
                            if name.startswith("origin/")
                            else name
                        )
                        remote_branches.append(short)
            except Exception:
                pass

            # Get all linked branches with their tickets
            with _db_lock:
                conn = get_db()
                init_db(conn)
                links = conn.execute(
                    "SELECT tb.branch_name, tb.ticket_id, tb.pr_number, tb.pr_status, tb.pr_url, "
                    "tb.ahead, tb.behind, t.title, t.status, t.priority, t.section "
                    "FROM ticket_branches tb "
                    "LEFT JOIN tickets t ON tb.ticket_id = t.id AND tb.project_id = t.project_id "
                    "WHERE tb.project_id = ? ORDER BY tb.branch_name, t.sort_order",
                    (project_id,),
                ).fetchall()
                conn.close()

            # Build branch-centric view
            branch_map = {}
            for link in links:
                bname = link["branch_name"]
                if bname not in branch_map:
                    branch_map[bname] = {
                        "name": bname,
                        "pr_number": link["pr_number"],
                        "pr_status": link["pr_status"],
                        "pr_url": link["pr_url"],
                        "ahead": link["ahead"],
                        "behind": link["behind"],
                        "tickets": [],
                    }
                if link["ticket_id"]:
                    branch_map[bname]["tickets"].append(
                        {
                            "id": link["ticket_id"],
                            "title": link["title"] or "",
                            "status": link["status"] or "",
                            "priority": link["priority"] or "medium",
                            "section": link["section"] or "",
                        }
                    )

            # Add remote branches that have no links
            for rb in remote_branches:
                if rb not in branch_map:
                    branch_map[rb] = {
                        "name": rb,
                        "pr_number": None,
                        "pr_status": "",
                        "pr_url": "",
                        "ahead": 0,
                        "behind": 0,
                        "tickets": [],
                    }

            # Sort: branches with tickets first, then alphabetical
            branches = sorted(
                branch_map.values(), key=lambda b: (len(b["tickets"]) == 0, b["name"])
            )
            self._send_json({"branches": branches})
            return

        # Branch links for this project
        if remainder == "/api/branches":
            project_id = proj["id"]
            params = parse_qs(urlparse(self.path).query)
            ticket_filter = params.get("ticket_id", [None])[0]
            with _db_lock:
                conn = get_db()
                init_db(conn)
                if ticket_filter:
                    rows = _actions_get_ticket_branches(conn, project_id, ticket_filter)
                else:
                    rows = _actions_get_project_branches(conn, project_id)
                conn.close()
            self._send_json({"branches": rows})
            return

        # Settings
        # Phase 3A: kitchen settings
        if remainder == "/api/settings/kitchen":
            self._send_json({"settings": _get_kitchen_settings()})
            return

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

        # Lane B: paginated activity feed for a ticket.
        # GET /api/tickets/{id}/activity?limit=50&before=ISO
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/activity$", remainder)
        if m:
            ticket_id = m.group(1)
            qs = parse_qs(urlparse(self.path).query)
            try:
                limit = max(1, min(int(qs.get("limit", ["50"])[0]), 200))
            except (ValueError, TypeError):
                limit = 50
            before_cursor = (qs.get("before", [""])[0] or "").strip() or None
            with _db_lock:
                conn = get_db()
                init_db(conn)
                from actions import get_ticket_activity as _get_ticket_activity

                events = _get_ticket_activity(
                    conn, proj["id"], ticket_id, limit=limit, before=before_cursor
                )
                conn.close()
            next_before = events[-1]["occurred_at"] if len(events) == limit else None
            self._send_json({"events": events, "next_before": next_before})
            return

        # Kitchen (M1b): activity history for a ticket. Newest first.
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/history$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                limit = int(
                    parse_qs(urlparse(self.path).query).get("limit", ["100"])[0]
                )
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
                events.append(
                    {
                        "id": r["id"],
                        "actor_type": r["actor_type"],
                        "actor_id": r["actor_id"],
                        "event_kind": r["event_kind"],
                        "payload": payload,
                        "occurred_at": r["occurred_at"],
                        "discarded_run_id": r["discarded_run_id"],
                    }
                )
            self._send_json({"events": events})
            return

        # Phase 3A: active kitchen runs for this project
        if remainder == "/api/runs/active":
            runs = _get_active_kitchen_runs(proj["id"])
            self._send_json({"runs": runs})
            return

        # Project-scoped list of tickets with mode='paused' — feeds the Live
        # tab's "Paused (auto on, not dispatching)" zone.
        if remainder == "/api/automation/paused":
            with _db_lock:
                conn = get_db()
                init_db(conn)
                rows = conn.execute(
                    "SELECT s.subject_id AS ticket_id, s.pause_reason, "
                    "       s.updated_at, t.title "
                    "FROM automation_subjects s "
                    "LEFT JOIN tickets t "
                    "       ON t.id = s.subject_id "
                    "      AND t.project_id = s.project_id "
                    "WHERE s.project_id = ? "
                    "  AND s.subject_type = 'ticket' "
                    "  AND s.automation_mode = 'paused' "
                    "ORDER BY s.updated_at DESC",
                    (proj["id"],),
                ).fetchall()
                conn.close()
            self._send_json(
                {
                    "paused": [
                        {
                            "ticket_id": r["ticket_id"],
                            "title": r["title"] or "",
                            "pause_reason": r["pause_reason"] or "",
                            "updated_at": r["updated_at"],
                        }
                        for r in rows
                    ]
                }
            )
            return

        # Phase 3A: recent finished kitchen runs for this project
        if remainder.startswith("/api/runs/recent"):
            qs = parse_qs(urlparse(self.path).query)
            try:
                limit = max(1, min(int(qs.get("limit", ["25"])[0]), 100))
            except (ValueError, TypeError):
                limit = 25
            runs = _get_recent_kitchen_runs(proj["id"], limit)
            self._send_json({"runs": runs})
            return

        # Phase 3A: run evidence file listing
        m = re.match(r"^/api/runs/(\d+)/evidence$", remainder)
        if m:
            run_id = int(m.group(1))
            files = _get_run_evidence(proj["id"], run_id)
            if files is None:
                self._send_json({"error": "run not found"}, 404)
                return
            self._send_json({"files": files})
            return

        # Kitchen (M3): list runs for a ticket. Newest first. Powers the live run panel.
        # Path: /api/runs?ticket={id}&limit={n}  (limit defaults 10, max 50)
        if remainder.startswith("/api/runs?") or remainder == "/api/runs":
            qs = parse_qs(urlparse(self.path).query)
            ticket_id = (qs.get("ticket", [""])[0] or "").strip()
            try:
                limit = max(1, min(int(qs.get("limit", ["10"])[0]), 50))
            except (ValueError, TypeError):
                limit = 10
            if not ticket_id:
                self._send_json({"runs": []})
                return
            with _db_lock:
                conn = get_db()
                init_db(conn)
                row = conn.execute(
                    "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
                    (ticket_id, proj["id"]),
                ).fetchone()
                tid = row["id"] if row else ticket_id
                rows = conn.execute(
                    "SELECT id, project_id, subject_type, subject_id, runner_kind, status, "
                    "       workspace_path, started_at, finished_at, duration_ms, "
                    "       error_class, error_message, summary, needs_input_prompt, "
                    "       attempt, triggered_by "
                    "FROM runs WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (proj["id"], tid, limit),
                ).fetchall()
                conn.close()
            self._send_json({"runs": [dict(r) for r in rows]})
            return

        # Kitchen (M3 + Phase 3A): single run detail with activity events.
        m = re.match(r"^/api/runs/(\d+)$", remainder)
        if m:
            run_id = int(m.group(1))
            detail = _get_kitchen_run_detail(proj["id"], run_id)
            if not detail:
                self._send_json({"error": "run not found"}, 404)
                return
            self._send_json(detail)
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
            self._send_json({"workflows": _list_workflows(project_id=proj["id"])})
            return

        # Active workflow runs across all tickets (for kanban indicators)
        if remainder == "/api/workflow/runs/active":
            with _db_lock:
                conn = get_db()
                init_db(conn)
                rows = conn.execute(
                    "SELECT id, ticket_id, status FROM workflow_runs WHERE project_id = ? AND status IN ('running', 'paused')",
                    (proj["id"],),
                ).fetchall()
                conn.close()
            self._send_json({"runs": [dict(r) for r in rows]})
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
            run_id = m.group(1)
            run = _get_workflow_run(run_id, project_id=proj["id"])
            if not run:
                self._send_json({"error": "Run not found"}, 404)
                return
            with _workflow_runs_lock:
                mem = _workflow_runs.get(run_id)
            if mem:
                run["status"] = mem.get("status", run["status"])
                if "current_step" in mem:
                    run["current_step"] = mem["current_step"]
            # Detect dead thread: DB says running but no thread in memory
            if run.get("status") == "running" and not mem:
                _update_workflow_run(
                    run_id, status="failed", completed_at=datetime.utcnow().isoformat()
                )
                run["status"] = "failed"
            self._send_json(run)
            return

        # Scenario API: serve artifact files (must come before run status check)
        if remainder.startswith("/api/scenarios/runs/") and "/artifacts/" in remainder:
            parts = remainder[len("/api/scenarios/runs/") :].split("/artifacts/", 1)
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
            run_id = remainder[len("/api/scenarios/runs/") :]
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
                manifests = (
                    discover_scenarios(scenarios_dir)
                    if os.path.isdir(scenarios_dir)
                    else []
                )
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
                return
            # Attach last run status if available
            with _scenario_runs_lock:
                for manifest in manifests:
                    for rid, run in _scenario_runs.items():
                        if run["scenario_id"] == manifest["id"]:
                            manifest["last_run"] = {
                                "run_id": rid,
                                "status": run["status"],
                                "started_at": run.get("started_at"),
                            }
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
            # Finalize any in-flight runs for this journey so the runs list
            # reflects up-to-date statuses.
            with _scenario_runs_lock:
                pending = [
                    rid
                    for rid, run in _scenario_runs.items()
                    if run.get("journey_id") == journey_id and not run.get("_finalized")
                ]
            for rid in pending:
                _finalize_journey_run(rid)
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    journey = get_journey(conn, project_id, journey_id)
                    conn.close()
                self._send_json(journey)
            except ValueError as e:
                self._send_typed_error(e)
            return

        # Journey API: get run details
        m = re.match(
            r"^/api/journeys/([A-Za-z0-9_-]+)/runs/([A-Za-z0-9_-]+)$", remainder
        )
        if m:
            journey_id = m.group(1)
            run_id = m.group(2)
            # Lazily finalize if the subprocess just exited.
            _finalize_journey_run(run_id)
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
                step_results_list = [dict(sr) for sr in step_results]
                conn.close()
            self._send_json({"run": run_dict, "step_results": step_results_list})
            return

        # Journey API: serve run screenshot
        m = re.match(
            r"^/api/journeys/([A-Za-z0-9_-]+)/runs/([A-Za-z0-9_-]+)/screenshots/(.+\.png)$",
            remainder,
        )
        if m:
            journey_id, run_id, filename = m.group(1), m.group(2), m.group(3)
            if "/" in filename or "\\" in filename or ".." in filename:
                self._send_json({"error": "Invalid filename"}, 400)
                return
            # Look up artifact_dir from DB (run_id and dir basename may differ)
            with _db_lock:
                conn = get_db()
                init_db(conn)
                row = conn.execute(
                    "SELECT artifact_dir FROM journey_runs WHERE id = ?", (run_id,)
                ).fetchone()
                conn.close()
            if row and row["artifact_dir"]:
                screenshot_path = os.path.join(row["artifact_dir"], filename)
            else:
                project_path = proj.get("path", "")
                screenshot_path = os.path.join(
                    project_path, ".artifacts", "journeys", journey_id, run_id, filename
                )
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
                self._send_json(
                    {
                        "screens": [],
                        "hint": "No scan yet. POST /api/screens/scan to discover pages.",
                    }
                )
            return

        # Pane links: GET /<pid>/api/tickets/<tid>/pane-links
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/pane-links$", remainder)
        if m:
            tid = m.group(1)
            import pane_links as _pl

            with _db_lock:
                conn = get_db()
                init_db(conn)
                rows = _pl.list_pane_links_for_ticket(conn, proj["id"], tid)
                result = [dict(r) for r in rows]
                conn.close()
            self._send_json({"pane_links": result})
            return

        self._send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        path = unquote(urlparse(self.path).path)
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
                except (OSError, json.JSONDecodeError):
                    self._send_json({"error": "Registry not found"}, 500)
                    return
                found = False
                updated_entry = None
                for entry in registry["projects"]:
                    if entry["id"] == pid:
                        # M2-03: 'watched' is a Kitchen-aggregator filter flag.
                        for field in ("name", "path", "active", "watched"):
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

            # Global settings update (theme/feedbacks).
            if remainder == "/api/settings":
                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError):
                    self._send_json({"error": "Invalid JSON"}, 400)
                    return
                _set_settings(body)
                self._send_json({"ok": True})
                return

            # Global update of a workflow agent — workflow_agents has no
            # project_id column, so the project-scoped handler logic mirrors here.
            # System agents are read-only (definition lives in workflows_seed.py).
            m = re.match(r"^/api/workflow/agents/([a-z0-9][a-z0-9_-]*)$", remainder)
            if m:
                agent_id = m.group(1)
                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError):
                    self._send_json({"error": "Invalid JSON"}, 400)
                    return
                # System agents: allow endpoint_id changes (orthogonal config), block all other fields.
                with _db_lock:
                    _ag_conn = get_db()
                    init_db(_ag_conn)
                    _ag_row = _ag_conn.execute(
                        "SELECT system FROM workflow_agents WHERE id = ?",
                        (agent_id,),
                    ).fetchone()
                    _ag_conn.close()
                if _ag_row and int(_ag_row["system"] or 0) == 1:
                    allowed_fields = {"endpoint_id"}
                    forbidden = set(body.keys()) - allowed_fields
                    if forbidden:
                        self._send_json(
                            {
                                "error": "system_agent",
                                "forbidden_fields": sorted(forbidden),
                            },
                            403,
                        )
                        return
                if isinstance(body.get("args"), list):
                    body["args"] = json.dumps(body["args"])
                updated = _update_workflow_agent(agent_id, body)
                if updated:
                    self._send_json(updated)
                else:
                    self._send_json({"error": "Agent not found"}, 404)
                return

            # Global update of a workflow — system rows may only toggle 'enabled'.
            m = re.match(
                r"^/api/workflow/workflows/([a-z0-9][a-z0-9_:.%-]*)$", remainder
            )
            if m:
                workflow_id = m.group(1)
                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError):
                    self._send_json({"error": "Invalid JSON"}, 400)
                    return
                existing = _get_workflow(workflow_id)
                if not existing:
                    self._send_json({"error": "Workflow not found"}, 404)
                    return
                if existing.get("system"):
                    non_enabled_keys = {k for k in body if k != "enabled"}
                    if non_enabled_keys:
                        self._send_json({"error": "system_workflow"}, 403)
                        return
                if "steps" in body and isinstance(body["steps"], list):
                    body["steps"] = json.dumps(body["steps"])
                for field in ("trigger_json", "on_success_json"):
                    if (
                        field in body
                        and body[field] is not None
                        and not isinstance(body[field], str)
                    ):
                        body[field] = json.dumps(body[field])
                updated = _update_workflow(workflow_id, body)
                if updated:
                    self._send_json(updated)
                else:
                    self._send_json({"error": "Workflow not found"}, 404)
                return

            # PUT /api/endpoints/{id} — update a user endpoint
            m = re.match(r"^/api/endpoints/([a-zA-Z0-9_-]+)$", remainder)
            if m:
                endpoint_id = m.group(1)
                from endpoints import EndpointMisconfigured, update_endpoint

                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError) as e:
                    self._send_json({"error": str(e)}, 400)
                    return
                conn = get_db()
                init_db(conn)
                try:
                    updated = update_endpoint(conn, endpoint_id, **body)
                except KeyError:
                    conn.close()
                    self._send_json({"error": "endpoint not found"}, 404)
                    return
                except PermissionError:
                    conn.close()
                    self._send_json({"error": "system_endpoint"}, 403)
                    return
                except EndpointMisconfigured as e:
                    conn.close()
                    self._send_json({"error": str(e)}, 400)
                    return
                conn.close()
                self._send_json(vars(updated))
                return

            if _LEGACY_PROJECT_ID and remainder.startswith("/api/"):
                self.send_response(301)
                self.send_header("Location", f"/{_LEGACY_PROJECT_ID}{remainder}")
                self.end_headers()
                return
            self._send_json({"error": "Not found"}, 404)
            return

        # ── Project-scoped routes ────────────────────────────────────

        # Phase 3A: kitchen settings update
        if remainder == "/api/settings/kitchen":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            if not isinstance(body, dict):
                self._send_json({"error": "Body must be a JSON object"}, 400)
                return
            settings, err = _set_kitchen_settings(body)
            if err:
                self._send_json({"error": err}, 400)
                return
            self._send_json({"settings": settings})
            return

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

        # Spec document write
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/spec/doc$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            if not isinstance(body, dict):
                self._send_json({"error": "Body must be a JSON object"}, 400)
                return
            rel_path = body.get("path", "") or ""
            content = body.get("content", "")
            if content is None:
                content = ""
            if not rel_path:
                self._send_json({"error": "path is required"}, 400)
                return
            result, status = _spec_doc_write(proj, ticket_id, rel_path, content)
            self._send_json(result, status)
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

        # Update workflow (Phase 3A: system workflow guard + new fields)
        m = re.match(r"^/api/workflow/workflows/([a-z0-9][a-z0-9_:.%-]*)$", remainder)
        if m:
            workflow_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            # 403 guard: system workflows may only have 'enabled' toggled
            existing = _get_workflow(workflow_id, project_id=proj["id"])
            if not existing:
                self._send_json({"error": "Workflow not found"}, 404)
                return
            if existing.get("system"):
                non_enabled_keys = {k for k in body if k != "enabled"}
                if non_enabled_keys:
                    self._send_json({"error": "system_workflow"}, 403)
                    return
            # Auto-serialize steps list to JSON
            if "steps" in body and isinstance(body["steps"], list):
                body["steps"] = json.dumps(body["steps"])
            # Accept trigger_json / on_success_json as object or string
            for field in ("trigger_json", "on_success_json"):
                if (
                    field in body
                    and body[field] is not None
                    and not isinstance(body[field], str)
                ):
                    body[field] = json.dumps(body[field])
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
            if (
                isinstance(idx, int)
                and isinstance(text, str)
                and _update_criterion_text(proj, ticket_id, idx, text)
            ):
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
            if (
                isinstance(text, str)
                and text.strip()
                and _add_criterion(proj, ticket_id, text.strip())
            ):
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

        # Handle tag operations
        if "add_tag" in body or "remove_tag" in body:
            add_tag = body.get("add_tag")
            remove_tag = body.get("remove_tag")
            add_tags = (
                [add_tag] if isinstance(add_tag, str) and add_tag.strip() else None
            )
            remove_tags = (
                [remove_tag]
                if isinstance(remove_tag, str) and remove_tag.strip()
                else None
            )
            with _db_lock:
                conn = get_db()
                init_db(conn)
                cli.ingest_markdown(conn, proj)
                try:
                    _actions_update_ticket(
                        conn,
                        project_id,
                        ticket_id,
                        add_tags=add_tags,
                        remove_tags=remove_tags,
                    )
                    conn.commit()
                    cli.sync_to_markdown(conn, proj)
                    cli.regenerate_dashboard(proj)
                except (ValueError, IndexError):
                    pass
                conn.close()
            t = _get_ticket_json(project_id, ticket_id)
            self._send_json(t or {"ok": True})
            return

        # Handle set_tags (replace all tags)
        if "set_tags" in body:
            new_tags = body["set_tags"]
            if isinstance(new_tags, list):
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    cli.ingest_markdown(conn, proj)
                    row = conn.execute(
                        "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
                        (ticket_id, project_id),
                    ).fetchone()
                    if row:
                        tid = row["id"]
                        conn.execute(
                            "DELETE FROM ticket_tags WHERE ticket_id = ? AND project_id = ?",
                            (tid, project_id),
                        )
                        for tag in new_tags:
                            tag = tag.strip().lower() if isinstance(tag, str) else ""
                            if tag:
                                conn.execute(
                                    "INSERT OR IGNORE INTO ticket_tags (ticket_id, project_id, tag) VALUES (?, ?, ?)",
                                    (tid, project_id, tag),
                                )
                        conn.commit()
                        cli.sync_to_markdown(conn, proj)
                        cli.regenerate_dashboard(proj)
                    conn.close()
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
                return

        # Handle branch operations
        if "add_branch" in body or "remove_branch" in body:
            add_br = body.get("add_branch")
            remove_br = body.get("remove_branch")
            add_branches = (
                [add_br] if isinstance(add_br, str) and add_br.strip() else None
            )
            remove_branches = (
                [remove_br]
                if isinstance(remove_br, str) and remove_br.strip()
                else None
            )
            with _db_lock:
                conn = get_db()
                init_db(conn)
                cli.ingest_markdown(conn, proj)
                try:
                    _actions_update_ticket(
                        conn,
                        project_id,
                        ticket_id,
                        add_branches=add_branches,
                        remove_branches=remove_branches,
                    )
                    conn.commit()
                    cli.sync_to_markdown(conn, proj)
                    cli.regenerate_dashboard(proj)
                except (ValueError, IndexError):
                    pass
                conn.close()
            t = _get_ticket_json(project_id, ticket_id)
            self._send_json(t or {"ok": True})
            return

        # Handle is_container toggle (Lane B — container ticket flag)
        if "is_container" in body:
            val = 1 if body["is_container"] else 0
            with _db_lock:
                conn = get_db()
                init_db(conn)
                row = conn.execute(
                    "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
                    (ticket_id, project_id),
                ).fetchone()
                if row:
                    tid = row["id"]
                    conn.execute(
                        "UPDATE tickets SET is_container = ?, updated_at = ? "
                        "WHERE id = ? AND project_id = ?",
                        (val, datetime.now().isoformat(), tid, project_id),
                    )
                    _kitchen_emit_event(
                        conn,
                        project_id,
                        "ticket",
                        tid,
                        "field_changed",
                        {"field": "is_container", "before": 1 - val, "after": val},
                        ActorContext.human(),
                    )
                    conn.commit()
                    cli.sync_to_markdown(conn, proj)
                    cli.regenerate_dashboard(proj)
                conn.close()
            t = _get_ticket_json(project_id, ticket_id)
            self._send_json(t or {"ok": True})
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
        path = unquote(urlparse(self.path).path)
        proj, remainder = _resolve_project_from_path(path)

        # ── Global routes ───────────────────────────────────────────
        if proj is None:
            # POST /api/endpoints — create a user endpoint
            if remainder == "/api/endpoints":
                from endpoints import (
                    Endpoint,
                    EndpointMisconfigured,
                    create_endpoint,
                )

                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError) as e:
                    self._send_json({"error": f"invalid JSON body: {e}"}, 400)
                    return
                try:
                    ep = Endpoint(
                        id=body.get("id"),
                        name=body.get("name") or body.get("id"),
                        endpoint_type=body.get("endpoint_type", "cli"),
                        command=body.get("command"),
                        args=body.get("args", []),
                        prompt_mode=body.get("prompt_mode", "template"),
                        provider=body.get("provider"),
                        model=body.get("model"),
                        base_url=body.get("base_url"),
                        api_key_env=body.get("api_key_env"),
                        timeout_s=int(body.get("timeout_s", 120)),
                        capabilities=body.get("capabilities", {}),
                        session_config=body.get("session_config", {}),
                        system=0,  # API can never create system rows
                    )
                except (TypeError, ValueError) as e:
                    self._send_json({"error": str(e)}, 400)
                    return
                conn = get_db()
                init_db(conn)
                try:
                    created = create_endpoint(conn, ep)
                except EndpointMisconfigured as e:
                    conn.close()
                    self._send_json({"error": str(e)}, 400)
                    return
                except sqlite3.IntegrityError:
                    conn.close()
                    self._send_json(
                        {"error": f"endpoint {ep.id!r} already exists"}, 409
                    )
                    return
                conn.close()
                self._send_json(vars(created), 201)
                return

            # Kitchen pause/resume (M6) — global control surface.
            if remainder in ("/api/kitchen/pause", "/api/kitchen/resume"):
                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError):
                    body = {}
                reason = (body.get("reason") if isinstance(body, dict) else "") or ""
                if remainder.endswith("/pause"):
                    changed = _kitchen.pause(get_db, reason=reason)
                else:
                    changed = _kitchen.resume(get_db, reason=reason)
                self._send_json(
                    {
                        "paused": _kitchen.is_paused(),
                        "changed": changed,
                    }
                )
                return

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
                    "active": True,
                }
                try:
                    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                        registry = json.load(f)
                except (OSError, json.JSONDecodeError):
                    registry = {"projects": []}
                registry["projects"].append(new_project)
                with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                    json.dump(registry, f, indent=2)
                _refresh_projects_cache()
                conn = get_db()
                init_db(conn)
                backlog = (
                    Path(os.path.expanduser(new_project["path"])) / "PRODUCT_BACKLOG.md"
                )
                result = dict(new_project)
                if backlog.exists():
                    count = cli.seed_project(conn, new_project)
                    result["seeded"] = count
                else:
                    cli.scaffold_project(conn, new_project)
                    result["scaffolded"] = True
                conn.close()
                cli.regenerate_dashboard(new_project)
                self._send_json(result, 201)
                return

            # Install feedbacks via git clone — global (no project required).
            if remainder == "/api/settings/feedbacks/install":
                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError):
                    body = {}
                from constants import FEEDBACKS_REPO_URL

                install_dir = body.get(
                    "install_dir", str(Path.home() / "projects" / "feedbacks")
                )
                repo_url = body.get("repo_url", FEEDBACKS_REPO_URL)
                resolved_dir = Path(os.path.realpath(os.path.expanduser(install_dir)))
                home = Path.home().resolve()
                try:
                    resolved_dir.relative_to(home)
                except ValueError:
                    self._send_json(
                        {"error": "install_dir must be within home directory"}, 400
                    )
                    return
                ALLOWED_REPO_PREFIXES = ("https://github.com/", "https://gitlab.com/")
                if not any(repo_url.startswith(p) for p in ALLOWED_REPO_PREFIXES):
                    self._send_json(
                        {"error": "repo_url must be a GitHub or GitLab HTTPS URL"}, 400
                    )
                    return
                try:
                    subprocess.Popen(
                        ["git", "clone", repo_url, str(resolved_dir)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    _set_settings({"feedbacks.home": install_dir})
                    _feedbacks_cache["result"] = None
                    self._send_json(
                        {
                            "ok": True,
                            "message": f"git clone started → {install_dir}",
                            "install_dir": install_dir,
                        }
                    )
                except Exception as e:
                    self._send_json({"error": f"Failed to clone feedbacks: {e}"}, 500)
                return

            # Workflow editor linter — given a candidate trigger_json + on_success_json,
            # report whether any action mutates an attribute the trigger reads.
            if remainder == "/api/workflow/lint":
                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError):
                    self._send_json({"error": "Invalid JSON"}, 400)
                    return
                from conditions import lint_closed_loop

                result = lint_closed_loop(
                    body.get("trigger_json"),
                    body.get("on_success_json"),
                )
                self._send_json(result)
                return

            # Global create of a workflow agent — workflow_agents has no
            # project_id column, so the project-scoped handler logic mirrors here.
            if remainder == "/api/workflow/agents":
                try:
                    body = self._read_body()
                except (json.JSONDecodeError, ValueError):
                    self._send_json({"error": "Invalid JSON"}, 400)
                    return
                agent_id = body.get("id", "").strip()
                if not agent_id or not re.match(r"^[a-z0-9][a-z0-9_-]*$", agent_id):
                    self._send_json(
                        {
                            "error": "Invalid agent id — must match ^[a-z0-9][a-z0-9_-]*$"
                        },
                        400,
                    )
                    return
                name = body.get("name", agent_id)
                command = body.get("command", "claude")
                args = body.get("args", "[]")
                if isinstance(args, list):
                    args = json.dumps(args)
                system_prompt = body.get("system_prompt", "")
                agent = _create_workflow_agent(
                    agent_id, name, command, args, system_prompt
                )
                if agent:
                    self._send_json(agent, 201)
                else:
                    self._send_json(
                        {"error": f"Agent '{agent_id}' already exists"}, 409
                    )
                return

            # Global duplicate of any workflow (system or user). Body may
            # specify project_id (target for the new copy) and name. Defaults
            # to the source's first link, falling back to the first registered
            # project. Used by the /workflows page Duplicate button so users
            # can customize a system workflow without leaving the global view.
            m = re.match(
                r"^/api/workflow/workflows/([a-z0-9][a-z0-9_:.%-]*)/duplicate$",
                remainder,
            )
            if m:
                source_id = m.group(1)
                source = _get_workflow(source_id)
                if not source:
                    self._send_json({"error": "Workflow not found"}, 404)
                    return
                try:
                    body = self._read_body() or {}
                except (json.JSONDecodeError, ValueError):
                    body = {}

                target_pid = (body.get("project_id") or "").strip()
                if not target_pid:
                    # Pick the source's first link, falling back to the first
                    # registered project. Migrations may strand a system row
                    # with no links if a project was unregistered — treat that
                    # as a 400 rather than silently misrouting.
                    with _db_lock:
                        conn_lookup = get_db()
                        init_db(conn_lookup)
                        link_row = conn_lookup.execute(
                            "SELECT project_id FROM workflow_projects "
                            "WHERE workflow_id = ? ORDER BY project_id LIMIT 1",
                            (source_id,),
                        ).fetchone()
                        conn_lookup.close()
                    if link_row:
                        target_pid = link_row["project_id"]
                    else:
                        with _PROJECTS_CACHE_LOCK:
                            cache_snapshot = list(_PROJECTS_CACHE.values())
                        if cache_snapshot:
                            target_pid = cache_snapshot[0]["id"]
                if not target_pid:
                    self._send_json({"error": "No target project available"}, 400)
                    return

                new_name = (
                    body.get("name") or ""
                ).strip() or f"{source.get('name', source_id)} (copy)"
                import uuid as _uuid

                base_id = (
                    re.sub(r"[^a-z0-9_-]+", "-", new_name.lower()).strip("-")
                    or _uuid.uuid4().hex[:8]
                )
                candidate = base_id
                n = 2
                while _get_workflow(candidate):
                    candidate = f"{base_id}-{n}"
                    n += 1
                    if n > 100:
                        candidate = f"{base_id}-{_uuid.uuid4().hex[:6]}"
                        break

                steps = source.get("steps", "[]")
                if isinstance(steps, (list, dict)):
                    steps = json.dumps(steps)
                trigger_json = source.get("trigger_json")
                if trigger_json is not None and not isinstance(trigger_json, str):
                    trigger_json = json.dumps(trigger_json)
                on_success_json = source.get("on_success_json")
                if on_success_json is not None and not isinstance(on_success_json, str):
                    on_success_json = json.dumps(on_success_json)

                wf = _create_workflow(
                    candidate,
                    new_name,
                    source.get("description", ""),
                    steps,
                    project_id=target_pid,
                    enabled=int(source.get("enabled", 1)),
                    trigger_json=trigger_json,
                    on_success_json=on_success_json,
                    subject_type=source.get("subject_type", "ticket"),
                )
                if wf:
                    self._send_json(wf, 201)
                else:
                    self._send_json({"error": "Failed to duplicate workflow"}, 500)
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
            # Accept either the new key (`pause_reason`) or legacy `hold_reason`
            # for one release in case any external callers still send the old
            # field name.
            pause_reason = body.get("pause_reason")
            if pause_reason is None:
                pause_reason = body.get("hold_reason")
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    _kitchen_set_mode(
                        conn,
                        proj["id"],
                        "ticket",
                        ticket_id,
                        mode,
                        ActorContext.human(),
                        pause_reason=pause_reason,
                    )
                    conn.commit()
                    conn.close()
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            # Regenerate the dashboard so the 2s DOM-diff poll picks up the
            # mode change (Auto pill count, kitchen-badge class, play/pause
            # icon state). Without this the optimistic UI flip in the JS gets
            # overwritten by the next poll's stale HTML.
            try:
                cli.regenerate_dashboard(proj)
            except Exception:
                pass
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
                    conn = get_db()
                    init_db(conn)
                    _kitchen_set_ntr(
                        conn,
                        proj["id"],
                        ticket_id,
                        enabled,
                        note,
                        ActorContext.human(),
                    )
                    conn.commit()
                    conn.close()
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            t = _get_ticket_json(proj["id"], ticket_id)
            self._send_json(t or {"ok": True})
            return

        # Kitchen (M3): manual "Run now" trigger.
        # POST /api/tickets/{id}/run-now → spawns an agent run for this ticket.
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/run-now$", remainder)
        if m:
            ticket_id = m.group(1)
            # Resolve canonical id + check eligibility for a clean error path.
            with _db_lock:
                conn = get_db()
                init_db(conn)
                row = conn.execute(
                    "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
                    (ticket_id, proj["id"]),
                ).fetchone()
                if not row:
                    conn.close()
                    self._send_json({"error": "ticket not found"}, 404)
                    return
                tid = row["id"]
                er = _kitchen_eligibility(conn, proj["id"], "ticket", tid)
                conn.close()
            if not er.eligible:
                self._send_json(
                    {
                        "error": "ticket not eligible to run",
                        "reasons": list(er.reasons),
                    },
                    422,
                )
                return
            settings = {}  # WORKFLOW.toml read inside trigger_run
            run_id = _kitchen.trigger_run(
                lambda: get_db(),
                proj["id"],
                "ticket",
                tid,
                settings,
                triggered_by="human",
            )
            if run_id is None:
                # Could be no project path or active-run conflict.
                self._send_json(
                    {
                        "error": "could not start run (already active or project misconfigured)"
                    },
                    409,
                )
                return
            # Return the new run row.
            conn = get_db()
            try:
                r = conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
            finally:
                conn.close()
            self._send_json(dict(r) if r else {"id": run_id})
            return

        # Kitchen (M3): per-run actions.
        # POST /api/runs/{rid}/{action} where action in (stop|discard|retry|retry-fresh|respond)
        m = re.match(
            r"^/api/runs/(\d+)/(stop|discard|retry|retry-fresh|respond)$", remainder
        )
        if m:
            run_id = int(m.group(1))
            action = m.group(2)
            try:
                body = self._read_body() if action == "respond" else {}
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            with _db_lock:
                conn = get_db()
                init_db(conn)
                run = conn.execute(
                    "SELECT * FROM runs WHERE id = ? AND project_id = ?",
                    (run_id, proj["id"]),
                ).fetchone()
                conn.close()
            if not run:
                self._send_json({"error": "run not found"}, 404)
                return

            if action == "stop":
                ok = _kitchen.request_cancel(run_id)
                # Also flip the row defensively if the runner thread isn't ours.
                if not ok:
                    with _db_lock:
                        conn = get_db()
                        conn.execute(
                            "UPDATE runs SET status='cancelled', finished_at=?, "
                            "heartbeat_at=?, summary='cancelled by user' "
                            "WHERE id = ? AND status IN ('queued','preparing','running','needs_input')",
                            (
                                datetime.now().isoformat(),
                                datetime.now().isoformat(),
                                run_id,
                            ),
                        )
                        _kitchen_emit_event(
                            conn,
                            run["project_id"],
                            run["subject_type"],
                            run["subject_id"],
                            "run_cancelled",
                            {"run_id": run_id},
                            ActorContext.human(),
                        )
                        conn.commit()
                        conn.close()
                with _db_lock:
                    conn = get_db()
                    r = conn.execute(
                        "SELECT * FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    conn.close()
                self._send_json(dict(r) if r else {"id": run_id})
                return

            if action == "discard":
                # Mark every event from this run as discarded; emit run_discarded.
                with _db_lock:
                    conn = get_db()
                    cur = conn.execute(
                        "UPDATE activity_events SET discarded_run_id = ? "
                        "WHERE actor_type = 'agent' AND actor_id = ?",
                        (run_id, str(run_id)),
                    )
                    reverted = cur.rowcount
                    _kitchen_emit_event(
                        conn,
                        run["project_id"],
                        run["subject_type"],
                        run["subject_id"],
                        "run_discarded",
                        {
                            "run_id": run_id,
                            "reason": "user-initiated discard",
                            "reverted_event_count": reverted,
                        },
                        ActorContext.human(),
                    )
                    conn.commit()
                    conn.close()
                with _db_lock:
                    conn = get_db()
                    r = conn.execute(
                        "SELECT * FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    conn.close()
                self._send_json(dict(r) if r else {"id": run_id})
                return

            if action in ("retry", "retry-fresh"):
                # Spawn a fresh run on the same subject. retry-fresh wipes the
                # worktree first so after_create runs again.
                if action == "retry-fresh":
                    project_path = _kitchen._resolve_project_path(run["project_id"])
                    if project_path is not None:
                        _kitchen_wipe_fresh(
                            project_path,
                            run["project_id"],
                            run["subject_type"],
                            run["subject_id"],
                        )
                new_rid = _kitchen.trigger_run(
                    lambda: get_db(),
                    run["project_id"],
                    run["subject_type"],
                    run["subject_id"],
                    {},
                    triggered_by="retry",
                )
                if new_rid is None:
                    self._send_json(
                        {"error": "could not start retry (active run exists?)"}, 409
                    )
                    return
                with _db_lock:
                    conn = get_db()
                    r = conn.execute(
                        "SELECT * FROM runs WHERE id = ?", (new_rid,)
                    ).fetchone()
                    conn.close()
                self._send_json(dict(r) if r else {"id": new_rid})
                return

            if action == "respond":
                # Validate run is in needs_input state.
                if run["status"] != "needs_input":
                    self._send_json({"error": "run is not waiting for input"}, 409)
                    return
                kind = body.get("kind", "text")
                needs_input_kind = (
                    run["needs_input_kind"]
                    if "needs_input_kind" in run.keys()
                    else "text"
                )
                if kind != needs_input_kind:
                    self._send_json(
                        {
                            "error": f"payload kind '{kind}' does not match run's "
                            f"needs_input_kind '{needs_input_kind}'"
                        },
                        400,
                    )
                    return

                # Build the response payload for AgentRunner.resume_with_response.
                response_payload: dict = {"kind": kind}
                if kind == "text":
                    response_text = (body.get("response") or "").strip()
                    if not response_text:
                        self._send_json(
                            {"error": "response is required for text kind"}, 400
                        )
                        return
                    response_payload["response"] = response_text
                elif kind == "propose":
                    response_payload["accepted"] = body.get("accepted") or {}
                else:
                    self._send_json({"error": f"unknown kind: {kind}"}, 400)
                    return

                # Emit input_provided event.
                with _db_lock:
                    conn = get_db()
                    excerpt = (
                        response_payload.get("response")
                        or str(response_payload.get("accepted", {}))
                    )[:500]
                    _kitchen_emit_event(
                        conn,
                        run["project_id"],
                        run["subject_type"],
                        run["subject_id"],
                        "input_provided",
                        {"run_id": run_id, "kind": kind, "response_excerpt": excerpt},
                        ActorContext.human(),
                    )
                    conn.commit()
                    conn.close()

                # Dispatch to AgentRunner.resume_with_response in a background thread.
                # We need workspace info and config from the run record.
                try:
                    from runners import AgentRunner as _AgentRunner
                    from workflow_config import (
                        load_prompt_template,
                        load_workflow_config,
                    )
                    from workspaces import WorkspaceInfo as _WorkspaceInfo

                    workspace_path_str = (
                        run["workspace_path"]
                        if "workspace_path" in run.keys()
                        else None
                    )
                    if workspace_path_str:
                        ws_path = Path(workspace_path_str)
                    else:
                        project_path = _kitchen._resolve_project_path(run["project_id"])
                        ws_path = Path(project_path) if project_path else Path(".")

                    ws = _WorkspaceInfo(
                        path=ws_path,
                        branch="",
                        base_ref="origin/main",
                        is_git_worktree=False,
                        created_now=False,
                        bootstrapped=True,
                    )

                    # Load config from project path.
                    project_path_for_cfg = _kitchen._resolve_project_path(
                        run["project_id"]
                    )
                    if project_path_for_cfg:
                        cfg = load_workflow_config(project_path_for_cfg)
                        cfg["_prompt_template"] = load_prompt_template(
                            project_path_for_cfg
                        )
                        # Restore workflow meta from metadata_json if present.
                        try:
                            meta_json = (
                                run["metadata_json"]
                                if "metadata_json" in run.keys()
                                else "{}"
                            )
                            meta = json.loads(meta_json or "{}")
                            if "steps" in meta or "on_success" in meta:
                                cfg["_workflow_meta"] = meta
                        except (json.JSONDecodeError, TypeError):
                            pass
                    else:
                        cfg = {"agent": {"command": "claude -p"}}

                    def _resume_thread():
                        try:
                            _AgentRunner.resume_with_response(
                                run_id=run_id,
                                response_payload=response_payload,
                                project_id=run["project_id"],
                                subject_type=run["subject_type"],
                                subject_id=run["subject_id"],
                                workspace=ws,
                                config=cfg,
                                conn_factory=get_db,
                            )
                        except Exception:
                            import logging as _logging

                            _logging.getLogger(__name__).exception(
                                "resume_with_response failed for run %d", run_id
                            )

                    t = threading.Thread(
                        target=_resume_thread,
                        name=f"kitchen-resume-{run_id}",
                        daemon=True,
                    )
                    t.start()
                except Exception:
                    import logging as _logging

                    _logging.getLogger(__name__).exception(
                        "Failed to dispatch resume thread for run %d", run_id
                    )
                    # Fall back to simple DB flip if dispatch fails.
                    with _db_lock:
                        conn = get_db()
                        conn.execute(
                            "UPDATE runs SET status = 'running', heartbeat_at = ? "
                            "WHERE id = ? AND status = 'needs_input'",
                            (datetime.now().isoformat(), run_id),
                        )
                        conn.commit()
                        conn.close()

                self._send_json({"status": "resumed", "run_id": run_id})
                return

        # Kitchen (M4): file a gap ticket from a red scenario run.
        # POST /api/runs/{rid}/file-gap-ticket — closes the loop between a failed
        # journey scenario and the implementation work it implies.
        m = re.match(r"^/api/runs/(\d+)/file-gap-ticket$", remainder)
        if m:
            run_id = int(m.group(1))
            with _db_lock:
                conn = get_db()
                init_db(conn)
                run = conn.execute(
                    "SELECT * FROM runs WHERE id = ? AND project_id = ?",
                    (run_id, proj["id"]),
                ).fetchone()
                conn.close()
            if not run:
                self._send_json({"error": "run not found"}, 404)
                return
            if run["subject_type"] != "journey":
                self._send_json(
                    {"error": "gap tickets only file from scenario (journey) runs"}, 400
                )
                return
            if run["status"] not in ("failed", "stalled"):
                self._send_json(
                    {"error": "gap tickets only file from failed runs"}, 400
                )
                return
            try:
                meta = json.loads(run["metadata_json"] or "{}")
            except (ValueError, TypeError):
                meta = {}
            gap = meta.get("gap_report") or {}
            if not gap:
                self._send_json({"error": "no gap_report on this run"}, 400)
                return

            gap_kind = gap.get("gap_kind", "missing_feature")
            failed_action = gap.get("failed_step_action") or ""
            failed_target = gap.get("failed_step_target") or {}
            target_repr = ""
            if isinstance(failed_target, dict):
                # Pick the most useful key for the ticket title.
                for k in ("testid", "css", "role", "text", "title"):
                    if k in failed_target:
                        target_repr = f"{k}={failed_target[k]!r}"
                        break

            # Title + description prefilled from the gap.
            journey_id = run["subject_id"]
            title = (
                f"[gap:{gap_kind}] {failed_action} step in journey {journey_id}".strip()
            )
            desc_lines = [
                f"_Auto-filed from red scenario run #{run_id} (journey `{journey_id}`)._",
                "",
                f"**Gap kind:** `{gap_kind}`",
            ]
            if failed_action:
                desc_lines.append(f"**Failed action:** `{failed_action}`")
            if target_repr:
                desc_lines.append(f"**Target:** `{target_repr}`")
            err = (gap.get("error_message") or run["error_message"] or "").strip()
            if err:
                desc_lines.append("")
                desc_lines.append("**Error:**")
                desc_lines.append("```")
                desc_lines.append(err[:1000])
                desc_lines.append("```")
            screenshot = gap.get("screenshot_path")
            if screenshot:
                desc_lines.append("")
                desc_lines.append(f"**Screenshot:** `{screenshot}`")
            description = "\n".join(desc_lines)

            with _db_lock:
                conn = get_db()
                init_db(conn)
                # Create the draft ticket. Prefilled criterion mirrors the gap
                # so the human triaging it has something to react to.
                tid = _actions_add_ticket(
                    conn,
                    proj["id"],
                    title,
                    section="Ideas",
                    priority="medium",
                    description=description,
                    draft=True,
                    emit_created_event=False,
                )
                # Pre-populate one acceptance criterion so the ticket reads as
                # actionable rather than empty.
                criterion = {
                    "missing_selector": f"Element selectable by {target_repr or 'the failed target'} exists and is visible",
                    "missing_screen": f"Route reached by {failed_action or 'the failed open step'} renders successfully",
                    "missing_feature": f"User can complete the {failed_action or 'failed'} step end-to-end",
                    "ambiguous_goal": f"Journey {journey_id} re-spec'd with concrete acceptance steps",
                    "external_dependency": f"External dependency identified by run #{run_id} resolved or mocked",
                    "test_harness_gap": f"Scenario harness can drive journey {journey_id} without engine-level error",
                }.get(gap_kind, f"Resolve gap from run #{run_id}")
                conn.execute(
                    "INSERT INTO acceptance_criteria (ticket_id, project_id, text) VALUES (?, ?, ?)",
                    (tid, proj["id"], criterion),
                )
                # Link the new ticket to the journey via journey_tickets so the
                # next cascade after this ticket lands in Done re-runs the
                # journey to prove green.
                conn.execute(
                    "INSERT OR IGNORE INTO journey_tickets (journey_id, project_id, ticket_id) "
                    "VALUES (?, ?, ?)",
                    (journey_id, proj["id"], tid),
                )
                _kitchen_emit_event(
                    conn,
                    proj["id"],
                    "ticket",
                    tid,
                    "ticket_created",
                    {
                        "origin": "journey_gap",
                        "draft": True,
                        "section": "Ideas",
                        "from_gap_run_id": run_id,
                        "linked_journey": journey_id,
                    },
                    ActorContext.system(),
                )
                conn.commit()
                conn.close()

            t = _get_ticket_json(proj["id"], tid)
            self._send_json(
                {"ticket": t, "linked_journey": journey_id, "gap_kind": gap_kind}, 201
            )
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

            valid_fields = {"description", "criteria", "reviewed"}
            if field not in valid_fields:
                self._send_json(
                    {
                        "error": f"field must be one of: {', '.join(sorted(valid_fields))}"
                    },
                    400,
                )
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

        # AI-powered learning candidate generation
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/learnings/generate$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            current_content = body.get("content", "")
            result = _run_learning_generation(proj, ticket_id, current_content)
            if "error" in result:
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

        # Bookmark toggle (I-43): POST /api/bookmarks/{ticket_id}
        m = re.match(r"^/api/bookmarks/([A-Za-z0-9_-]+)$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    try:
                        new_state = _actions_toggle_bookmark(
                            conn, proj["id"], ticket_id
                        )
                    finally:
                        conn.close()
            except AppError as e:
                self._send_json({"error": str(e)}, 404)
                return
            self._send_json({"ticket_id": ticket_id, "bookmarked": new_state})
            return

        # Recents touch (I-43): POST /api/recents/{ticket_id}
        m = re.match(r"^/api/recents/([A-Za-z0-9_-]+)$", remainder)
        if m:
            ticket_id = m.group(1)
            try:
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    try:
                        _actions_touch_recent(conn, proj["id"], ticket_id)
                    finally:
                        conn.close()
            except AppError as e:
                self._send_json({"error": str(e)}, 404)
                return
            self._send_json({"ok": True})
            return

        # Accept ticket (move to Done + append to PRODUCT_SPECIFICATION.md)
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/accept$", remainder)
        if m:
            ticket_id = m.group(1)
            # Accept has historically been a bodyless POST; keep that working and
            # treat a body purely as the optional {"force": "<reason>"} override.
            try:
                body = self._read_body() or {}
            except (json.JSONDecodeError, ValueError):
                body = {}
            ok, message = _accept_ticket(
                proj, ticket_id, force=str(body.get("force", "") or "")
            )
            if ok:
                t = _get_ticket_json(proj["id"], ticket_id)
                self._send_json(t or {"ok": True})
            else:
                # Forward the gate's reason rather than a generic failure, so the
                # dashboard can show what the CLI would have printed.
                self._send_json({"error": message or "Failed to accept ticket"}, 400)
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

        # Scan branches + PRs
        if remainder == "/api/branches/scan":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                body = {}
            include_prs = body.get("include_prs", True)
            project_id = proj["id"]
            project_path = os.path.expanduser(proj.get("path", ""))

            with _db_lock:
                conn = get_db()
                init_db(conn)
                result = _actions_scan_branches(conn, project_id, project_path)
                pr_result = {"updated": 0}
                if include_prs:
                    pr_result = _actions_scan_prs(conn, project_id, project_path)
                conn.commit()
                cli.sync_to_markdown(conn, proj)
                conn.close()
            cli.regenerate_dashboard(proj)
            self._send_json(
                {
                    "linked": result.get("linked", 0),
                    "total_remote": result.get("total_remote", 0),
                    "pr_updated": pr_result.get("updated", 0),
                    "error": result.get("error") or pr_result.get("error") or None,
                }
            )
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
            att = _add_attachment(
                proj["id"],
                ticket_id,
                attachment_type="feedbacks",
                name=session_name,
                path=session_path,
                summary=body.get("summary", ""),
                metadata=json.dumps(
                    {k: v for k, v in body.items() if k not in ("ticket_id",)}
                ),
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

            callback_url = (
                f"http://localhost:{SERVER_PORT}/{proj['id']}/api/feedbacks/callback"
            )
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
                self._send_json(
                    {"error": "feedbacks.home not configured or start.sh not found"},
                    400,
                )
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
                    journey = add_journey(
                        conn,
                        proj["id"],
                        title,
                        description=body.get("description", ""),
                        persona=body.get("persona", ""),
                    )
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
                    step = add_step(
                        conn,
                        journey_id,
                        proj["id"],
                        action=body.get("action", "click"),
                        label=body.get("label", ""),
                        actor=body.get("actor", "user"),
                        target=body.get("target"),
                        value=body.get("value", ""),
                        key=body.get("key", ""),
                        capture=body.get("capture"),
                        assertion=body.get("assertion"),
                    )
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
                from scenarios import ScenarioValidationError, validate_manifest

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
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                body = {}
            backend = body.get("backend") if isinstance(body, dict) else None
            if backend not in ("playwright", "cdp"):
                backend = None
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
            # If CDP was requested, make sure a debuggable Chrome is reachable.
            # Either reuse an existing one (the user's, or a previously
            # spawned managed instance) or spawn a fresh headless one.
            if backend == "cdp":
                ok, detail = _ensure_cdp_chrome()
                if not ok:
                    self._send_json(
                        {
                            "error": f"CDP backend requested but no Chrome reachable: {detail}"
                        },
                        503,
                    )
                    return
            run_id = f"{scenario_id}-{int(time.time())}"
            started_at = datetime.now(timezone.utc).isoformat()
            project_path = proj.get("path", "")
            scenarios_dir = os.path.join(project_path, "tests", "scenarios")
            os.makedirs(scenarios_dir, exist_ok=True)
            with open(
                os.path.join(scenarios_dir, f"{journey_id}.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)
                f.write("\n")
            # Insert journey_runs row up front so the GUI can poll status.
            # Step results get filled in by _finalize_journey_run on completion.
            with _db_lock:
                conn = get_db()
                init_db(conn)
                step_rows = conn.execute(
                    "SELECT id FROM journey_steps "
                    "WHERE journey_id = ? AND project_id = ? ORDER BY sort_order",
                    (journey_id, proj["id"]),
                ).fetchall()
                conn.execute(
                    "INSERT INTO journey_runs "
                    "(id, journey_id, project_id, status, started_at) "
                    "VALUES (?, ?, ?, 'running', ?)",
                    (run_id, journey_id, proj["id"], started_at),
                )
                for i, srow in enumerate(step_rows):
                    conn.execute(
                        "INSERT INTO journey_step_results "
                        "(run_id, step_id, sort_order, status) "
                        "VALUES (?, ?, ?, 'pending')",
                        (run_id, srow["id"], i),
                    )
                conn.commit()
                conn.close()
            cmd = [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_scenarios.py",
                "-v",
                f"--scenario-id={scenario_id}",
            ]
            if backend:
                cmd.append(f"--backend={backend}")
            env = {
                **os.environ,
                "TT_SCENARIO_BASE_URL": f"http://localhost:{SERVER_PORT}/{proj['id']}",
            }
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
                    "journey_id": journey_id,
                    "project_id": proj["id"],
                }
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
                    conn.execute(
                        "DELETE FROM journey_steps WHERE journey_id = ? AND project_id = ?",
                        (journey_id, proj["id"]),
                    )
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
            backend = body.get("backend")
            if backend not in ("playwright", "cdp"):
                backend = None
            if not scenario_id:
                self._send_json({"error": "scenario_id required"}, 400)
                return

            project_path = proj.get("path", "")
            run_id = f"{scenario_id}-{int(time.time())}"

            cmd = [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_scenarios.py",
                "-v",
                f"--scenario-id={scenario_id}",
            ]
            if publish:
                cmd.append("--publish")
            if backend:
                cmd.append(f"--backend={backend}")

            env = {
                **os.environ,
                "TT_SCENARIO_BASE_URL": f"http://localhost:{SERVER_PORT}/{proj['id']}",
            }

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
                existing = (
                    discover_scenarios(scenarios_dir)
                    if os.path.isdir(scenarios_dir)
                    else []
                )
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
                candidates_out.append(
                    {
                        "title": c.title,
                        "summary": c.summary,
                        "manifest": c.manifest,
                        "assumptions": c.assumptions,
                        "prerequisites": c.prerequisites,
                        "confidence": c.confidence,
                    }
                )

            self._send_json(
                {
                    "intent_summary": result.intent_summary,
                    "candidates": candidates_out,
                    "warnings": result.warnings,
                }
            )
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
                    self._send_json(
                        {"error": "'filename' or manifest 'id' is required"}, 400
                    )
                    return
                filename = f"{manifest_id}.json"

            # Ensure .json extension
            if not filename.endswith(".json"):
                filename = filename + ".json"

            # Security: reject path traversal
            if "/" in filename or "\\" in filename or ".." in filename:
                self._send_json(
                    {
                        "error": "filename must be a plain filename with no path separators"
                    },
                    400,
                )
                return

            # Validate the manifest
            from scenarios import ScenarioValidationError, validate_manifest

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

            install_dir = body.get(
                "install_dir", str(Path.home() / "projects" / "feedbacks")
            )
            repo_url = body.get("repo_url", FEEDBACKS_REPO_URL)
            # Validate install_dir is within home directory
            resolved_dir = Path(os.path.realpath(os.path.expanduser(install_dir)))
            home = Path.home().resolve()
            try:
                resolved_dir.relative_to(home)
            except ValueError:
                self._send_json(
                    {"error": "install_dir must be within home directory"}, 400
                )
                return
            # Validate repo_url is a trusted HTTPS source
            ALLOWED_REPO_PREFIXES = ("https://github.com/", "https://gitlab.com/")
            if not any(repo_url.startswith(p) for p in ALLOWED_REPO_PREFIXES):
                self._send_json(
                    {"error": "repo_url must be a GitHub or GitLab HTTPS URL"}, 400
                )
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
                self._send_json(
                    {
                        "ok": True,
                        "message": f"git clone started → {install_dir}",
                        "install_dir": install_dir,
                    }
                )
            except Exception as e:
                self._send_json({"error": f"Failed to clone feedbacks: {e}"}, 500)
            return

        # Phase 3A: eligibility inspector
        if remainder == "/api/workflows/inspect":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            ticket_id = (body.get("ticket_id") or "").strip()
            if not ticket_id:
                self._send_json({"error": "ticket_id is required"}, 400)
                return
            try:
                result = _inspect_workflows_for_ticket(proj["id"], ticket_id)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 404)
                return
            self._send_json(result)
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
                self._send_json(
                    {"error": "Invalid agent id — must match ^[a-z0-9][a-z0-9_-]*$"},
                    400,
                )
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

        # Create workflow (Phase 3A: accepts new fields)
        if remainder == "/api/workflow/workflows":
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            workflow_id = body.get("id", "").strip()
            if not workflow_id or not re.match(r"^[a-z0-9][a-z0-9_-]*$", workflow_id):
                self._send_json(
                    {"error": "Invalid workflow id — must match ^[a-z0-9][a-z0-9_-]*$"},
                    400,
                )
                return
            name = body.get("name", workflow_id)
            description = body.get("description", "")
            steps = body.get("steps", [])
            if isinstance(steps, list):
                steps = json.dumps(steps)
            # New Phase 3A fields — accept object or string for trigger_json/on_success_json
            raw_trigger = body.get("trigger_json")
            trigger_json = None
            if raw_trigger is not None:
                trigger_json = (
                    json.dumps(raw_trigger)
                    if not isinstance(raw_trigger, str)
                    else raw_trigger
                )
            raw_success = body.get("on_success_json")
            on_success_json = None
            if raw_success is not None:
                on_success_json = (
                    json.dumps(raw_success)
                    if not isinstance(raw_success, str)
                    else raw_success
                )
            enabled = int(bool(body.get("enabled", True)))
            subject_type = body.get("subject_type", "ticket")
            wf = _create_workflow(
                workflow_id,
                name,
                description,
                steps,
                project_id=proj["id"],
                enabled=enabled,
                trigger_json=trigger_json,
                on_success_json=on_success_json,
                subject_type=subject_type,
            )
            if wf:
                self._send_json(wf, 201)
            else:
                self._send_json(
                    {"error": f"Workflow '{workflow_id}' already exists"}, 409
                )
            return

        # Duplicate workflow — clones any workflow (including system rows, regardless
        # of enabled state) into a new user-owned (system=0) row.
        m = re.match(
            r"^/api/workflow/workflows/([a-z0-9][a-z0-9_:.%-]*)/duplicate$", remainder
        )
        if m:
            source_id = m.group(1)
            existing = _get_workflow(source_id, project_id=proj["id"])
            if not existing:
                self._send_json({"error": "Workflow not found"}, 404)
                return
            # Optional override name from body, else "<name> (copy)".
            try:
                body = self._read_body() or {}
            except (json.JSONDecodeError, ValueError):
                body = {}
            new_name = (
                body.get("name") or ""
            ).strip() or f"{existing.get('name', source_id)} (copy)"

            # Generate a fresh ID by suffixing -copy / -copy-2 / -copy-N.
            import uuid as _uuid

            base_id = (
                re.sub(r"[^a-z0-9_-]+", "-", new_name.lower()).strip("-")
                or _uuid.uuid4().hex[:8]
            )
            candidate = base_id
            n = 2
            while _get_workflow(candidate, project_id=proj["id"]):
                candidate = f"{base_id}-{n}"
                n += 1
                if n > 100:
                    candidate = f"{base_id}-{_uuid.uuid4().hex[:6]}"
                    break

            # Steps / trigger_json / on_success_json may be strings or objects in
            # the serialized form returned by _get_workflow. Re-stringify if needed.
            steps = existing.get("steps", "[]")
            if isinstance(steps, (list, dict)):
                steps = json.dumps(steps)
            trigger_json = existing.get("trigger_json")
            if trigger_json is not None and not isinstance(trigger_json, str):
                trigger_json = json.dumps(trigger_json)
            on_success_json = existing.get("on_success_json")
            if on_success_json is not None and not isinstance(on_success_json, str):
                on_success_json = json.dumps(on_success_json)
            description = existing.get("description", "")
            subject_type = existing.get("subject_type", "ticket")

            wf = _create_workflow(
                candidate,
                new_name,
                description,
                steps,
                project_id=proj["id"],
                enabled=int(existing.get("enabled", 1)),
                trigger_json=trigger_json,
                on_success_json=on_success_json,
                subject_type=subject_type,
            )
            if wf:
                self._send_json(wf, 201)
            else:
                self._send_json({"error": "Failed to duplicate workflow"}, 500)
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
            workflow = _get_workflow(workflow_id, project_id=proj["id"])
            if not workflow:
                self._send_json({"error": f"Workflow '{workflow_id}' not found"}, 404)
                return
            # Parse steps to get total
            try:
                steps = (
                    json.loads(workflow.get("steps", "[]"))
                    if isinstance(workflow.get("steps"), str)
                    else workflow.get("steps", [])
                )
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
            if not _get_workflow_run(run_id, project_id=proj["id"]):
                self._send_json({"error": "Run not found"}, 404)
                return
            with _workflow_runs_lock:
                mem = _workflow_runs.get(run_id)
                if mem:
                    mem["status"] = "cancelled"
            _update_workflow_run(
                run_id, status="cancelled", completed_at=datetime.utcnow().isoformat()
            )
            self._send_json({"ok": True, "run_id": run_id, "status": "cancelled"})
            return

        # Resume a paused workflow run
        m = re.match(r"^/api/workflow/runs/([A-Za-z0-9_-]+)/resume$", remainder)
        if m:
            run_id = m.group(1)
            if not _get_workflow_run(run_id, project_id=proj["id"]):
                self._send_json({"error": "Run not found"}, 404)
                return
            with _workflow_runs_lock:
                mem = _workflow_runs.get(run_id)
                if mem:
                    mem["status"] = "running"
            _update_workflow_run(run_id, status="running")
            self._send_json({"ok": True, "run_id": run_id, "status": "running"})
            return

        # Respond to a needs_input workflow run — appends user turn then flips to paused
        # so the spin-loop in _run_workflow_thread picks it up and resumes.
        m = re.match(r"^/api/workflow/runs/([A-Za-z0-9_-]+)/respond$", remainder)
        if m:
            run_id = m.group(1)
            run = _get_workflow_run(run_id, project_id=proj["id"])
            if not run:
                self._send_json({"error": "Run not found"}, 404)
                return
            if run.get("status") != "needs_input":
                self._send_json({"error": "Run is not awaiting input"}, 409)
                return
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            response_text = (body.get("response") or "").strip()
            if not response_text:
                self._send_json({"error": "response is required"}, 400)
                return
            # Append a user turn to conversation so the next subprocess call sees it
            current_step = run.get("current_step", 0)
            try:
                conversation = json.loads(run.get("conversation") or "[]")
            except (json.JSONDecodeError, TypeError):
                conversation = []
            conversation.append(
                {
                    "role": "user",
                    "step": current_step,
                    "content": response_text,
                    "ts": datetime.utcnow().isoformat(),
                }
            )
            # Flip to paused (spin-loop condition: st != "paused" → break) so the
            # orchestrator exits the spin-loop and resumes execution.
            _update_workflow_run(
                run_id,
                conversation=conversation,
                status="paused",
            )
            with _workflow_runs_lock:
                mem = _workflow_runs.get(run_id)
                if mem:
                    mem["status"] = "paused"
            updated = _get_workflow_run(run_id)
            self._send_json(updated or {"ok": True, "run_id": run_id})
            return

        # Pane link: POST /<pid>/api/tickets/<tid>/pane-links
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/pane-links$", remainder)
        if m:
            tid = m.group(1)
            try:
                payload = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            pane_addr = payload.get("pane_address", "").strip()
            host = payload.get("host", "").strip()
            desc = payload.get("pane_descriptor", "").strip()
            if not pane_addr or not host:
                self._send_json({"error": "pane_address and host required"}, 400)
                return
            if not re.match(r"^%[0-9]+$", pane_addr):
                self._send_json(
                    {
                        "error": "pane_address must match ^%[0-9]+$ (real tmux pane IDs only)"
                    },
                    400,
                )
                return
            import pane_links as _pl

            with _db_lock:
                conn = get_db()
                init_db(conn)
                if not conn.execute(
                    "SELECT 1 FROM tickets WHERE id = ? AND project_id = ?",
                    (tid, proj["id"]),
                ).fetchone():
                    conn.close()
                    self._send_json({"error": "ticket not found"}, 404)
                    return
                from actions import ActorContext as _AC
                from actions import emit_event as _emit

                row_id = _pl.link_pane(conn, tid, proj["id"], pane_addr, host, desc)
                _emit(
                    conn,
                    proj["id"],
                    "ticket",
                    tid,
                    "pane_linked",
                    {"pane_address": pane_addr, "host": host, "pane_descriptor": desc},
                    _AC.human(),
                )
                conn.commit()
                conn.close()
            self._send_json({"id": row_id, "pane_address": pane_addr}, 201)
            return

        # POST /<pid>/api/pane-links/<addr>/send-keys
        m = re.match(r"^/api/pane-links/(.+)/send-keys$", remainder)
        if m:
            from constants import PANE_SEND_KEYS_MAX_BYTES

            # pane_address values are literal tmux IDs like %23. Extract from
            # the raw (un-decoded) path so that %23 is not decoded to '#'.
            # The raw segment IS the pane_address as stored in the DB.
            raw_path = urlparse(self.path).path
            _raw_proj, raw_remainder = _resolve_project_from_path(raw_path)
            rm2 = re.match(r"^/api/pane-links/(.+)/send-keys$", raw_remainder)
            pane_addr = rm2.group(1) if rm2 else m.group(1)
            try:
                payload = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            text = payload.get("text", "")
            press_enter = bool(payload.get("press_enter", True))
            if "\x00" in text:
                self._send_json({"error": "null bytes not allowed"}, 400)
                return
            if len(text.encode("utf-8")) > PANE_SEND_KEYS_MAX_BYTES:
                self._send_json({"error": "text exceeds 4KB"}, 413)
                return
            import socket as _sock

            import pane_links as _pl

            with _db_lock:
                conn = get_db()
                init_db(conn)
                row = _pl.get_ticket_for_pane(conn, pane_addr)
                conn.close()
            if not row:
                self._send_json({"error": "no link for pane"}, 404)
                return
            local = _sock.gethostname()
            if row["host"] != local:
                self._send_json(
                    {
                        "error": f"pane on host {row['host']!r}, server is {local!r}; cross-host send not supported in v1"
                    },
                    409,
                )
                return
            if not self._check_send_keys_rate(pane_addr):
                self._send_json({"error": "rate limit (10/s)"}, 429)
                return
            import subprocess as _sub

            args_ = ["tmux", "send-keys", "-t", pane_addr, "-l", text]
            try:
                _sub.run(args_, check=True, timeout=2)
                if press_enter:
                    _sub.run(
                        ["tmux", "send-keys", "-t", pane_addr, "Enter"],
                        check=True,
                        timeout=2,
                    )
            except _sub.CalledProcessError as e:
                self._send_json({"error": f"tmux send-keys failed: {e}"}, 502)
                return
            # Audit event — text is intentionally omitted (may contain secrets)
            from actions import ActorContext as _AC
            from actions import emit_event as _emit

            with _db_lock:
                _aconn = get_db()
                init_db(_aconn)
                try:
                    _emit(
                        _aconn,
                        row["project_id"],
                        "ticket",
                        row["ticket_id"],
                        "pane_send_keys",
                        {
                            "pane_address": pane_addr,
                            "text_bytes": len(text.encode("utf-8")),
                            "press_enter": press_enter,
                        },
                        _AC.human(),
                    )
                    _aconn.commit()
                finally:
                    _aconn.close()
            self._send_json({"sent": True})
            return

        self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        path = unquote(urlparse(self.path).path)
        proj, remainder = _resolve_project_from_path(path)

        # ── Global routes ───────────────────────────────────────────
        if proj is None:
            m = re.match(r"^/api/projects/([a-z0-9][a-z0-9-]*[a-z0-9])$", remainder)
            if m:
                pid = m.group(1)
                try:
                    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                        registry = json.load(f)
                except (OSError, json.JSONDecodeError):
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

            # Global delete of a workflow agent — system agents refused.
            m = re.match(r"^/api/workflow/agents/([a-z0-9][a-z0-9_-]*)$", remainder)
            if m:
                agent_id = m.group(1)
                with _db_lock:
                    _ag_conn = get_db()
                    init_db(_ag_conn)
                    _ag_row = _ag_conn.execute(
                        "SELECT system FROM workflow_agents WHERE id = ?",
                        (agent_id,),
                    ).fetchone()
                    _ag_conn.close()
                if _ag_row and int(_ag_row["system"] or 0) == 1:
                    self._send_json({"error": "system_agent"}, 403)
                    return
                if _delete_workflow_agent(agent_id):
                    self._send_json({"ok": True, "deleted": agent_id})
                else:
                    self._send_json({"error": "Agent not found"}, 404)
                return

            # Global delete of a workflow — system rows refused.
            m = re.match(
                r"^/api/workflow/workflows/([a-z0-9][a-z0-9_:.%-]*)$", remainder
            )
            if m:
                workflow_id = m.group(1)
                existing = _get_workflow(workflow_id)
                if not existing:
                    self._send_json({"error": "Workflow not found"}, 404)
                    return
                if existing.get("system"):
                    self._send_json({"error": "system_workflow"}, 403)
                    return
                if _delete_workflow(workflow_id):
                    self._send_json({"ok": True, "deleted": workflow_id})
                else:
                    self._send_json({"error": "Workflow not found"}, 404)
                return

            # DELETE /api/endpoints/{id} — delete a user endpoint
            m = re.match(r"^/api/endpoints/([a-zA-Z0-9_-]+)$", remainder)
            if m:
                endpoint_id = m.group(1)
                from endpoints import delete_endpoint

                conn = get_db()
                init_db(conn)
                try:
                    unlinked = delete_endpoint(conn, endpoint_id)
                except KeyError:
                    conn.close()
                    self._send_json({"error": "endpoint not found"}, 404)
                    return
                except PermissionError:
                    conn.close()
                    self._send_json({"error": "system_endpoint"}, 403)
                    return
                conn.close()
                if unlinked > 0:
                    self._send_json({"agents_unlinked": unlinked})
                else:
                    self.send_response(204)
                    self.end_headers()
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

        m = re.match(
            r"^/api/journeys/([A-Za-z0-9_-]+)/link/([A-Za-z0-9_-]+)$", remainder
        )
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

        # Delete workflow (Phase 3A: system workflow guard, Phase 3 fix: project-scoped)
        m = re.match(r"^/api/workflow/workflows/([a-z0-9][a-z0-9_:.%-]*)$", remainder)
        if m:
            workflow_id = m.group(1)
            existing = _get_workflow(workflow_id, project_id=proj["id"])
            if not existing:
                self._send_json({"error": "Workflow not found"}, 404)
                return
            if existing.get("system"):
                self._send_json({"error": "system_workflow"}, 403)
                return
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

        # Delete pane link: DELETE /<pid>/api/pane-links/<addr>
        m = re.match(r"^/api/pane-links/(.+)$", remainder)
        if m:
            # pane_address values are literal tmux IDs like %23. Extract from
            # the raw (un-decoded) path so that %23 is not decoded to '#'.
            raw_path = urlparse(self.path).path
            _raw_proj, raw_remainder = _resolve_project_from_path(raw_path)
            rm2 = re.match(r"^/api/pane-links/(.+)$", raw_remainder)
            pane_addr = rm2.group(1) if rm2 else m.group(1)
            import pane_links as _pl

            with _db_lock:
                conn = get_db()
                init_db(conn)
                row = _pl.get_ticket_for_pane(conn, pane_addr)
                if not row:
                    conn.close()
                    self._send_json({"error": "no link for pane"}, 404)
                    return
                from actions import ActorContext as _AC
                from actions import emit_event as _emit

                _pl.unlink_pane(conn, pane_addr)
                _emit(
                    conn,
                    row["project_id"],
                    "ticket",
                    row["ticket_id"],
                    "pane_unlinked",
                    {"pane_address": pane_addr},
                    _AC.human(),
                )
                conn.commit()
                conn.close()
            self._send_json({"deleted": pane_addr})
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
                        print(
                            f"[feedbacks-watcher] Linked session {entry.name} → {ticket_id}"
                        )

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
                                print(
                                    f"[watcher] External edits absorbed for {project.get('id', '?')}"
                                )
                            conn.close()
                    except Exception as exc:
                        print(f"[watcher] Error for {project.get('id', '?')}: {exc}")
            except Exception:
                import traceback

                traceback.print_exc()

    t = threading.Thread(target=_poll, daemon=True, name="md-edit-watcher")
    t.start()


# ---------------------------------------------------------------------------
# Pane capture worker — polls active local panes every 2s
# ---------------------------------------------------------------------------


def _start_pane_capture_worker():
    """Daemon thread: every 2s, capture each active local pane and update.

    Local-only: only panes whose host matches socket.gethostname() are captured.
    Capture failure → mark stale. Tail is ANSI-stripped and bounded.
    """
    import socket as _sock
    import subprocess as _sub
    import time as _t

    import pane_links as _pl
    from constants import PANE_CAPTURE_INTERVAL_S

    local = _sock.gethostname()

    def _poll():
        while True:
            try:
                _t.sleep(PANE_CAPTURE_INTERVAL_S)

                # Phase 1: collect link rows under the lock, then release.
                # Holding the lock across tmux subprocesses (phase 2) would block
                # API requests for up to 3*N seconds per cycle with N active panes.
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    try:
                        rows = list(_pl.list_pane_links_for_host(conn, local))
                        # Snapshot fields we need; convert Row → plain dict so we
                        # can safely use the values after conn is closed.
                        snapshots = [
                            {
                                "pane_address": r["pane_address"],
                                "prev_tail": r["tail_text"] or "",
                                "prev_time": r["last_captured_at"] or 0,
                            }
                            for r in rows
                        ]
                    finally:
                        conn.close()

                # Phase 2: run tmux subprocesses WITHOUT the lock.
                captures = []  # list of (addr, tail_or_None)
                for snap in snapshots:
                    addr = snap["pane_address"]
                    try:
                        res = _sub.run(
                            ["tmux", "capture-pane", "-p", "-S", "-200", "-t", addr],
                            capture_output=True,
                            text=True,
                            timeout=3,
                        )
                        if res.returncode != 0:
                            captures.append((addr, None))
                        else:
                            captures.append((addr, _pl.strip_ansi(res.stdout)))
                    except (_sub.TimeoutExpired, FileNotFoundError, OSError):
                        captures.append((addr, None))

                # Phase 3: write results under the lock.
                if captures:
                    snap_by_addr = {s["pane_address"]: s for s in snapshots}
                    with _db_lock:
                        conn = get_db()
                        init_db(conn)
                        try:
                            for addr, tail in captures:
                                if tail is None:
                                    _pl.mark_pane_stale(conn, addr)
                                else:
                                    snap = snap_by_addr[addr]
                                    state = _pl.classify_attention(
                                        tail,
                                        prev_tail=snap["prev_tail"],
                                        prev_time=snap["prev_time"],
                                    )
                                    _pl.update_pane_capture(conn, addr, tail, state)
                            conn.commit()
                        finally:
                            conn.close()
            except Exception as _exc:
                print(f"[pane-capture] error: {_exc}", flush=True)

    t = threading.Thread(target=_poll, daemon=True, name="pane-capture")
    t.start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    global _LEGACY_PROJECT_ID, SERVER_PORT

    args = sys.argv[1:]
    bind_host = "127.0.0.1"
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            SERVER_PORT = int(args[idx + 1])
    if "--project" in args:
        idx = args.index("--project")
        if idx + 1 < len(args):
            _LEGACY_PROJECT_ID = args[idx + 1]
    if "--bind" in args:
        idx = args.index("--bind")
        if idx + 1 < len(args):
            bind_host = args[idx + 1]

    # Populate registry cache
    _refresh_projects_cache()

    if not _PROJECTS_CACHE:
        print(
            "No active projects in registry. Register a project first.", file=sys.stderr
        )
        sys.exit(1)

    project_names = [p.get("name", p["id"]) for p in _PROJECTS_CACHE.values()]
    print(f"Serving {len(project_names)} project(s): {', '.join(project_names)}")

    # Recover workflow runs stuck in "running" from a previous server session
    _recover_stuck_workflow_runs()

    # Seed default global endpoints (idempotent; no-op until migration 19 creates the table)
    try:
        _ep_db = get_db()
        _seed_default_endpoints(_ep_db)
        _ep_db.close()
    except sqlite3.OperationalError as _e:
        print(f"  Warning: endpoints table not ready: {_e}", file=sys.stderr)

    # Seed default global agents (idempotent, runs once regardless of projects)
    try:
        _ag_db = get_db()
        _ag_result = _seed_default_agents(_ag_db)
        _ag_db.close()
        if _ag_result["inserted"]:
            print(f"  Seeded {_ag_result['inserted']} default agent(s)")
        if _ag_result["migrated"]:
            print(
                f"  Migrated {_ag_result['migrated']} agent(s) (agent_planchk → agent_consultant)"
            )
    except Exception as _e:
        print(
            f"  Warning: could not seed default agents: {_e}",
            file=__import__("sys").stderr,
        )

    # Seed default system workflows for every registered project (idempotent).
    # Post-migration-16 the seeder links the project to existing canonical
    # workflow rows rather than duplicating them.
    for _proj in list(_PROJECTS_CACHE.values()):
        try:
            _wf_db = get_db()
            _result = _seed_default_workflows(_wf_db, _proj["id"])
            _wf_db.close()
            if _result.get("linked"):
                print(
                    f"  Linked {_result['linked']} default workflow(s) to project {_proj['id']!r}"
                )
        except Exception as _e:
            print(
                f"  Warning: could not seed default workflows for {_proj.get('id')!r}: {_e}",
                file=__import__("sys").stderr,
            )

    # Start background threads
    _start_external_edit_watcher()
    _start_feedbacks_session_watcher()
    _start_pane_capture_worker()

    # Kitchen orchestrator (M3) — polls eligible subjects, dispatches agent runs.
    # Pinned to 5s tick by default; WORKFLOW.toml's automation.* settings are
    # read per-project at dispatch time inside trigger logic.
    _kitchen.start(
        get_db,
        settings={
            "kitchen_poll_seconds": 5.0,
            "max_concurrent_runs": 3,
            "max_concurrent_per_project": 1,
        },
    )

    # Kitchen evidence rotation (M5) — daily sweep transitions on-disk
    # artifacts live → summarised → pruned per docs/KITCHEN.md §13.
    _kitchen_evidence.start_rotation_daemon(get_db, live_days=30, summarised_days=60)

    server = ThreadingHTTPServer((bind_host, SERVER_PORT), DashboardHandler)
    url = f"http://localhost:{SERVER_PORT}"
    if bind_host not in ("127.0.0.1", "localhost"):
        print(
            f"Binding to {bind_host}:{SERVER_PORT} (reachable on non-loopback interfaces)."
        )
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
        _kitchen.stop()
        _kitchen_evidence.stop_rotation_daemon()
        server.server_close()


if __name__ == "__main__":
    main()
