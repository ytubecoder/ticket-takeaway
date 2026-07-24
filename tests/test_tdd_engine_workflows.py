"""TDD tests for kitchen.py Phase 2 — DB-workflow dispatch path.

Verifies:
  - kitchen.use_db_workflows=true routes to _dispatch_via_workflows
  - kitchen.use_db_workflows=false (default) routes to legacy path
  - A queued run gets correct metadata_json.workflow_id when dispatched via workflows
  - get_use_db_workflows() reads the setting correctly

Pure logic: no server, no subprocess.  Runner is stubbed to record dispatch calls.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import os
import threading
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import init_db
from actions import ActorContext, set_automation_mode
from workflows_seed import seed_default_workflows
import kitchen


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_kitchen_state():
    """Reset kitchen module-level state between tests."""
    kitchen._started = False
    kitchen._stop_event = None
    kitchen._loop_thread = None
    kitchen._active_runs.clear()
    kitchen._paused = True
    yield
    # Clean up if start() was called.
    if kitchen._started:
        kitchen.stop()
    kitchen._started = False
    kitchen._stop_event = None
    kitchen._loop_thread = None
    kitchen._active_runs.clear()
    kitchen._paused = True


@pytest.fixture
def db_factory(tmp_path):
    """Returns a factory producing connections to a shared temp DB."""
    db_file = tmp_path / "test.db"

    def _factory():
        c = sqlite3.connect(str(db_file))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        init_db(c)
        return c

    # Run init once to create schema.
    c = _factory()
    c.close()
    return _factory


def _add_ticket(db_factory, tid="B-1", section="Backlog", status="specified",
                description="A real description.", project_id="p"):
    conn = db_factory()
    conn.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, project_id, f"Title {tid}", section, status, description),
    )
    conn.execute(
        "INSERT INTO acceptance_criteria (ticket_id, project_id, text) VALUES (?, ?, ?)",
        (tid, project_id, "Does the thing"),
    )
    conn.commit()
    conn.close()


def _set_auto(db_factory, tid="B-1", project_id="p"):
    conn = db_factory()
    set_automation_mode(conn, project_id, "ticket", tid, "auto", ActorContext.human())
    conn.commit()
    conn.close()


def _freeze_summary_hash(db_factory, tid="B-1", project_id="p"):
    """Pre-populate tickets.summary_hash to current content hash so the
    Refresh-summary workflow's summary_stale predicate is False on this
    ticket. Use in tests that want to assert "no workflow is eligible".
    """
    from actions import compute_summary_hash
    conn = db_factory()
    h = compute_summary_hash(conn, project_id, tid)
    conn.execute(
        "UPDATE tickets SET summary_hash = ? WHERE id = ? AND project_id = ?",
        (h, tid, project_id),
    )
    conn.commit()
    conn.close()


def _set_tests_flag(db_factory, tid="B-1", project_id="p"):
    """Legacy helper kept as a no-op.

    Migration 15 collapsed the tests/smoke readiness flags into acceptance
    criteria. Existing tests still call this for narrative clarity; we keep
    the function as a no-op so the test surface stays stable.
    """
    return None


def _declare_lane(db_factory, tid="B-1", project_id="p"):
    """Entry gate into automation: eligibility + the Backlog → WIP trigger
    require a declared spec lane (spec_linked)."""
    conn = db_factory()
    conn.execute(
        "INSERT OR REPLACE INTO readiness_flags (ticket_id, project_id, flag, content, set_by) "
        "VALUES (?, ?, 'spec', ?, 'test')",
        (tid, project_id, f"B:{tid.lower()}-test-change"),
    )
    conn.commit()
    conn.close()


def _set_flag(db_factory, key: str, value: str):
    conn = db_factory()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()
    conn.close()


def _seed_workflows(db_factory, project_id="p"):
    conn = db_factory()
    seed_default_workflows(conn, project_id)
    conn.close()


def _get_queued_runs(db_factory):
    conn = db_factory()
    rows = conn.execute(
        "SELECT id, subject_id, metadata_json FROM runs WHERE status = 'queued'"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# get_use_db_workflows helper
# ---------------------------------------------------------------------------

class TestGetUseDbWorkflows:
    def test_default_is_false(self, db_factory):
        conn = db_factory()
        result = kitchen.get_use_db_workflows(conn)
        conn.close()
        assert result is False

    def test_false_string_returns_false(self, db_factory):
        _set_flag(db_factory, "kitchen.use_db_workflows", "false")
        conn = db_factory()
        result = kitchen.get_use_db_workflows(conn)
        conn.close()
        assert result is False

    def test_true_string_returns_true(self, db_factory):
        _set_flag(db_factory, "kitchen.use_db_workflows", "true")
        conn = db_factory()
        result = kitchen.get_use_db_workflows(conn)
        conn.close()
        assert result is True

    def test_TRUE_uppercase_returns_true(self, db_factory):
        _set_flag(db_factory, "kitchen.use_db_workflows", "TRUE")
        conn = db_factory()
        result = kitchen.get_use_db_workflows(conn)
        conn.close()
        assert result is True

    def test_any_other_value_returns_false(self, db_factory):
        _set_flag(db_factory, "kitchen.use_db_workflows", "yes")
        conn = db_factory()
        result = kitchen.get_use_db_workflows(conn)
        conn.close()
        assert result is False


# ---------------------------------------------------------------------------
# Stub runner — records dispatch calls without subprocesses
# ---------------------------------------------------------------------------

class _StubRunner:
    """Records execute() calls. Immediately marks runs as succeeded."""
    runner_kind = "agent"

    def __init__(self):
        self.calls: list[dict] = []

    def execute(self, run_id, project_id, subject_type, subject_id,
                workspace, config, conn_factory, cancel_event=None):
        from runners import RunOutcome
        self.calls.append({
            "run_id": run_id,
            "project_id": project_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "config": config,
        })
        # Mark the run succeeded immediately so the DB is left in a clean state.
        from actions import utcnow_iso
        conn = conn_factory()
        conn.execute(
            "UPDATE runs SET status='succeeded', finished_at=?, heartbeat_at=? WHERE id=?",
            (utcnow_iso(), utcnow_iso(), run_id),
        )
        conn.commit()
        conn.close()
        return RunOutcome(
            run_id=run_id, final_status="succeeded",
            duration_ms=0, summary="stub",
        )


# ---------------------------------------------------------------------------
# DB-workflow dispatch path
# ---------------------------------------------------------------------------

class TestDispatchViaWorkflows:
    """With kitchen.use_db_workflows=true, a queued run with workflow metadata appears."""

    def _setup_eligible(self, db_factory, project_id="p"):
        """Create a ticket that matches the 'Backlog → WIP' trigger."""
        _add_ticket(db_factory, tid="B-1", section="Backlog", project_id=project_id)
        _set_auto(db_factory, tid="B-1", project_id=project_id)
        _set_tests_flag(db_factory, tid="B-1", project_id=project_id)
        _declare_lane(db_factory, tid="B-1", project_id=project_id)
        _seed_workflows(db_factory, project_id=project_id)
        _set_flag(db_factory, "kitchen.use_db_workflows", "true")

    def test_workflow_dispatch_queues_run(self, db_factory, tmp_path):
        self._setup_eligible(db_factory)

        # Stub the project path resolver so workspace setup doesn't fail.
        (tmp_path / "p").mkdir(parents=True, exist_ok=True)
        kitchen.set_project_path_resolver(lambda pid: tmp_path / pid)

        # Stub the runner to avoid subprocess.
        stub = _StubRunner()
        kitchen.register_runner("agent", stub)

        settings = {"max_concurrent_runs": 3, "max_concurrent_per_project": 1}
        kitchen._paused = False
        kitchen._dispatch_eligible(db_factory, settings)

        # Give the thread a moment to start and record.
        import time; time.sleep(0.2)

        runs = _get_queued_runs(db_factory)
        # Run may already be succeeded (stub transitions immediately); check all.
        conn = db_factory()
        all_runs = conn.execute("SELECT id, subject_id, metadata_json, status FROM runs").fetchall()
        conn.close()
        assert len(all_runs) == 1, f"Expected 1 run, got {len(all_runs)}"
        run = all_runs[0]
        assert run["subject_id"] == "B-1"
        meta = json.loads(run["metadata_json"])
        assert "workflow_id" in meta
        assert "Backlog" in meta["workflow_id"] or "backlog" in meta["workflow_id"].lower()
        assert meta["workflow_name"] == "Backlog → WIP"
        assert meta["step_index"] == 0

    def test_workflow_dispatch_metadata_has_on_success(self, db_factory, tmp_path):
        self._setup_eligible(db_factory)
        (tmp_path / "p").mkdir(parents=True, exist_ok=True)
        kitchen.set_project_path_resolver(lambda pid: tmp_path / pid)
        stub = _StubRunner()
        kitchen.register_runner("agent", stub)

        settings = {"max_concurrent_runs": 3, "max_concurrent_per_project": 1}
        kitchen._paused = False
        kitchen._dispatch_eligible(db_factory, settings)

        import time; time.sleep(0.2)

        conn = db_factory()
        run = conn.execute("SELECT metadata_json FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert run is not None
        meta = json.loads(run["metadata_json"])
        assert "on_success" in meta
        on_success = meta["on_success"]
        # Backlog → WIP: move_to WIP, set_status in-progress
        assert on_success.get("move_to") == "WIP"

    def test_no_run_queued_when_no_eligible_ticket(self, db_factory, tmp_path):
        """If the ticket is in Done (matches no enabled workflow trigger), no run fires.

        Pre-populates summary_hash to the current content hash so the
        Refresh-summary workflow's summary_stale predicate returns False —
        otherwise the test ticket's empty hash would trigger a summary run
        even in Done.
        """
        _add_ticket(db_factory, tid="B-1", section="Done", status="done")
        _set_auto(db_factory, tid="B-1")
        _set_tests_flag(db_factory, tid="B-1")
        _declare_lane(db_factory, tid="B-1")
        _freeze_summary_hash(db_factory, tid="B-1")
        _seed_workflows(db_factory)
        _set_flag(db_factory, "kitchen.use_db_workflows", "true")

        (tmp_path / "p").mkdir(parents=True, exist_ok=True)
        kitchen.set_project_path_resolver(lambda pid: tmp_path / pid)
        stub = _StubRunner()
        kitchen.register_runner("agent", stub)

        settings = {"max_concurrent_runs": 3, "max_concurrent_per_project": 1}
        kitchen._paused = False
        kitchen._dispatch_eligible(db_factory, settings)

        import time; time.sleep(0.1)

        conn = db_factory()
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert count == 0

    def test_ideas_ticket_dispatched_by_spec_workflow(self, db_factory, tmp_path):
        """Ideas ticket with description+criteria gets dispatched by 'Spec → Backlog'."""
        _add_ticket(db_factory, tid="B-1", section="Ideas", status="proposed")
        _set_auto(db_factory, tid="B-1")
        # Spec → Backlog trigger checks has_field:description and criteria_count >= 1.
        # Both are satisfied by _add_ticket (which adds description + criteria).
        # Freeze summary_hash so the Refresh-summary workflow doesn't outrace
        # Spec → Backlog (lexicographic id order picks the summary one first
        # otherwise, since both match this Ideas ticket).
        _freeze_summary_hash(db_factory, tid="B-1")
        _seed_workflows(db_factory)
        _set_flag(db_factory, "kitchen.use_db_workflows", "true")

        (tmp_path / "p").mkdir(parents=True, exist_ok=True)
        kitchen.set_project_path_resolver(lambda pid: tmp_path / pid)
        stub = _StubRunner()
        kitchen.register_runner("agent", stub)

        settings = {"max_concurrent_runs": 3, "max_concurrent_per_project": 1}
        kitchen._paused = False
        kitchen._dispatch_eligible(db_factory, settings)

        import time; time.sleep(0.2)

        conn = db_factory()
        all_runs = conn.execute("SELECT metadata_json FROM runs").fetchall()
        conn.close()
        assert len(all_runs) == 1
        meta = json.loads(all_runs[0]["metadata_json"])
        # Should be dispatched by 'Spec → Backlog', not 'Backlog → WIP'.
        assert meta["workflow_name"] == "Spec → Backlog"

    def test_no_run_queued_when_manual_mode(self, db_factory, tmp_path):
        """automation_mode=manual means ticket won't appear in subjects query."""
        _add_ticket(db_factory, tid="B-1", section="Backlog")
        # Don't call _set_auto — stays manual.
        _set_tests_flag(db_factory, tid="B-1")
        _declare_lane(db_factory, tid="B-1")
        _seed_workflows(db_factory)
        _set_flag(db_factory, "kitchen.use_db_workflows", "true")

        (tmp_path / "p").mkdir(parents=True, exist_ok=True)
        kitchen.set_project_path_resolver(lambda pid: tmp_path / pid)
        stub = _StubRunner()
        kitchen.register_runner("agent", stub)

        settings = {"max_concurrent_runs": 3, "max_concurrent_per_project": 1}
        kitchen._paused = False
        kitchen._dispatch_eligible(db_factory, settings)

        import time; time.sleep(0.1)

        conn = db_factory()
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# Legacy path with flag=false
# ---------------------------------------------------------------------------

class TestLegacyPathWithFlagFalse:
    """With kitchen.use_db_workflows=false (default), legacy path still runs."""

    def test_legacy_path_dispatches_eligible_ticket(self, db_factory, tmp_path):
        """Legacy path dispatches an eligible ticket even when no workflows are seeded."""
        _add_ticket(db_factory, tid="B-1", section="Backlog")
        _set_auto(db_factory, tid="B-1")
        _set_tests_flag(db_factory, tid="B-1")
        _declare_lane(db_factory, tid="B-1")
        # flag stays false (default)
        # No workflows seeded — legacy doesn't need them.

        (tmp_path / "p").mkdir(parents=True, exist_ok=True)
        kitchen.set_project_path_resolver(lambda pid: tmp_path / pid)
        stub = _StubRunner()
        kitchen.register_runner("agent", stub)

        settings = {"max_concurrent_runs": 3, "max_concurrent_per_project": 1}
        kitchen._paused = False
        kitchen._dispatch_eligible(db_factory, settings)

        import time; time.sleep(0.2)

        conn = db_factory()
        all_runs = conn.execute("SELECT id, subject_id, metadata_json, status FROM runs").fetchall()
        conn.close()
        assert len(all_runs) == 1
        assert all_runs[0]["subject_id"] == "B-1"
        # Legacy path: metadata_json should be empty/default {}.
        meta = json.loads(all_runs[0]["metadata_json"])
        assert "workflow_id" not in meta

    def test_explicit_false_uses_legacy(self, db_factory, tmp_path):
        _set_flag(db_factory, "kitchen.use_db_workflows", "false")
        _add_ticket(db_factory, tid="B-1", section="Backlog")
        _set_auto(db_factory, tid="B-1")
        _set_tests_flag(db_factory, tid="B-1")
        _declare_lane(db_factory, tid="B-1")

        (tmp_path / "p").mkdir(parents=True, exist_ok=True)
        kitchen.set_project_path_resolver(lambda pid: tmp_path / pid)
        stub = _StubRunner()
        kitchen.register_runner("agent", stub)

        settings = {"max_concurrent_runs": 3, "max_concurrent_per_project": 1}
        kitchen._paused = False
        kitchen._dispatch_eligible(db_factory, settings)

        import time; time.sleep(0.2)

        conn = db_factory()
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert count == 1

    def test_ineligible_ticket_not_dispatched_legacy(self, db_factory, tmp_path):
        """Legacy path respects manual mode — no run for manual ticket."""
        _add_ticket(db_factory, tid="B-1", section="Backlog")
        # Don't set auto — stays manual.

        (tmp_path / "p").mkdir(parents=True, exist_ok=True)
        kitchen.set_project_path_resolver(lambda pid: tmp_path / pid)
        stub = _StubRunner()
        kitchen.register_runner("agent", stub)

        settings = {"max_concurrent_runs": 3, "max_concurrent_per_project": 1}
        kitchen._paused = False
        kitchen._dispatch_eligible(db_factory, settings)

        import time; time.sleep(0.1)

        conn = db_factory()
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# Tick routing
# ---------------------------------------------------------------------------

class TestTickRouting:
    """tick() routes to the correct dispatch path based on flag."""

    def test_tick_calls_legacy_when_paused_noop(self, db_factory):
        """When paused, tick() runs reconcile but not dispatch — no runs created."""
        kitchen._paused = True
        kitchen.tick(db_factory, {})
        conn = db_factory()
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert count == 0

    def test_tick_uses_db_workflows_when_flag_true(self, db_factory, tmp_path, monkeypatch):
        """When flag=true, tick dispatches via workflow path (no legacy path called)."""
        _add_ticket(db_factory, tid="B-1", section="Backlog")
        _set_auto(db_factory, tid="B-1")
        _set_tests_flag(db_factory, tid="B-1")
        _declare_lane(db_factory, tid="B-1")
        _seed_workflows(db_factory)
        _set_flag(db_factory, "kitchen.use_db_workflows", "true")

        (tmp_path / "p").mkdir(parents=True, exist_ok=True)
        kitchen.set_project_path_resolver(lambda pid: tmp_path / pid)
        stub = _StubRunner()
        kitchen.register_runner("agent", stub)

        kitchen._paused = False
        kitchen.tick(db_factory, {"max_concurrent_runs": 3, "max_concurrent_per_project": 1})

        import time; time.sleep(0.2)

        conn = db_factory()
        all_runs = conn.execute("SELECT metadata_json FROM runs").fetchall()
        conn.close()
        assert len(all_runs) == 1
        meta = json.loads(all_runs[0]["metadata_json"])
        assert "workflow_id" in meta
