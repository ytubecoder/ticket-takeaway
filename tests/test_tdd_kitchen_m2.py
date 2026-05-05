"""TDD tests for Kitchen M2 — cross-project aggregator and Kitchen view shape.

Hermetic tests that drive _aggregate_kitchen_state() with seeded data and
assert bucket placement priority + project-summary counts.
"""

import importlib
import importlib.util
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def serve_mod(tmp_path, monkeypatch):
    """Boot serve.py against a temp DB with two registered projects."""
    db_file = tmp_path / "tickets.db"
    monkeypatch.setenv("HOME", str(tmp_path))
    import constants
    monkeypatch.setattr(constants, "DB_PATH", db_file)
    monkeypatch.setattr(constants, "DASHBOARD_DIR", tmp_path / ".claude" / "ticket-takeaway")
    (tmp_path / ".claude" / "ticket-takeaway").mkdir(parents=True, exist_ok=True)

    import db
    importlib.reload(db)

    spec = importlib.util.spec_from_file_location("serve_under_test", "src/serve.py")
    serve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(serve)
    monkeypatch.setattr(serve, "get_db", lambda: db.get_db(str(db_file)))

    # Stub two projects in the in-memory cache.
    serve._PROJECTS_CACHE.clear()
    serve._PROJECTS_CACHE["alpha"] = {"id": "alpha", "name": "Alpha", "path": "/tmp/alpha"}
    serve._PROJECTS_CACHE["beta"]  = {"id": "beta",  "name": "Beta",  "path": "/tmp/beta"}

    # Initialise schema once.
    c = serve.get_db(); serve.init_db(c); c.close()
    return serve, db_file


def _seed_ticket(db_file, pid, tid, section="Backlog", status="specified", description="d", with_crit=True):
    c = sqlite3.connect(db_file)
    c.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, pid, f"T {tid}", section, status, description),
    )
    if with_crit:
        c.execute(
            "INSERT INTO acceptance_criteria (ticket_id, project_id, text) VALUES (?, ?, ?)",
            (tid, pid, "X"),
        )
    c.commit(); c.close()


def _set_mode(db_file, pid, tid, mode, pause_reason=None):
    from actions import set_automation_mode, ActorContext
    c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
    set_automation_mode(c, pid, "ticket", tid, mode, ActorContext.human(),
                        pause_reason=pause_reason)
    c.commit(); c.close()


def _seed_run(db_file, pid, tid, status, runner_kind="agent"):
    c = sqlite3.connect(db_file)
    c.execute(
        "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, triggered_by) "
        "VALUES (?, 'ticket', ?, ?, ?, 'human')",
        (pid, tid, runner_kind, status),
    )
    c.commit(); c.close()


def _set_no_test_required(db_file, pid, tid, note="docs only"):
    c = sqlite3.connect(db_file)
    c.execute(
        "UPDATE tickets SET no_test_required = 1, no_test_required_note = ? "
        "WHERE id = ? AND project_id = ?",
        (note, tid, pid),
    )
    c.commit(); c.close()


# ---------------------------------------------------------------------------
# Aggregator bucket assignment
# ---------------------------------------------------------------------------

class TestAggregatorBuckets:
    def test_empty_state_returns_empty_buckets(self, serve_mod):
        serve, _ = serve_mod
        state = serve._aggregate_kitchen_state()
        assert set(state["buckets"].keys()) == {
            "needs_me", "running", "ready_to_delegate", "paused", "failed"
        }
        for k in state["buckets"]:
            assert state["buckets"][k] == []
        # Both registered projects appear in summaries.
        names = {p["name"] for p in state["projects"]}
        assert names == {"Alpha", "Beta"}

    def test_paused_ticket_lands_in_paused_bucket(self, serve_mod):
        serve, db_file = serve_mod
        _seed_ticket(db_file, "alpha", "B-1")
        _set_mode(db_file, "alpha", "B-1", "paused", pause_reason="waiting on key")
        state = serve._aggregate_kitchen_state()
        assert len(state["buckets"]["paused"]) == 1
        item = state["buckets"]["paused"][0]
        assert item["ticket_id"] == "B-1"
        assert item["project_id"] == "alpha"
        assert item["pause_reason"] == "waiting on key"

    def test_eligible_auto_no_run_lands_in_ready(self, serve_mod):
        serve, db_file = serve_mod
        _seed_ticket(db_file, "alpha", "B-1")
        _set_no_test_required(db_file, "alpha", "B-1")
        _set_mode(db_file, "alpha", "B-1", "auto")
        state = serve._aggregate_kitchen_state()
        ready = state["buckets"]["ready_to_delegate"]
        assert len(ready) == 1
        assert ready[0]["ticket_id"] == "B-1"

    def test_running_takes_priority_over_mode(self, serve_mod):
        # Mode=auto + active run → Running bucket, not Ready To Delegate.
        serve, db_file = serve_mod
        _seed_ticket(db_file, "alpha", "B-1")
        _set_mode(db_file, "alpha", "B-1", "auto")
        _seed_run(db_file, "alpha", "B-1", "running")
        state = serve._aggregate_kitchen_state()
        assert len(state["buckets"]["running"]) == 1
        assert state["buckets"]["ready_to_delegate"] == []

    def test_needs_input_takes_priority_over_running(self, serve_mod):
        serve, db_file = serve_mod
        _seed_ticket(db_file, "alpha", "B-1")
        _set_mode(db_file, "alpha", "B-1", "auto")
        _seed_run(db_file, "alpha", "B-1", "needs_input")
        state = serve._aggregate_kitchen_state()
        assert len(state["buckets"]["needs_me"]) == 1
        assert state["buckets"]["running"] == []

    def test_failed_run_lands_in_failed_bucket(self, serve_mod):
        serve, db_file = serve_mod
        _seed_ticket(db_file, "beta", "B-2")
        _set_mode(db_file, "beta", "B-2", "auto")
        _seed_run(db_file, "beta", "B-2", "failed")
        state = serve._aggregate_kitchen_state()
        assert len(state["buckets"]["failed"]) == 1
        assert state["buckets"]["ready_to_delegate"] == []

    def test_subjects_appear_in_at_most_one_bucket(self, serve_mod):
        serve, db_file = serve_mod
        # Mix across both projects with different states.
        _seed_ticket(db_file, "alpha", "B-1")
        _set_no_test_required(db_file, "alpha", "B-1")
        _set_mode(db_file, "alpha", "B-1", "auto")  # ready

        _seed_ticket(db_file, "alpha", "B-2")
        _set_mode(db_file, "alpha", "B-2", "paused", pause_reason="x")

        _seed_ticket(db_file, "beta", "B-3")
        _set_mode(db_file, "beta", "B-3", "auto")
        _seed_run(db_file, "beta", "B-3", "running")  # running

        _seed_ticket(db_file, "beta", "B-4")
        _set_mode(db_file, "beta", "B-4", "auto")
        _seed_run(db_file, "beta", "B-4", "needs_input")  # needs_me

        state = serve._aggregate_kitchen_state()
        all_items = []
        for items in state["buckets"].values():
            all_items.extend(items)
        ticket_ids = [i["ticket_id"] for i in all_items]
        assert sorted(ticket_ids) == ["B-1", "B-2", "B-3", "B-4"]
        # Confirm bucket placement
        assert {i["ticket_id"] for i in state["buckets"]["ready_to_delegate"]} == {"B-1"}
        assert {i["ticket_id"] for i in state["buckets"]["paused"]}              == {"B-2"}
        assert {i["ticket_id"] for i in state["buckets"]["running"]}           == {"B-3"}
        assert {i["ticket_id"] for i in state["buckets"]["needs_me"]}          == {"B-4"}


# ---------------------------------------------------------------------------
# Project summaries
# ---------------------------------------------------------------------------

class TestWatchedProjects:
    """M2-03: projects with watched=false are excluded from Kitchen aggregation."""

    def test_default_includes_all_projects(self, serve_mod):
        serve, db_file = serve_mod
        _seed_ticket(db_file, "alpha", "B-1")
        _set_mode(db_file, "alpha", "B-1", "paused", pause_reason="x")
        _seed_ticket(db_file, "beta", "B-2")
        _set_mode(db_file, "beta", "B-2", "paused", pause_reason="y")
        state = serve._aggregate_kitchen_state()
        assert {i["project_id"] for i in state["buckets"]["paused"]} == {"alpha", "beta"}

    def test_unwatched_project_dropped_from_buckets_and_summaries(self, serve_mod):
        serve, db_file = serve_mod
        _seed_ticket(db_file, "alpha", "B-1")
        _set_mode(db_file, "alpha", "B-1", "paused", pause_reason="x")
        _seed_ticket(db_file, "beta",  "B-2")
        _set_mode(db_file, "beta",  "B-2", "paused", pause_reason="y")
        # Mark beta unwatched in the cache (simulates the registry flag).
        serve._PROJECTS_CACHE["beta"]["watched"] = False
        state = serve._aggregate_kitchen_state()
        assert {i["project_id"] for i in state["buckets"]["paused"]} == {"alpha"}
        assert {p["id"] for p in state["projects"]} == {"alpha"}


class TestProjectSummaries:
    def test_counts_wip_and_review(self, serve_mod):
        serve, db_file = serve_mod
        _seed_ticket(db_file, "alpha", "W-1", section="WIP", status="in-progress")
        _seed_ticket(db_file, "alpha", "W-2", section="WIP", status="blocked")
        _seed_ticket(db_file, "alpha", "R-1", section="For Review", status="for-review")
        _seed_ticket(db_file, "beta",  "B-9", section="WIP", status="in-progress")
        state = serve._aggregate_kitchen_state()
        by_id = {p["id"]: p for p in state["projects"]}
        assert by_id["alpha"]["counts"]["wip"] == 2
        assert by_id["alpha"]["counts"]["review"] == 1
        assert by_id["alpha"]["counts"]["blocked"] == 1
        assert by_id["beta"]["counts"]["wip"] == 1

    def test_running_and_needs_me_counts(self, serve_mod):
        serve, db_file = serve_mod
        _seed_ticket(db_file, "alpha", "B-1")
        _set_mode(db_file, "alpha", "B-1", "auto")
        _seed_run(db_file, "alpha", "B-1", "running")
        _seed_ticket(db_file, "alpha", "B-2")
        _seed_run(db_file, "alpha", "B-2", "needs_input")
        state = serve._aggregate_kitchen_state()
        by_id = {p["id"]: p for p in state["projects"]}
        assert by_id["alpha"]["counts"]["running"] == 1
        assert by_id["alpha"]["counts"]["needs_me"] == 1
