"""Kitchen — local agentic work orchestrator.

Replaces the M1a no-op stub with the real M3 poll → reconcile → dispatch loop
described in docs/KITCHEN.md §8.

Responsibilities:
    - Tick on a fixed interval (poll_seconds, default 5s)
    - Reconcile active runs: bump heartbeats, expire stalls
    - Compute global + per-project slot availability
    - Fetch eligible subjects across registered projects
    - Atomically claim a subject (BEGIN IMMEDIATE) and INSERT a run row at
      status='queued' — the partial unique index `one_active_run_per_subject`
      prevents double-dispatch under any race
    - Spawn a runner thread for each claim and track it for cancellation /
      lifecycle observation

The orchestrator never invokes subprocesses directly — that's the runner's
job. This module is purely scheduling + claim management.

Single-instance assumed (per docs/KITCHEN.md §16). Multi-instance support
would require richer claim_owner reconciliation; out of scope for M3.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from actions import ActorContext, emit_event, utcnow_iso
from db import REGISTRY_PATH
from runners import AgentRunner, RunOutcome, Runner, ScenarioRunner
from workspaces import WorkspaceInfo, create_or_reuse, run_hook, workspace_path_for
from workflow_config import load_workflow_config, load_prompt_template

logger = logging.getLogger(__name__)

# Stall threshold — if a run has no heartbeat for this long, expire it.
STALL_TIMEOUT_S = 600  # 10 min

# Owner tag stamped on every claim — uniquely identifies this orchestrator instance.
_INSTANCE_OWNER = f"kitchen:{os.getpid()}"


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

@dataclass
class _ActiveRun:
    """Tracking metadata for a run currently being executed in a thread."""
    run_id: int
    project_id: str
    subject_type: str
    subject_id: str
    thread: threading.Thread
    cancel_event: threading.Event = field(default_factory=threading.Event)


_started = False
_stop_event: Optional[threading.Event] = None
_loop_thread: Optional[threading.Thread] = None
_active_runs: dict[int, _ActiveRun] = {}
_active_runs_lock = threading.Lock()

# Runner registry — maps runner_kind to Runner instance. Tests can swap.
_RUNNERS: dict[str, Runner] = {"agent": AgentRunner(), "scenario": ScenarioRunner()}


def _runner_kind_for(subject_type: str) -> str:
    """Map subject_type to the runner_kind that handles it.

    M4 convention: tickets → agent (claude/codex), journeys → scenario (Playwright),
    investigations → agent (deferred). The orchestrator stamps this on the
    runs row at claim time.
    """
    if subject_type == "journey":
        return "scenario"
    return "agent"

# Test seam: lets tests inject project paths without touching the real registry.
_PROJECT_PATH_RESOLVER: Optional[Callable[[str], Optional[Path]]] = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start(get_db: Callable[[], sqlite3.Connection], settings: dict | None = None) -> None:
    """Start the orchestrator background thread."""
    global _started, _stop_event, _loop_thread
    if _started:
        logger.warning("kitchen.start called twice — ignoring")
        return
    _stop_event = threading.Event()
    _loop_thread = threading.Thread(
        target=_run_loop,
        args=(get_db, dict(settings or {}), _stop_event),
        name="kitchen-orchestrator",
        daemon=True,
    )
    _loop_thread.start()
    _started = True
    logger.info("kitchen started (M3 orchestrator)")


def stop(timeout: float = 5.0) -> None:
    """Signal the orchestrator to stop, request cancellation on active runs,
    and wait briefly for everyone to finish."""
    global _started, _stop_event, _loop_thread
    if not _started:
        return
    if _stop_event:
        _stop_event.set()
    # Request cancellation on every active runner. They'll transition their
    # rows to 'cancelled' on next checkpoint.
    with _active_runs_lock:
        for ar in list(_active_runs.values()):
            ar.cancel_event.set()
    if _loop_thread:
        _loop_thread.join(timeout=timeout)
    # Drain runner threads.
    with _active_runs_lock:
        threads = [ar.thread for ar in _active_runs.values()]
    for t in threads:
        t.join(timeout=timeout)
    _started = False
    _stop_event = None
    _loop_thread = None
    logger.info("kitchen stopped")


# ---------------------------------------------------------------------------
# Test seams
# ---------------------------------------------------------------------------

def set_project_path_resolver(resolver: Optional[Callable[[str], Optional[Path]]]) -> None:
    """Swap how kitchen finds a project's repo path. Default reads registry.json."""
    global _PROJECT_PATH_RESOLVER
    _PROJECT_PATH_RESOLVER = resolver


def register_runner(kind: str, runner: Runner) -> None:
    """Register/replace a runner instance for a given runner_kind."""
    _RUNNERS[kind] = runner


# ---------------------------------------------------------------------------
# Project resolution
# ---------------------------------------------------------------------------

def _resolve_project_path(project_id: str) -> Optional[Path]:
    """Find the on-disk path for a project. Test seam wins; otherwise read registry."""
    if _PROJECT_PATH_RESOLVER is not None:
        return _PROJECT_PATH_RESOLVER(project_id)
    try:
        import json
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            registry = json.load(f)
        for entry in registry.get("projects", []):
            if entry.get("id") == project_id:
                p = entry.get("path", "")
                if not p:
                    return None
                expanded = os.path.expanduser(p)
                return Path(expanded)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


# ---------------------------------------------------------------------------
# The poll loop
# ---------------------------------------------------------------------------

def _run_loop(get_db: Callable, settings: dict, stop_event: threading.Event) -> None:
    """Tick on a fixed interval until stop_event is set."""
    poll_interval_s = float(settings.get("kitchen_poll_seconds", 5.0))
    while not stop_event.is_set():
        try:
            tick(get_db, settings)
        except Exception:
            logger.exception("kitchen tick failed; will retry")
        stop_event.wait(timeout=poll_interval_s)


def tick(get_db: Callable[[], sqlite3.Connection], settings: dict) -> None:
    """One poll cycle: reconcile → compute slots → dispatch.

    Public so tests can drive it deterministically without spinning the loop.
    """
    _reconcile(get_db)
    _dispatch_eligible(get_db, settings)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _reconcile(get_db: Callable[[], sqlite3.Connection]) -> None:
    """Sweep active runs:
       - drop _active_runs entries whose threads have exited
       - flag stalls (no heartbeat in STALL_TIMEOUT_S) and force them to 'stalled'
    """
    # Drop dead-thread entries from our tracking map.
    with _active_runs_lock:
        for rid in list(_active_runs.keys()):
            if not _active_runs[rid].thread.is_alive():
                del _active_runs[rid]

    # Stall detection runs against the DB (covers the case where this
    # orchestrator was restarted with stale rows from a previous instance).
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, project_id, subject_type, subject_id, heartbeat_at "
            "FROM runs WHERE status IN ('preparing', 'running', 'queued')"
        ).fetchall()
        now_iso = utcnow_iso()
        for r in rows:
            hb = r["heartbeat_at"]
            if not hb:
                continue
            try:
                # Cheap age check: parse the ISO string we wrote ourselves.
                from datetime import datetime, timezone
                hb_dt = datetime.fromisoformat(hb)
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                age_s = (datetime.now(timezone.utc) - hb_dt).total_seconds()
            except (ValueError, TypeError):
                continue
            if age_s > STALL_TIMEOUT_S:
                conn.execute(
                    "UPDATE runs SET status='stalled', finished_at=?, error_class='stalled', "
                    "error_message=? WHERE id = ? AND status IN ('preparing','running','queued')",
                    (now_iso, f"no heartbeat for {int(age_s)}s", r["id"]),
                )
                emit_event(
                    conn, r["project_id"], r["subject_type"], r["subject_id"],
                    "run_stalled",
                    {"run_id": r["id"], "last_heartbeat_age_ms": int(age_s * 1000)},
                    ActorContext.system(),
                )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _count_active_consuming_slots(conn: sqlite3.Connection) -> int:
    """Per docs/KITCHEN.md §8: queued + needs_input do NOT consume slots.
    Active capacity counts only preparing + running.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE status IN ('preparing', 'running')"
    ).fetchone()
    return row["n"] if row else 0


def _count_active_for_project(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE project_id = ? "
        "AND status IN ('preparing', 'running')",
        (project_id,),
    ).fetchone()
    return row["n"] if row else 0


def _dispatch_eligible(get_db: Callable[[], sqlite3.Connection], settings: dict) -> None:
    """Find every eligible ticket across all projects, claim until slots run out."""
    from actions import eligibility as _eligibility

    conn = get_db()
    try:
        # Discover projects from registry; settings can override per project.
        projects = _list_project_ids(conn)
        if not projects:
            return

        # Per-project max from settings (later WORKFLOW.toml may override per project).
        global_cap = int(settings.get("max_concurrent_runs", 3))
        per_project_cap = int(settings.get("max_concurrent_per_project", 1))

        active_total = _count_active_consuming_slots(conn)
        slots = max(0, global_cap - active_total)
        if slots <= 0:
            return

        # For each project, find eligible tickets in priority order.
        for project_id in projects:
            if slots <= 0:
                break
            active_for_proj = _count_active_for_project(conn, project_id)
            project_slots = max(0, per_project_cap - active_for_proj)
            if project_slots <= 0:
                continue

            tickets = conn.execute(
                "SELECT id FROM tickets WHERE project_id = ? AND archived = 0 AND draft = 0 "
                "AND section IN ('Backlog', 'WIP', 'For Review') "
                # Priority: high → low; then created_at oldest first.
                "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
                "         WHEN 'low' THEN 2 ELSE 3 END, created_at ASC, id ASC",
                (project_id,),
            ).fetchall()

            for trow in tickets:
                if slots <= 0 or project_slots <= 0:
                    break
                tid = trow["id"]
                er = _eligibility(conn, project_id, "ticket", tid)
                if not er.eligible:
                    continue
                if _try_claim_and_dispatch(get_db, project_id, "ticket", tid, settings):
                    slots -= 1
                    project_slots -= 1
    finally:
        conn.close()


def _list_project_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT project_id FROM tickets ORDER BY project_id"
    ).fetchall()
    return [r["project_id"] for r in rows]


def _try_claim_and_dispatch(
    get_db: Callable[[], sqlite3.Connection],
    project_id: str,
    subject_type: str,
    subject_id: str,
    settings: dict,
) -> bool:
    """Atomic claim using BEGIN IMMEDIATE + the partial unique index.

    Returns True on successful dispatch, False if already-active or other race.
    """
    project_path = _resolve_project_path(project_id)
    if project_path is None:
        logger.warning("dispatch skipped: no project path for %s", project_id)
        return False
    config = load_workflow_config(project_path)
    config["_prompt_template"] = load_prompt_template(project_path)
    base_ref = (config.get("agent", {}) or {}).get("base_ref", "origin/main")

    # Insert the queued row inside a BEGIN IMMEDIATE tx so a concurrent tick
    # would block; the partial unique index makes the race safe regardless.
    claim_owner = _INSTANCE_OWNER
    runner_kind = _runner_kind_for(subject_type)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO runs "
                "(project_id, subject_type, subject_id, runner_kind, status, "
                " claimed_at, claim_owner, heartbeat_at, started_at, triggered_by) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, 'scheduled')",
                (project_id, subject_type, subject_id, runner_kind,
                 utcnow_iso(), claim_owner, utcnow_iso(), utcnow_iso()),
            )
            run_id = cur.lastrowid
            emit_event(conn, project_id, subject_type, subject_id, "run_started",
                       {"run_id": run_id, "runner_kind": runner_kind,
                        "triggered_by": "scheduled"},
                       ActorContext.system())
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return False  # Another tick or human action beat us to it.
    finally:
        conn.close()

    # Provision workspace (worktree) — outside the claim tx, but before the
    # runner thread starts, so any failure marks the run failed cleanly.
    try:
        ws = create_or_reuse(project_path, project_id, subject_type, subject_id, base_ref=base_ref)
    except ValueError as e:
        # Workspace setup failed — mark the run failed in a fresh tx.
        c = get_db()
        try:
            c.execute(
                "UPDATE runs SET status='failed', error_class='workspace_setup', "
                "error_message=?, finished_at=?, heartbeat_at=? WHERE id = ?",
                (str(e), utcnow_iso(), utcnow_iso(), run_id),
            )
            emit_event(c, project_id, subject_type, subject_id, "run_failed",
                       {"run_id": run_id, "error_class": "workspace_setup",
                        "error_message": str(e)}, ActorContext.system())
            c.commit()
        finally:
            c.close()
        return False

    # Spawn the runner thread.
    runner = _RUNNERS.get(runner_kind)
    if runner is None:
        logger.error("no runner registered for kind %r", runner_kind)
        return False

    cancel = threading.Event()
    t = threading.Thread(
        target=_runner_thread,
        args=(runner, run_id, project_id, subject_type, subject_id, ws, config, get_db, cancel),
        name=f"kitchen-runner-{run_id}",
        daemon=True,
    )
    with _active_runs_lock:
        _active_runs[run_id] = _ActiveRun(
            run_id=run_id, project_id=project_id,
            subject_type=subject_type, subject_id=subject_id,
            thread=t, cancel_event=cancel,
        )
    t.start()
    return True


def _runner_thread(
    runner: Runner,
    run_id: int,
    project_id: str,
    subject_type: str,
    subject_id: str,
    ws: WorkspaceInfo,
    config: dict,
    get_db: Callable[[], sqlite3.Connection],
    cancel_event: threading.Event,
) -> None:
    """Thread body — invokes runner.execute() and cleans up."""
    try:
        runner.execute(run_id, project_id, subject_type, subject_id,
                       ws, config, get_db, cancel_event=cancel_event)
    except Exception:
        logger.exception("runner thread crashed for run %d", run_id)
        # Best-effort: mark the run failed if it isn't terminal already.
        try:
            c = get_db()
            try:
                c.execute(
                    "UPDATE runs SET status='failed', error_class='runner_crash', "
                    "error_message='runner thread crashed', finished_at=?, heartbeat_at=? "
                    "WHERE id = ? AND status NOT IN ('succeeded','failed','stalled','cancelled')",
                    (utcnow_iso(), utcnow_iso(), run_id),
                )
                emit_event(c, project_id, subject_type, subject_id, "run_failed",
                           {"run_id": run_id, "error_class": "runner_crash",
                            "error_message": "runner thread crashed"},
                           ActorContext.system())
                c.commit()
            finally:
                c.close()
        except Exception:
            pass
    finally:
        with _active_runs_lock:
            _active_runs.pop(run_id, None)


# ---------------------------------------------------------------------------
# External hooks — invoked by serve.py for human-triggered actions
# ---------------------------------------------------------------------------

def request_cancel(run_id: int) -> bool:
    """Signal an active run to cancel. Returns True iff the run was tracked."""
    with _active_runs_lock:
        ar = _active_runs.get(run_id)
        if ar is None:
            return False
        ar.cancel_event.set()
        return True


def is_running(run_id: int) -> bool:
    """True iff this orchestrator currently has a thread for this run."""
    with _active_runs_lock:
        return run_id in _active_runs


def active_runs_snapshot() -> list[dict]:
    """Diagnostic surface — list of currently-tracked runs."""
    with _active_runs_lock:
        return [
            {"run_id": ar.run_id, "project_id": ar.project_id,
             "subject_type": ar.subject_type, "subject_id": ar.subject_id,
             "alive": ar.thread.is_alive()}
            for ar in _active_runs.values()
        ]


def trigger_run(
    get_db: Callable[[], sqlite3.Connection],
    project_id: str,
    subject_type: str,
    subject_id: str,
    settings: dict,
    triggered_by: str = "human",
) -> Optional[int]:
    """Manually trigger a run for a subject. Returns the new run_id on success.

    Used by POST /api/tickets/{id}/run-now. Like dispatch, but bypasses the
    eligibility gate — the API layer is expected to gate on its own (returning
    422 with reasons when ineligible) before calling here.
    """
    project_path = _resolve_project_path(project_id)
    if project_path is None:
        return None
    config = load_workflow_config(project_path)
    config["_prompt_template"] = load_prompt_template(project_path)
    base_ref = (config.get("agent", {}) or {}).get("base_ref", "origin/main")

    # Claim atomically.
    runner_kind = _runner_kind_for(subject_type)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO runs "
                "(project_id, subject_type, subject_id, runner_kind, status, "
                " claimed_at, claim_owner, heartbeat_at, started_at, triggered_by) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
                (project_id, subject_type, subject_id, runner_kind,
                 utcnow_iso(), _INSTANCE_OWNER, utcnow_iso(), utcnow_iso(), triggered_by),
            )
            run_id = cur.lastrowid
            emit_event(conn, project_id, subject_type, subject_id, "run_started",
                       {"run_id": run_id, "runner_kind": runner_kind,
                        "triggered_by": triggered_by},
                       ActorContext.human() if triggered_by == "human" else ActorContext.system())
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return None
    finally:
        conn.close()

    try:
        ws = create_or_reuse(project_path, project_id, subject_type, subject_id, base_ref=base_ref)
    except ValueError as e:
        c = get_db()
        try:
            c.execute(
                "UPDATE runs SET status='failed', error_class='workspace_setup', "
                "error_message=?, finished_at=?, heartbeat_at=? WHERE id = ?",
                (str(e), utcnow_iso(), utcnow_iso(), run_id),
            )
            emit_event(c, project_id, subject_type, subject_id, "run_failed",
                       {"run_id": run_id, "error_class": "workspace_setup",
                        "error_message": str(e)}, ActorContext.system())
            c.commit()
        finally:
            c.close()
        return run_id  # Failed but row exists — return id so caller can show it.

    runner = _RUNNERS.get(runner_kind)
    if runner is None:
        return run_id

    cancel = threading.Event()
    t = threading.Thread(
        target=_runner_thread,
        args=(runner, run_id, project_id, subject_type, subject_id, ws, config, get_db, cancel),
        name=f"kitchen-runner-{run_id}",
        daemon=True,
    )
    with _active_runs_lock:
        _active_runs[run_id] = _ActiveRun(
            run_id=run_id, project_id=project_id,
            subject_type=subject_type, subject_id=subject_id,
            thread=t, cancel_event=cancel,
        )
    t.start()
    return run_id
