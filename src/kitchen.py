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
from runners import AgentRunner, NoopRunner, RunOutcome, Runner, ScenarioRunner
from workspaces import WorkspaceInfo, create_or_reuse, run_hook, workspace_path_for
from workflow_config import load_workflow_config, load_prompt_template

# Feature flag key stored in the settings table.
_USE_DB_WORKFLOWS_KEY = "kitchen.use_db_workflows"

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

# M6: pause flag. True (default) = orchestrator polls + reconciles but does
# NOT auto-dispatch. The user has to explicitly resume() to start runs flowing.
# Manual `trigger_run` (per-ticket "Run now") works regardless — pressing
# that button IS the explicit OK. Settings table persists last user choice.
_paused = True
_paused_lock = threading.Lock()
_PAUSED_SETTING_KEY = "kitchen.paused"

# Runner registry — maps runner_kind to Runner instance. Tests can swap.
_RUNNERS: dict[str, Runner] = {
    "agent": AgentRunner(),
    "scenario": ScenarioRunner(),
    "noop": NoopRunner(),
}


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
    """Start the orchestrator background thread.

    On first start the paused flag is loaded from the settings table; if the
    setting is missing the default is paused=True. The user explicitly resumes
    via the UI / API to start auto-dispatch.
    """
    global _started, _stop_event, _loop_thread, _paused
    if _started:
        logger.warning("kitchen.start called twice — ignoring")
        return
    # Load persisted pause state. Default = paused.
    try:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (_PAUSED_SETTING_KEY,)
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            with _paused_lock:
                _paused = (str(row[0]).lower() != "false")  # missing/anything-else = paused
    except Exception:
        # Pre-migration or transient DB issue — stay paused (the safe default).
        pass

    _stop_event = threading.Event()
    _loop_thread = threading.Thread(
        target=_run_loop,
        args=(get_db, dict(settings or {}), _stop_event),
        name="kitchen-orchestrator",
        daemon=True,
    )
    _loop_thread.start()
    _started = True
    state = "paused" if is_paused() else "running"
    logger.info("kitchen started (M3 orchestrator) — %s", state)


# ---------------------------------------------------------------------------
# Pause / resume — M6
# ---------------------------------------------------------------------------

def is_paused() -> bool:
    with _paused_lock:
        return _paused


def pause(get_db: Optional[Callable[[], sqlite3.Connection]] = None,
          actor=None, reason: str = "") -> bool:
    """Pause auto-dispatch. Returns True if state actually changed.

    Reconciliation (stall detection) keeps running; new runs do NOT get
    claimed by the polling tick. Manual `trigger_run` calls still work.
    Idempotent: pausing while already paused is a no-op.
    """
    global _paused
    with _paused_lock:
        if _paused:
            return False
        _paused = True
    _persist_paused(get_db, True)
    _emit_pause_event(get_db, "paused", reason, actor)
    logger.info("kitchen paused (reason=%r)", reason)
    return True


def resume(get_db: Optional[Callable[[], sqlite3.Connection]] = None,
           actor=None, reason: str = "") -> bool:
    """Resume auto-dispatch. Returns True if state actually changed.

    Idempotent: resuming while already running is a no-op.
    """
    global _paused
    with _paused_lock:
        if not _paused:
            return False
        _paused = False
    _persist_paused(get_db, False)
    _emit_pause_event(get_db, "resumed", reason, actor)
    logger.info("kitchen resumed (reason=%r)", reason)
    return True


def _persist_paused(get_db, value: bool) -> None:
    """Best-effort write of the pause flag to the settings table."""
    if get_db is None:
        return
    try:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (_PAUSED_SETTING_KEY, "true" if value else "false"),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("failed to persist kitchen.paused")


def _emit_pause_event(get_db, kind: str, reason: str, actor) -> None:
    """Audit kitchen pause/resume so the History tab can show why work stopped.

    Logged as a system-level event on a synthetic 'kitchen/lifecycle' subject;
    not bound to any one project. Best-effort — never breaks the toggle.
    """
    if get_db is None:
        return
    try:
        from actions import emit_event, ActorContext
        if actor is None:
            actor = ActorContext.system()
        conn = get_db()
        try:
            # Use a sentinel project_id so it's surfaceable in any cross-project view.
            emit_event(
                conn, "_kitchen", "investigation", "lifecycle",
                f"kitchen_{kind}",  # kitchen_paused | kitchen_resumed
                {"reason": reason},
                actor,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("failed to emit kitchen %s event", kind)


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
    """One poll cycle: reconcile → (if not paused) compute slots → dispatch.

    Reconciliation runs even while paused — stall detection is safety, not new
    work. Auto-dispatch is gated on the paused flag.

    Public so tests can drive it deterministically without spinning the loop.
    """
    _reconcile(get_db)
    if is_paused():
        return
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


def get_use_db_workflows(conn: sqlite3.Connection) -> bool:
    """Read the kitchen.use_db_workflows feature flag from settings.

    Default is False (legacy path) per the Phase 2 spec.
    Memory: "Settings boolean strings — SQLite stores strings, explicit comparison needed".
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (_USE_DB_WORKFLOWS_KEY,)
    ).fetchone()
    if row is None:
        return False
    return str(row[0]).lower() == "true"


def _dispatch_eligible(get_db: Callable[[], sqlite3.Connection], settings: dict) -> None:
    """Route to the DB-workflow path or legacy path based on feature flag."""
    conn = get_db()
    try:
        use_db = get_use_db_workflows(conn)
    finally:
        conn.close()

    if use_db:
        _dispatch_via_workflows(get_db, settings)
    else:
        _dispatch_via_legacy(get_db, settings)


def _dispatch_via_legacy(get_db: Callable[[], sqlite3.Connection], settings: dict) -> None:
    """Legacy dispatch path: evaluates tickets using _ticket_eligibility hardcoded predicate."""
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


def _dispatch_via_workflows(get_db: Callable[[], sqlite3.Connection], settings: dict) -> None:
    """DB-workflow dispatch path: evaluates tickets against enabled workflow triggers.

    System workflows (system=1) bypass the `automation_mode='auto'` filter so
    they can fire against ANY non-draft, non-archived ticket. User workflows
    (system=0) keep the legacy auto-mode-only behaviour.

    For each (workflow, ticket) pair, evaluates the trigger; first matching
    workflow per ticket dispatches a run (one workflow per tick per subject).
    """
    import json as _json
    from conditions import build_subject_context, evaluate_trigger
    from actions import _has_active_run  # type: ignore[import]

    conn = get_db()
    try:
        projects = _list_project_ids(conn)
        if not projects:
            return

        global_cap = int(settings.get("max_concurrent_runs", 3))
        per_project_cap = int(settings.get("max_concurrent_per_project", 1))

        active_total = _count_active_consuming_slots(conn)
        slots = max(0, global_cap - active_total)
        if slots <= 0:
            return

        for project_id in projects:
            if slots <= 0:
                break
            active_for_proj = _count_active_for_project(conn, project_id)
            project_slots = max(0, per_project_cap - active_for_proj)
            if project_slots <= 0:
                continue

            # Load enabled workflows for this project. System workflows come first
            # so user-defined workflows can shadow them by being evaluated later
            # (first workflow match wins per subject).
            workflows = conn.execute(
                "SELECT id, name, trigger_json, on_success_json, steps, system "
                "FROM workflows "
                "WHERE enabled = 1 AND project_id = ? "
                "ORDER BY system DESC, id ASC",
                (project_id,),
            ).fetchall()

            if not workflows:
                continue

            # Two candidate subject pools:
            #   - auto_subjects:   tickets with automation_mode='auto' (used by user workflows)
            #   - all_subjects:    every non-draft/non-archived ticket (used by system workflows)
            # System workflows must fire regardless of automation_mode, so we
            # evaluate them against the wider pool.
            auto_subjects = conn.execute(
                "SELECT t.id FROM tickets t "
                "JOIN automation_subjects s ON s.subject_id = t.id "
                "  AND s.project_id = t.project_id AND s.subject_type = 'ticket' "
                "WHERE t.project_id = ? AND t.archived = 0 AND t.draft = 0 "
                "  AND s.automation_mode = 'auto' "
                "ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
                "         WHEN 'low' THEN 2 ELSE 3 END, t.created_at ASC, t.id ASC",
                (project_id,),
            ).fetchall()
            all_subjects = conn.execute(
                "SELECT t.id FROM tickets t "
                "WHERE t.project_id = ? AND t.archived = 0 AND t.draft = 0 "
                "ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
                "         WHEN 'low' THEN 2 ELSE 3 END, t.created_at ASC, t.id ASC",
                (project_id,),
            ).fetchall()

            auto_ids = {s["id"] for s in auto_subjects}
            # Iterate over the union (system workflows can fire on any ticket;
            # user workflows only on auto-mode tickets — we'll filter per-workflow).
            seen: set = set()
            ordered_ids: list[str] = []
            for s in all_subjects:
                if s["id"] not in seen:
                    seen.add(s["id"])
                    ordered_ids.append(s["id"])

            for ticket_id in ordered_ids:
                if slots <= 0 or project_slots <= 0:
                    break

                # Skip if already has an active run.
                if _has_active_run(conn, project_id, "ticket", ticket_id):
                    continue

                # Try each workflow in order — first match dispatches.
                for wf in workflows:
                    is_system = bool(wf["system"])
                    # User workflows still respect automation_mode='auto' filter.
                    if not is_system and ticket_id not in auto_ids:
                        continue

                    try:
                        trigger_raw = wf["trigger_json"]
                        # Workflows with null trigger_json are manual-only — skip.
                        if trigger_raw is None or trigger_raw == "null":
                            continue
                        trigger = (
                            _json.loads(trigger_raw) if isinstance(trigger_raw, str)
                            else trigger_raw
                        )
                        ctx = build_subject_context(conn, project_id, ticket_id)
                        passed, reasons = evaluate_trigger(trigger, ctx)
                    except Exception:
                        logger.exception(
                            "workflow trigger evaluation failed for workflow %r ticket %r",
                            wf["id"], ticket_id,
                        )
                        continue

                    if not passed:
                        continue

                    # Parse workflow metadata to pass into the run.
                    try:
                        steps = _json.loads(wf["steps"]) if isinstance(wf["steps"], str) else wf["steps"]
                    except Exception:
                        steps = []
                    on_success = None
                    try:
                        on_success_raw = wf["on_success_json"]
                        on_success = (
                            _json.loads(on_success_raw)
                            if isinstance(on_success_raw, str) and on_success_raw
                            else (on_success_raw or {})
                        )
                    except Exception:
                        on_success = {}

                    workflow_meta = {
                        "workflow_id": wf["id"],
                        "workflow_name": wf["name"],
                        "step_index": 0,
                        "step_count": len(steps),
                        "on_success": on_success,
                        "steps": steps,
                    }

                    if _try_claim_and_dispatch(
                        get_db, project_id, "ticket", ticket_id, settings,
                        workflow_meta=workflow_meta,
                    ):
                        slots -= 1
                        project_slots -= 1
                    break  # One workflow per subject per tick regardless of claim success.
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
    workflow_meta: Optional[dict] = None,
) -> bool:
    """Atomic claim using BEGIN IMMEDIATE + the partial unique index.

    Returns True on successful dispatch, False if already-active or other race.

    workflow_meta: if provided (DB-workflow path), stored in metadata_json and used
    to render the workflow's prompt_template instead of PROMPT.md.
    """
    import json as _json

    project_path = _resolve_project_path(project_id)
    if project_path is None:
        logger.warning("dispatch skipped: no project path for %s", project_id)
        return False
    config = load_workflow_config(project_path)
    config["_prompt_template"] = load_prompt_template(project_path)
    base_ref = (config.get("agent", {}) or {}).get("base_ref", "origin/main")

    # Detect zero-step workflows (pure mutation rules — e.g. system workflows
    # like parent-promote and auto-accept). These skip workspace + agent
    # subprocess and run via NoopRunner, which only applies on_success effects.
    is_noop_workflow = bool(
        workflow_meta and not (workflow_meta.get("steps") or [])
    )

    # When a workflow is specified, override the prompt template with the
    # workflow step's prompt_template (substitution happens in the runner).
    if workflow_meta:
        steps = workflow_meta.get("steps", [])
        step_index = workflow_meta.get("step_index", 0)
        if steps and step_index < len(steps):
            step_template = steps[step_index].get("prompt_template", "")
            if step_template:
                config["_prompt_template"] = step_template
        # Also store workflow context in config so the runner can apply on_success.
        config["_workflow_meta"] = workflow_meta

    # Insert the queued row inside a BEGIN IMMEDIATE tx so a concurrent tick
    # would block; the partial unique index makes the race safe regardless.
    claim_owner = _INSTANCE_OWNER
    runner_kind = "noop" if is_noop_workflow else _runner_kind_for(subject_type)
    metadata = _json.dumps(workflow_meta) if workflow_meta else "{}"
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO runs "
                "(project_id, subject_type, subject_id, runner_kind, status, "
                " claimed_at, claim_owner, heartbeat_at, started_at, triggered_by, "
                " metadata_json) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, 'scheduled', ?)",
                (project_id, subject_type, subject_id, runner_kind,
                 utcnow_iso(), claim_owner, utcnow_iso(), utcnow_iso(), metadata),
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
    # Noop workflows (zero steps) need no workspace; we pass a stub.
    if is_noop_workflow:
        ws = WorkspaceInfo(
            path=Path(project_path),
            branch="",
            base_ref=base_ref,
            is_git_worktree=False,
            created_now=False,
            bootstrapped=True,
        )
    else:
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
