"""TDD for the cross-project activity feed (Follow the Action mode)."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import actions
import constants
from db import init_db


class TestTaxonomy:
    # Every event kind emit_event() is actually called with today (grep'd from
    # actions.py / kitchen.py / runners.py / serve.py / tickets-cli.py / seek.py)
    EMITTED_KINDS = {
        "ticket_created", "section_change", "status_change", "criteria_check",
        "field_changed", "gate_override", "mode_changed", "pause_set",
        "pause_cleared", "kitchen_paused", "kitchen_resumed", "run_started",
        "run_stalled", "run_failed", "run_succeeded", "run_cancelled",
        "run_discarded", "workspace_created", "handoff_recorded",
        "agent_output", "hook_started", "hook_succeeded", "hook_failed",
        "input_provided", "pane_linked", "pane_unlinked",
    }

    def test_every_emitted_kind_has_a_group(self):
        missing = self.EMITTED_KINDS - set(constants.EVENT_KIND_GROUPS)
        assert not missing, f"kinds without a group: {sorted(missing)}"

    def test_every_group_has_a_color(self):
        for kind, group in constants.EVENT_KIND_GROUPS.items():
            assert group in constants.EVENT_GROUP_COLORS, (kind, group)

    def test_precedence_is_known_kinds_and_starts_with_moves(self):
        assert constants.FOLLOW_KIND_PRECEDENCE[0] == "section_change"
        assert constants.FOLLOW_KIND_PRECEDENCE[1] == "ticket_created"
        for k in constants.FOLLOW_KIND_PRECEDENCE:
            assert k in constants.EVENT_KIND_GROUPS, k


PROJECTS = [
    {"id": "p1", "name": "Project One", "watched": True},
    {"id": "p2", "name": "Project Two", "watched": False},
]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    c.execute(
        "INSERT INTO tickets (id, project_id, title, section, status) "
        "VALUES ('B-01', 'p1', 'First ticket', 'WIP', 'in-progress')"
    )
    c.commit()
    return c


def _emit(c, pid, kind, subject="B-01", payload=None, actor=None, subject_type="ticket"):
    eid = actions.emit_event(
        c, pid, subject_type, subject, kind, payload or {},
        actor or actions.ActorContext.human(),
    )
    c.commit()
    return eid


class TestActivityFeed:
    def test_no_since_returns_latest_only(self, conn):
        _emit(conn, "p1", "status_change", payload={"before": "a", "after": "b"})
        feed = actions.get_activity_feed(conn, projects=PROJECTS)
        assert feed["events"] == []
        assert feed["latest_id"] >= 1

    def test_empty_db_latest_zero(self, conn):
        feed = actions.get_activity_feed(conn, projects=PROJECTS)
        assert feed == {"latest_id": 0, "events": []}

    def test_since_ordering_and_limit(self, conn):
        ids = [_emit(conn, "p1", "field_changed") for _ in range(5)]
        feed = actions.get_activity_feed(conn, since_id=ids[1], limit=2, projects=PROJECTS)
        got = [e["id"] for e in feed["events"]]
        assert got == [ids[2], ids[3]]  # ascending, oldest first, limited

    def test_discarded_events_excluded(self, conn):
        keep = _emit(conn, "p1", "status_change")
        drop = _emit(conn, "p1", "status_change")
        conn.execute("UPDATE activity_events SET discarded_run_id = 99 WHERE id = ?", (drop,))
        conn.commit()
        feed = actions.get_activity_feed(conn, since_id=0, projects=PROJECTS)
        assert [e["id"] for e in feed["events"]] == [keep]

    def test_unwatched_project_filtered_but_latest_global(self, conn):
        _emit(conn, "p2", "status_change")   # unwatched
        feed = actions.get_activity_feed(conn, since_id=0, projects=PROJECTS)
        assert feed["events"] == []
        assert feed["latest_id"] >= 1        # latest_id is global, unfiltered

    def test_kitchen_sentinel_included_named(self, conn):
        _emit(conn, "_kitchen", "kitchen_paused", subject="lifecycle",
              subject_type="investigation", actor=actions.ActorContext.system())
        feed = actions.get_activity_feed(conn, since_id=0, projects=PROJECTS)
        assert len(feed["events"]) == 1
        assert feed["events"][0]["project_name"] == "Kitchen"

    def test_ticket_enrichment_and_deleted_null(self, conn):
        _emit(conn, "p1", "status_change")                       # exists
        _emit(conn, "p1", "status_change", subject="B-99")       # no such ticket
        feed = actions.get_activity_feed(conn, since_id=0, projects=PROJECTS)
        by_subject = {e["subject_id"]: e for e in feed["events"]}
        assert by_subject["B-01"]["ticket_title"] == "First ticket"
        assert by_subject["B-01"]["section"] == "WIP"
        assert by_subject["B-99"]["ticket_title"] is None
        assert by_subject["B-99"]["section"] is None

    def test_agent_actor_name_resolved(self, conn):
        conn.execute(
            "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, "
            "status, triggered_by, metadata_json) "
            "VALUES ('p1', 'ticket', 'B-01', 'agent', 'succeeded', 'human', "
            "'{\"workflow_name\": \"bounce-workflow\"}')"
        )
        run_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        conn.commit()
        _emit(conn, "p1", "section_change",
              payload={"before": "Backlog", "after": "WIP"},
              actor=actions.ActorContext.agent(str(run_id)))
        feed = actions.get_activity_feed(conn, since_id=0, projects=PROJECTS)
        assert feed["events"][0]["actor_name"] == "bounce-workflow"

    def test_projects_none_yields_only_kitchen(self, conn):
        _emit(conn, "p1", "status_change")
        feed = actions.get_activity_feed(conn, since_id=0, projects=None)
        assert feed["events"] == []
