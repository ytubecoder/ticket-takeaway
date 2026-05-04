"""TDD tests for Kitchen M4 — journey cascade hook.

When a ticket linked to one or more journeys reaches the Done section,
actions._cascade_to_linked_journeys queues a scenario run for each linked
journey via kitchen.trigger_run with triggered_by='journey-cascade'.

We monkey-patch kitchen.trigger_run to record the calls — we don't actually
spin up a ScenarioRunner / Playwright in these tests.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Fresh in-memory DB with the full schema."""
    import constants
    db_file = tmp_path / "tickets.db"
    monkeypatch.setattr(constants, "DB_PATH", db_file)
    monkeypatch.setattr(constants, "DASHBOARD_DIR", tmp_path / ".claude" / "ticket-takeaway")
    (tmp_path / ".claude" / "ticket-takeaway").mkdir(parents=True, exist_ok=True)
    import db
    import importlib
    importlib.reload(db)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    db.init_db(c)
    return c


@pytest.fixture
def cascade_recorder(monkeypatch):
    """Replace kitchen.trigger_run so we can assert what would have been queued."""
    import kitchen
    calls: list[tuple] = []
    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return 999  # pretend run id
    monkeypatch.setattr(kitchen, "trigger_run", fake)
    return calls


def _seed_ticket(conn, tid="B-1", section="WIP", status="in-progress", project_id="p"):
    conn.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description) "
        "VALUES (?, ?, 'T', ?, ?, 'd')",
        (tid, project_id, section, status),
    )


def _seed_journey(conn, jid="J-1", project_id="p"):
    conn.execute(
        "INSERT INTO journeys (id, project_id, title, description) "
        "VALUES (?, ?, 'Test journey', '')",
        (jid, project_id),
    )


def _link(conn, jid, tid, project_id="p"):
    conn.execute(
        "INSERT INTO journey_tickets (journey_id, project_id, ticket_id) "
        "VALUES (?, ?, ?)",
        (jid, project_id, tid),
    )


# ---------------------------------------------------------------------------
# Cascade behaviour
# ---------------------------------------------------------------------------

class TestCascade:
    def test_done_with_no_linked_journey_does_not_cascade(self, conn, cascade_recorder):
        from actions import move_ticket, ActorContext
        _seed_ticket(conn); conn.commit()
        move_ticket(conn, "p", "B-1", "Done", actor=ActorContext.human())
        conn.commit()
        assert cascade_recorder == []

    def test_done_with_linked_journey_queues_one_cascade_run(self, conn, cascade_recorder):
        from actions import move_ticket, ActorContext
        _seed_ticket(conn)
        _seed_journey(conn, "J-onboarding")
        _link(conn, "J-onboarding", "B-1")
        conn.commit()
        move_ticket(conn, "p", "B-1", "Done", actor=ActorContext.human())
        conn.commit()
        assert len(cascade_recorder) == 1
        # trigger_run signature: (get_db, project_id, subject_type, subject_id, settings, ...)
        args, kwargs = cascade_recorder[0]
        # project_id, subject_type, subject_id positional after get_db
        assert args[1] == "p"
        assert args[2] == "journey"
        assert args[3] == "J-onboarding"
        assert kwargs.get("triggered_by") == "journey-cascade"

    def test_multiple_linked_journeys_each_get_their_own_cascade(self, conn, cascade_recorder):
        from actions import move_ticket, ActorContext
        _seed_ticket(conn)
        _seed_journey(conn, "J-1"); _seed_journey(conn, "J-2")
        _link(conn, "J-1", "B-1"); _link(conn, "J-2", "B-1")
        conn.commit()
        move_ticket(conn, "p", "B-1", "Done", actor=ActorContext.human())
        conn.commit()
        assert len(cascade_recorder) == 2
        ids = sorted(args[3] for args, _ in cascade_recorder)
        assert ids == ["J-1", "J-2"]

    def test_already_in_done_does_not_re_cascade(self, conn, cascade_recorder):
        # Defensive: section_change cascade only fires when transitioning INTO
        # Done, not for status updates while already in Done.
        from actions import move_ticket, update_ticket, ActorContext
        _seed_ticket(conn, section="For Review", status="for-review")
        _seed_journey(conn, "J-1"); _link(conn, "J-1", "B-1")
        conn.commit()
        move_ticket(conn, "p", "B-1", "Done", actor=ActorContext.human())
        conn.commit()
        assert len(cascade_recorder) == 1
        # A no-op move_ticket back to Done shouldn't double-fire.
        move_ticket(conn, "p", "B-1", "Done", actor=ActorContext.human())
        conn.commit()
        assert len(cascade_recorder) == 1

    def test_cascade_fires_for_accept_ticket_too(self, conn, cascade_recorder):
        # accept_ticket flips section to Done, which is the same code path.
        from actions import accept_ticket, ActorContext
        _seed_ticket(conn, section="For Review", status="for-review")
        _seed_journey(conn, "J-1"); _link(conn, "J-1", "B-1")
        conn.commit()
        accept_ticket(conn, "p", "B-1", "/tmp", "p", actor=ActorContext.human())
        conn.commit()
        assert len(cascade_recorder) == 1
        args, kwargs = cascade_recorder[0]
        assert args[3] == "J-1"
        assert kwargs.get("triggered_by") == "journey-cascade"

    def test_cascade_does_not_break_when_kitchen_trigger_throws(self, conn, monkeypatch):
        # Fault injection: trigger_run blowing up must not break the section move.
        import kitchen
        def boom(*args, **kwargs):
            raise RuntimeError("simulated kitchen failure")
        monkeypatch.setattr(kitchen, "trigger_run", boom)
        from actions import move_ticket, ActorContext
        _seed_ticket(conn)
        _seed_journey(conn, "J-1"); _link(conn, "J-1", "B-1")
        conn.commit()
        # Should NOT raise — cascade swallows kitchen failures.
        move_ticket(conn, "p", "B-1", "Done", actor=ActorContext.human())
        conn.commit()
        # Ticket reached Done despite the cascade exploding.
        row = conn.execute("SELECT section FROM tickets WHERE id='B-1'").fetchone()
        assert row["section"] == "Done"

    def test_cascade_with_no_journey_tickets_table_is_safe(self, monkeypatch, tmp_path):
        # Pre-migration DB safety: actions.py shouldn't crash if journey_tickets
        # is missing. Build a connection with ONLY the tickets table.
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE tickets (id TEXT, project_id TEXT, section TEXT, "
                  "status TEXT, title TEXT, description TEXT, "
                  "draft INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, "
                  "no_test_required INTEGER DEFAULT 0, no_test_required_note TEXT DEFAULT '', "
                  "parent TEXT, sort_order INTEGER DEFAULT 0, "
                  "created_at TEXT, updated_at TEXT, summary TEXT DEFAULT '', "
                  "priority TEXT DEFAULT 'medium', "
                  "commit_hash TEXT DEFAULT '', release_tag TEXT DEFAULT '', "
                  "PRIMARY KEY (id, project_id))")
        c.commit()
        # Import directly and call the cascade helper.
        from actions import _cascade_to_linked_journeys
        # Should not raise OperationalError.
        _cascade_to_linked_journeys(c, "p", "B-1")
