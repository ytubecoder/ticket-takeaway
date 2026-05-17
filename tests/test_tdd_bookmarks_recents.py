"""TDD tests for bookmarks + recents (I-43).

Covers the actions.py contract: toggle/list bookmarks, touch/list recents,
and the 20-row cap on recents per project.
"""

import sqlite3
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import init_db
import actions


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    # Seed a project + a handful of tickets we can bookmark/touch.
    for i in range(1, 26):
        c.execute(
            "INSERT INTO tickets (id, project_id, title, section, status) "
            "VALUES (?, 'p1', ?, 'Backlog', 'proposed')",
            (f"B-{i:02d}", f"Ticket {i}"),
        )
    c.commit()
    return c


class TestSchema:
    def test_bookmarks_table_exists(self, conn):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ticket_bookmarks)").fetchall()]
        assert {"project_id", "ticket_id", "created_at"}.issubset(cols)

    def test_recents_table_exists(self, conn):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ticket_recents)").fetchall()]
        assert {"project_id", "ticket_id", "last_seen_at"}.issubset(cols)


class TestBookmarks:
    def test_toggle_on_then_off(self, conn):
        assert actions.toggle_bookmark(conn, "p1", "B-01") is True
        assert actions.is_bookmarked(conn, "p1", "B-01") is True
        assert actions.toggle_bookmark(conn, "p1", "B-01") is False
        assert actions.is_bookmarked(conn, "p1", "B-01") is False

    def test_unknown_ticket_raises(self, conn):
        with pytest.raises(actions.TicketNotFoundError):
            actions.toggle_bookmark(conn, "p1", "NOPE-99")

    def test_list_orders_newest_first(self, conn):
        actions.toggle_bookmark(conn, "p1", "B-01")
        actions.toggle_bookmark(conn, "p1", "B-02")
        actions.toggle_bookmark(conn, "p1", "B-03")
        items = actions.list_bookmarks(conn, "p1")
        ids = [i["id"] for i in items]
        assert ids == ["B-03", "B-02", "B-01"]

    def test_list_is_project_scoped(self, conn):
        # Add a ticket in a second project + bookmark it
        conn.execute(
            "INSERT INTO tickets (id, project_id, title, section, status) "
            "VALUES ('B-99', 'p2', 'Other proj', 'Backlog', 'proposed')"
        )
        conn.commit()
        actions.toggle_bookmark(conn, "p1", "B-01")
        actions.toggle_bookmark(conn, "p2", "B-99")
        p1 = [i["id"] for i in actions.list_bookmarks(conn, "p1")]
        p2 = [i["id"] for i in actions.list_bookmarks(conn, "p2")]
        assert p1 == ["B-01"]
        assert p2 == ["B-99"]

    def test_deleted_ticket_drops_from_list(self, conn):
        actions.toggle_bookmark(conn, "p1", "B-01")
        actions.toggle_bookmark(conn, "p1", "B-02")
        conn.execute("DELETE FROM tickets WHERE id = 'B-01'")
        conn.commit()
        ids = [i["id"] for i in actions.list_bookmarks(conn, "p1")]
        assert ids == ["B-02"]


class TestRecents:
    def test_touch_creates_row(self, conn):
        actions.touch_recent(conn, "p1", "B-01")
        items = actions.list_recents(conn, "p1")
        assert [i["id"] for i in items] == ["B-01"]

    def test_touch_updates_existing(self, conn):
        actions.touch_recent(conn, "p1", "B-01")
        actions.touch_recent(conn, "p1", "B-02")
        # Re-touch B-01 to bring it back to the top
        actions.touch_recent(conn, "p1", "B-01")
        ids = [i["id"] for i in actions.list_recents(conn, "p1")]
        assert ids[0] == "B-01"
        assert "B-02" in ids
        # The row count is still 2 (no duplicate)
        rows = conn.execute(
            "SELECT COUNT(*) FROM ticket_recents WHERE project_id = 'p1'"
        ).fetchone()[0]
        assert rows == 2

    def test_cap_at_20(self, conn):
        # Touch 25 tickets in sequence.
        for i in range(1, 26):
            actions.touch_recent(conn, "p1", f"B-{i:02d}")
        rows = conn.execute(
            "SELECT COUNT(*) FROM ticket_recents WHERE project_id = 'p1'"
        ).fetchone()[0]
        assert rows == actions.RECENTS_CAP == 20

    def test_cap_keeps_most_recent(self, conn):
        for i in range(1, 26):
            actions.touch_recent(conn, "p1", f"B-{i:02d}")
        ids = [i["id"] for i in actions.list_recents(conn, "p1")]
        # The oldest 5 (B-01..B-05) should have been trimmed.
        assert "B-25" in ids
        assert "B-01" not in ids
        assert "B-05" not in ids
        assert "B-06" in ids

    def test_unknown_ticket_raises(self, conn):
        with pytest.raises(actions.TicketNotFoundError):
            actions.touch_recent(conn, "p1", "NOPE-99")

    def test_project_scoped(self, conn):
        conn.execute(
            "INSERT INTO tickets (id, project_id, title, section, status) "
            "VALUES ('X-01', 'p2', 'P2', 'Backlog', 'proposed')"
        )
        conn.commit()
        actions.touch_recent(conn, "p1", "B-01")
        actions.touch_recent(conn, "p2", "X-01")
        assert [i["id"] for i in actions.list_recents(conn, "p1")] == ["B-01"]
        assert [i["id"] for i in actions.list_recents(conn, "p2")] == ["X-01"]
