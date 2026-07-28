"""TDD tests for Kitchen M6 — pause/resume + simulation mode.

The orchestrator defaults to paused (nothing auto-dispatches). The user has
to explicitly resume(). Manual trigger_run() works regardless — pressing
"Run now" is itself the explicit OK.
"""

import importlib
import os
import sqlite3
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def repo(tmp_path):
    upstream = tmp_path / "upstream.git"
    work = tmp_path / "project_repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(upstream)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(work)],
        check=True,
        capture_output=True,
    )
    for k, v in [("user.email", "test@example.invalid"), ("user.name", "Test")]:
        subprocess.run(
            ["git", "-C", str(work), "config", k, v], check=True, capture_output=True
        )
    (work / "README.md").write_text("# project\n")
    subprocess.run(
        ["git", "-C", str(work), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(upstream)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )
    return work


class _RecordingRunner:
    """Same shape as the orchestrator-test runner: records calls + flips
    status to running so slot accounting sees the run as active."""

    runner_kind = "agent"

    def __init__(self):
        self.calls: list = []

    def execute(
        self,
        run_id,
        project_id,
        subject_type,
        subject_id,
        workspace,
        config,
        conn_factory,
        cancel_event=None,
    ):
        self.calls.append((run_id, subject_id))
        c = conn_factory()
        try:
            from runners import _set_run_status

            _set_run_status(c, run_id, "running")
            c.commit()
            _set_run_status(c, run_id, "succeeded", finished=True, summary="ok")
            c.commit()
        finally:
            c.close()
        from runners import RunOutcome

        return RunOutcome(
            run_id=run_id, final_status="succeeded", duration_ms=1, summary="ok"
        )


@pytest.fixture
def env(tmp_path, monkeypatch, repo):
    import constants

    db_file = tmp_path / "tickets.db"
    monkeypatch.setattr(constants, "DASHBOARD_DIR", tmp_path / ".claude" / "tt")
    (tmp_path / ".claude" / "tt").mkdir(parents=True, exist_ok=True)

    import db

    importlib.reload(db)
    import workspaces

    importlib.reload(workspaces)
    import runners

    importlib.reload(runners)
    import kitchen

    importlib.reload(kitchen)

    c = sqlite3.connect(db_file)
    c.row_factory = sqlite3.Row
    db.init_db(c)
    c.close()

    def conn_factory():
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        return c

    kitchen.set_project_path_resolver(lambda pid: repo)
    return {"db_file": db_file, "conn_factory": conn_factory, "kitchen": kitchen}


def _seed_eligible(env, tid="B-1"):
    from actions import ActorContext, set_automation_mode, set_no_test_required

    c = env["conn_factory"]()
    c.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description) "
        "VALUES (?, 'p', 'T', 'Backlog', 'specified', 'd')",
        (tid,),
    )
    c.execute(
        "INSERT INTO acceptance_criteria (ticket_id, project_id, text) VALUES (?, 'p', 'X')",
        (tid,),
    )
    # Entry gate into automation: eligibility requires a declared spec lane.
    c.execute(
        "INSERT OR REPLACE INTO readiness_flags (ticket_id, project_id, flag, content, set_by) "
        "VALUES (?, 'p', 'spec', ?, 'test')",
        (tid, f"B:{tid.lower()}-test-change"),
    )
    set_no_test_required(c, "p", tid, True, "docs only", ActorContext.human())
    set_automation_mode(c, "p", "ticket", tid, "auto", ActorContext.human())
    c.commit()
    c.close()


def _runs(env):
    c = env["conn_factory"]()
    rows = c.execute("SELECT id, status, subject_id FROM runs ORDER BY id").fetchall()
    c.close()
    return rows


# ---------------------------------------------------------------------------
# Default state
# ---------------------------------------------------------------------------


class TestDefaultState:
    def test_kitchen_starts_paused(self, env):
        # is_paused() returns True before any pause/resume call.
        assert env["kitchen"].is_paused() is True

    def test_paused_tick_does_not_dispatch(self, env):
        rec = _RecordingRunner()
        env["kitchen"].register_runner("agent", rec)
        _seed_eligible(env, "B-1")
        # Default = paused; tick should NOT pick up the eligible ticket.
        env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 3})
        time.sleep(0.1)
        assert rec.calls == []
        assert _runs(env) == []


# ---------------------------------------------------------------------------
# Resume → dispatch flows again
# ---------------------------------------------------------------------------


class TestResumeRestoresDispatch:
    def test_resume_then_tick_dispatches(self, env):
        rec = _RecordingRunner()
        env["kitchen"].register_runner("agent", rec)
        _seed_eligible(env, "B-1")
        env["kitchen"].resume(env["conn_factory"])
        env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 3})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not rec.calls:
            time.sleep(0.05)
        assert len(rec.calls) == 1

    def test_pause_after_resume_stops_new_dispatches(self, env):
        rec = _RecordingRunner()
        env["kitchen"].register_runner("agent", rec)
        env["kitchen"].resume(env["conn_factory"])
        _seed_eligible(env, "B-1")
        env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 3})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not rec.calls:
            time.sleep(0.05)
        assert len(rec.calls) == 1

        # Pause again. New eligible ticket should NOT be picked up.
        env["kitchen"].pause(env["conn_factory"])
        _seed_eligible(env, "B-2")
        env["kitchen"].tick(env["conn_factory"], {"max_concurrent_runs": 3})
        time.sleep(0.2)
        assert len(rec.calls) == 1  # B-2 not dispatched


# ---------------------------------------------------------------------------
# Manual trigger_run bypasses the pause gate (the click IS the explicit OK).
# ---------------------------------------------------------------------------


class TestManualTriggerWhilePaused:
    def test_trigger_run_works_while_paused(self, env):
        rec = _RecordingRunner()
        env["kitchen"].register_runner("agent", rec)
        c = env["conn_factory"]()
        c.execute(
            "INSERT INTO tickets (id, project_id, title, section, status, description) "
            "VALUES ('B-1', 'p', 'T', 'Backlog', 'specified', 'd')"
        )
        c.commit()
        c.close()
        # Paused (default).
        assert env["kitchen"].is_paused() is True
        rid = env["kitchen"].trigger_run(env["conn_factory"], "p", "ticket", "B-1", {})
        assert rid is not None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not rec.calls:
            time.sleep(0.05)
        assert len(rec.calls) == 1


# ---------------------------------------------------------------------------
# Reconciliation runs even when paused (safety, not new work).
# ---------------------------------------------------------------------------


class TestReconcileRunsWhilePaused:
    def test_paused_tick_still_expires_stalls(self, env):
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        c = env["conn_factory"]()
        c.execute(
            "INSERT INTO tickets (id, project_id, title, section, status, description) "
            "VALUES ('B-x', 'p', 'X', 'WIP', 'in-progress', 'd')"
        )
        c.execute(
            "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, "
            " heartbeat_at, started_at, triggered_by) "
            "VALUES ('p', 'ticket', 'B-x', 'agent', 'running', ?, ?, 'human')",
            (old, old),
        )
        c.commit()
        c.close()
        # Paused — but reconcile should still flip the stalled run.
        assert env["kitchen"].is_paused() is True
        env["kitchen"].tick(env["conn_factory"], {})
        c = env["conn_factory"]()
        row = c.execute("SELECT status FROM runs WHERE subject_id='B-x'").fetchone()
        c.close()
        assert row["status"] == "stalled"


# ---------------------------------------------------------------------------
# pause/resume idempotency + audit emission
# ---------------------------------------------------------------------------


class TestIdempotencyAndAudit:
    def test_pause_when_already_paused_returns_false(self, env):
        assert env["kitchen"].is_paused() is True
        # Already paused → no-op.
        assert env["kitchen"].pause(env["conn_factory"]) is False

    def test_resume_when_already_running_returns_false(self, env):
        env["kitchen"].resume(env["conn_factory"])
        # Already running → no-op.
        assert env["kitchen"].resume(env["conn_factory"]) is False

    def test_pause_persists_in_settings(self, env):
        env["kitchen"].resume(env["conn_factory"])
        c = env["conn_factory"]()
        row = c.execute(
            "SELECT value FROM settings WHERE key = 'kitchen.paused'"
        ).fetchone()
        c.close()
        assert row is not None
        assert row[0] == "false"
        env["kitchen"].pause(env["conn_factory"])
        c = env["conn_factory"]()
        row = c.execute(
            "SELECT value FROM settings WHERE key = 'kitchen.paused'"
        ).fetchone()
        c.close()
        assert row[0] == "true"

    def test_pause_emits_kitchen_paused_event(self, env):
        env["kitchen"].resume(env["conn_factory"])
        env["kitchen"].pause(env["conn_factory"], reason="ratelimit hit")
        c = env["conn_factory"]()
        ev = c.execute(
            "SELECT actor_type, payload_json FROM activity_events "
            "WHERE event_kind = 'kitchen_paused' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        c.close()
        assert ev is not None
        assert ev["actor_type"] == "system"
        import json

        assert json.loads(ev["payload_json"])["reason"] == "ratelimit hit"

    def test_resume_emits_kitchen_resumed_event(self, env):
        env["kitchen"].resume(env["conn_factory"], reason="manually unpausing")
        c = env["conn_factory"]()
        ev = c.execute(
            "SELECT event_kind, payload_json FROM activity_events "
            "WHERE event_kind = 'kitchen_resumed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        c.close()
        assert ev is not None
        import json

        assert json.loads(ev["payload_json"])["reason"] == "manually unpausing"


# ---------------------------------------------------------------------------
# start() honors persisted state on subsequent restarts
# ---------------------------------------------------------------------------


class TestStartReadsPersistedState:
    def test_start_with_persisted_running_stays_running(self, env):
        # User opted in to running previously.
        c = env["conn_factory"]()
        c.execute(
            "INSERT INTO settings (key, value) VALUES ('kitchen.paused', 'false') "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
        )
        c.commit()
        c.close()
        # Reset module state and restart.
        env["kitchen"]._paused = True  # force back to default
        env["kitchen"].start(env["conn_factory"])
        try:
            assert env["kitchen"].is_paused() is False
        finally:
            env["kitchen"].stop()

    def test_start_with_no_persisted_state_defaults_paused(self, env):
        env["kitchen"]._paused = True
        env["kitchen"].start(env["conn_factory"])
        try:
            assert env["kitchen"].is_paused() is True
        finally:
            env["kitchen"].stop()
