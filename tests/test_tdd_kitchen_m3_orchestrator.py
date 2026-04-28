"""TDD tests for Kitchen M3 — orchestrator tick (kitchen.py).

Hermetic. Uses a mock runner that records dispatch calls instead of spawning
real subprocesses, so we can verify scheduling/claim/cap behavior fast and
deterministically. The real subprocess path is covered by
test_tdd_kitchen_m3_runners.py.
"""

import importlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def repo(tmp_path):
    upstream = tmp_path / "upstream.git"
    work = tmp_path / "project_repo"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(upstream)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(work)],
                   check=True, capture_output=True)
    for k, v in [("user.email", "test@example.invalid"), ("user.name", "Test")]:
        subprocess.run(["git", "-C", str(work), "config", k, v], check=True, capture_output=True)
    (work / "README.md").write_text("# project\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(upstream)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "main"],
                   check=True, capture_output=True)
    return work


class _RecordingRunner:
    """Captures dispatch calls instead of running anything. Marks the run
    succeeded synchronously inside execute() so the next tick sees a clean slot."""
    runner_kind = "agent"

    def __init__(self, mark_succeeded=True, hold_open_event: threading.Event | None = None):
        self.calls: list[tuple[int, str, str]] = []
        self._mark_succeeded = mark_succeeded
        self._hold_open = hold_open_event

    def execute(self, run_id, project_id, subject_type, subject_id,
                workspace, config, conn_factory, cancel_event=None):
        self.calls.append((run_id, project_id, subject_id))
        # Match real AgentRunner behavior: flip to 'running' immediately so
        # the orchestrator's slot accounting (which counts only preparing|running)
        # sees this run as active.
        c = conn_factory()
        try:
            from runners import _set_run_status
            _set_run_status(c, run_id, "running")
            c.commit()
        finally:
            c.close()
        if self._hold_open is not None:
            # Block until the test releases us — simulates a long-running agent.
            self._hold_open.wait(timeout=5)
        if self._mark_succeeded:
            c = conn_factory()
            try:
                from runners import _set_run_status
                _set_run_status(c, run_id, "succeeded", finished=True, summary="ok")
                c.commit()
            finally:
                c.close()
        from runners import RunOutcome
        return RunOutcome(run_id=run_id, final_status="succeeded",
                          duration_ms=1, summary="ok")


@pytest.fixture
def env(tmp_path, monkeypatch, repo):
    """Wires up DB + project resolver + monkey-patched workspaces root."""
    import constants
    db_file = tmp_path / "tickets.db"
    monkeypatch.setattr(constants, "DASHBOARD_DIR", tmp_path / ".claude" / "ticket-takeaway")
    (tmp_path / ".claude" / "ticket-takeaway").mkdir(parents=True, exist_ok=True)

    import db
    importlib.reload(db)
    import workspaces
    importlib.reload(workspaces)
    import runners
    importlib.reload(runners)
    import kitchen
    importlib.reload(kitchen)

    c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
    db.init_db(c); c.close()

    def conn_factory():
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    # Test seam: every project_id resolves to the test repo.
    kitchen.set_project_path_resolver(lambda pid: repo)

    return {
        "db_file": db_file,
        "conn_factory": conn_factory,
        "kitchen": kitchen,
        "workspaces": workspaces,
        "runners": runners,
        "repo": repo,
    }


def _seed_eligible(env, ticket_id="B-1", project_id="p", priority="medium"):
    """Insert an eligible (auto + tests-bypass + criteria + description) ticket."""
    from actions import set_automation_mode, set_no_test_required, ActorContext
    c = env["conn_factory"]()
    c.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description, priority) "
        "VALUES (?, ?, 'T', 'Backlog', 'specified', 'desc', ?)",
        (ticket_id, project_id, priority),
    )
    c.execute(
        "INSERT INTO acceptance_criteria (ticket_id, project_id, text) VALUES (?, ?, 'X')",
        (ticket_id, project_id),
    )
    c.commit(); c.close()
    c = env["conn_factory"]()
    set_no_test_required(c, project_id, ticket_id, True, "docs only", ActorContext.human())
    set_automation_mode(c, project_id, "ticket", ticket_id, "auto", ActorContext.human())
    c.commit(); c.close()


def _runs(env, project_id="p"):
    c = env["conn_factory"]()
    rows = c.execute(
        "SELECT id, status, project_id, subject_id FROM runs WHERE project_id = ? ORDER BY id",
        (project_id,),
    ).fetchall()
    c.close()
    return rows


# ---------------------------------------------------------------------------
# Empty / no-op cases
# ---------------------------------------------------------------------------

class TestEmptyState:
    def test_tick_with_no_projects_is_noop(self, env):
        # No tickets exist at all → no projects discovered → no dispatch.
        env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 3})
        assert _runs(env) == []

    def test_tick_with_ineligible_tickets_dispatches_nothing(self, env):
        # Manual mode → not eligible.
        c = env["conn_factory"]()
        c.execute("INSERT INTO tickets (id, project_id, title, section, status, description) "
                  "VALUES ('B-1', 'p', 'T', 'Backlog', 'specified', 'd')")
        c.execute("INSERT INTO acceptance_criteria (ticket_id, project_id, text) VALUES ('B-1', 'p', 'X')")
        c.commit(); c.close()
        env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 3})
        assert _runs(env) == []


# ---------------------------------------------------------------------------
# Dispatch happy path
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_eligible_ticket_dispatches_one_run(self, env):
        rec = _RecordingRunner(mark_succeeded=True)
        env["kitchen"].register_runner("agent", rec)
        _seed_eligible(env, "B-1")
        env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 3, "max_concurrent_per_project": 1})
        # Wait briefly for the runner thread to mark succeeded.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not rec.calls:
            time.sleep(0.05)
        assert len(rec.calls) == 1
        # Run row exists and reached terminal.
        deadline = time.monotonic() + 3
        runs = _runs(env)
        while time.monotonic() < deadline and (not runs or runs[0]["status"] in ("queued", "preparing", "running")):
            time.sleep(0.05)
            runs = _runs(env)
        assert len(runs) == 1
        assert runs[0]["status"] == "succeeded"

    def test_two_eligible_with_per_project_cap_1_dispatches_one_at_a_time(self, env):
        # Hold the runner open so the first run stays "active" while we tick again.
        gate = threading.Event()
        rec = _RecordingRunner(mark_succeeded=True, hold_open_event=gate)
        env["kitchen"].register_runner("agent", rec)
        _seed_eligible(env, "B-1")
        _seed_eligible(env, "B-2")

        env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 3, "max_concurrent_per_project": 1})
        # Wait for first dispatch.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(rec.calls) < 1:
            time.sleep(0.05)
        assert len(rec.calls) == 1

        # Second tick — first run is still running (gate not released) →
        # per-project cap should block dispatching B-2.
        env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 3, "max_concurrent_per_project": 1})
        time.sleep(0.2)  # let any (incorrect) thread finish
        assert len(rec.calls) == 1, f"per-project cap violated: {rec.calls}"

        # Release the runner; on the next tick B-2 should pick up.
        gate.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(rec.calls) < 2:
            env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 3, "max_concurrent_per_project": 1})
            time.sleep(0.1)
        assert len(rec.calls) == 2

    def test_priority_high_dispatched_first(self, env):
        rec = _RecordingRunner(mark_succeeded=True)
        env["kitchen"].register_runner("agent", rec)
        _seed_eligible(env, "B-low",  priority="low")
        _seed_eligible(env, "B-high", priority="high")
        env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 1, "max_concurrent_per_project": 1})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not rec.calls:
            time.sleep(0.05)
        assert rec.calls[0][2] == "B-high", f"expected high-priority first; got {rec.calls}"


# ---------------------------------------------------------------------------
# Reconciliation — stall detection
# ---------------------------------------------------------------------------

class TestReconciliation:
    def test_stale_heartbeat_marks_run_stalled(self, env):
        # Insert a run row with a heartbeat from way in the past.
        old_hb = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        c = env["conn_factory"]()
        cur = c.execute(
            "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, "
            " triggered_by, heartbeat_at, started_at) "
            "VALUES ('p', 'ticket', 'B-stalled', 'agent', 'running', 'human', ?, ?)",
            (old_hb, old_hb),
        )
        rid = cur.lastrowid
        # Need the ticket to exist for the eligibility query later (also seed
        # something so _list_project_ids returns something — irrelevant for
        # reconcile but cleaner state).
        c.execute("INSERT INTO tickets (id, project_id, title, section, status, description) "
                  "VALUES ('B-stalled', 'p', 'X', 'WIP', 'in-progress', 'd')")
        c.commit(); c.close()

        env["kitchen"].tick(env["conn_factory"], {})

        c = env["conn_factory"]()
        row = c.execute("SELECT status, error_class FROM runs WHERE id = ?", (rid,)).fetchone()
        ev = c.execute(
            "SELECT event_kind, actor_type FROM activity_events "
            "WHERE subject_id = 'B-stalled' AND event_kind = 'run_stalled'"
        ).fetchone()
        c.close()
        assert row["status"] == "stalled"
        assert row["error_class"] == "stalled"
        assert ev is not None
        assert ev["actor_type"] == "system"


# ---------------------------------------------------------------------------
# Manual trigger — trigger_run() bypasses eligibility (caller's responsibility)
# ---------------------------------------------------------------------------

class TestTriggerRun:
    def test_trigger_run_creates_run_for_ineligible_ticket(self, env):
        rec = _RecordingRunner(mark_succeeded=True)
        env["kitchen"].register_runner("agent", rec)
        # Ticket exists but is in 'manual' mode → ineligible — trigger_run should still work.
        c = env["conn_factory"]()
        c.execute("INSERT INTO tickets (id, project_id, title, section, status, description) "
                  "VALUES ('B-1', 'p', 'T', 'Backlog', 'specified', 'd')")
        c.commit(); c.close()
        rid = env["kitchen"].trigger_run(env["conn_factory"], "p", "ticket", "B-1", {})
        assert rid is not None
        # Wait for the runner to mark succeeded.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not rec.calls:
            time.sleep(0.05)
        assert len(rec.calls) == 1
        assert rec.calls[0] == (rid, "p", "B-1")

    def test_trigger_run_emits_run_started_with_human_actor(self, env):
        rec = _RecordingRunner(mark_succeeded=True)
        env["kitchen"].register_runner("agent", rec)
        c = env["conn_factory"]()
        c.execute("INSERT INTO tickets (id, project_id, title, section, status, description) "
                  "VALUES ('B-1', 'p', 'T', 'Backlog', 'specified', 'd')")
        c.commit(); c.close()
        rid = env["kitchen"].trigger_run(env["conn_factory"], "p", "ticket", "B-1", {})
        c = env["conn_factory"]()
        ev = c.execute(
            "SELECT actor_type FROM activity_events WHERE event_kind='run_started' AND payload_json LIKE ?",
            (f'%"run_id": {rid}%',),
        ).fetchone()
        c.close()
        assert ev is not None
        assert ev["actor_type"] == "human"

    def test_trigger_run_blocked_when_active_run_exists(self, env):
        rec = _RecordingRunner(mark_succeeded=True, hold_open_event=threading.Event())
        env["kitchen"].register_runner("agent", rec)
        c = env["conn_factory"]()
        c.execute("INSERT INTO tickets (id, project_id, title, section, status, description) "
                  "VALUES ('B-1', 'p', 'T', 'Backlog', 'specified', 'd')")
        c.commit(); c.close()
        rid1 = env["kitchen"].trigger_run(env["conn_factory"], "p", "ticket", "B-1", {})
        assert rid1 is not None
        # Second trigger_run for the same subject should fail (partial unique index).
        rid2 = env["kitchen"].trigger_run(env["conn_factory"], "p", "ticket", "B-1", {})
        assert rid2 is None


# ---------------------------------------------------------------------------
# Cancellation surface
# ---------------------------------------------------------------------------

class TestCancellation:
    def test_request_cancel_sets_event_for_active_run(self, env):
        gate = threading.Event()
        rec = _RecordingRunner(mark_succeeded=True, hold_open_event=gate)
        env["kitchen"].register_runner("agent", rec)
        c = env["conn_factory"]()
        c.execute("INSERT INTO tickets (id, project_id, title, section, status, description) "
                  "VALUES ('B-1', 'p', 'T', 'Backlog', 'specified', 'd')")
        c.commit(); c.close()
        rid = env["kitchen"].trigger_run(env["conn_factory"], "p", "ticket", "B-1", {})
        # Wait until the runner has been entered.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not env["kitchen"].is_running(rid):
            time.sleep(0.05)
        assert env["kitchen"].is_running(rid)
        ok = env["kitchen"].request_cancel(rid)
        assert ok is True
        # Release the gate so the recording runner returns.
        gate.set()

    def test_request_cancel_returns_false_for_unknown_run(self, env):
        assert env["kitchen"].request_cancel(99999) is False
