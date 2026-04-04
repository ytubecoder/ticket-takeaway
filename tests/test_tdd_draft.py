"""TDD tests for draft ticket concept and attachments data model."""

import json
import sqlite3
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import get_db, init_db


@pytest.fixture
def conn():
    """In-memory DB with schema initialized."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c


class TestDraftColumn:
    def test_draft_column_exists(self, conn):
        cols = [row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()]
        assert "draft" in cols

    def test_draft_default_is_zero(self, conn):
        conn.execute("INSERT INTO tickets (id, project_id, title) VALUES ('T-01', 'test', 'Test')")
        row = conn.execute("SELECT draft FROM tickets WHERE id = 'T-01'").fetchone()
        assert row["draft"] == 0

    def test_draft_can_be_set_to_one(self, conn):
        conn.execute("INSERT INTO tickets (id, project_id, title, draft) VALUES ('T-02', 'test', 'Draft', 1)")
        row = conn.execute("SELECT draft FROM tickets WHERE id = 'T-02'").fetchone()
        assert row["draft"] == 1


class TestSourceAttachmentIdColumn:
    def test_source_attachment_id_column_exists(self, conn):
        cols = [row[1] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()]
        assert "source_attachment_id" in cols

    def test_source_attachment_id_default_null(self, conn):
        conn.execute("INSERT INTO tickets (id, project_id, title) VALUES ('T-03', 'test', 'Test')")
        row = conn.execute("SELECT source_attachment_id FROM tickets WHERE id = 'T-03'").fetchone()
        assert row["source_attachment_id"] is None


class TestAttachmentsTable:
    def test_ticket_attachments_table_exists(self, conn):
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "ticket_attachments" in tables

    def test_insert_attachment(self, conn):
        conn.execute("INSERT INTO tickets (id, project_id, title) VALUES ('T-01', 'test', 'Test')")
        conn.execute(
            "INSERT INTO ticket_attachments "
            "(ticket_id, project_id, attachment_type, name, path, summary, metadata) "
            "VALUES ('T-01', 'test', 'feedbacks_session', 'feedbacks-2026-04-04', '/tmp/sess', 'A summary', '{}')"
        )
        row = conn.execute("SELECT * FROM ticket_attachments WHERE ticket_id = 'T-01'").fetchone()
        assert row["attachment_type"] == "feedbacks_session"
        assert row["name"] == "feedbacks-2026-04-04"
        assert row["triage_status"] == ""

    def test_unique_constraint(self, conn):
        conn.execute("INSERT INTO tickets (id, project_id, title) VALUES ('T-01', 'test', 'Test')")
        conn.execute(
            "INSERT INTO ticket_attachments (ticket_id, project_id, attachment_type, name) "
            "VALUES ('T-01', 'test', 'feedbacks_session', 'sess-1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO ticket_attachments (ticket_id, project_id, attachment_type, name) "
                "VALUES ('T-01', 'test', 'feedbacks_session', 'sess-1')"
            )

    def test_same_session_different_tickets(self, conn):
        conn.execute("INSERT INTO tickets (id, project_id, title) VALUES ('T-01', 'test', 'A')")
        conn.execute("INSERT INTO tickets (id, project_id, title) VALUES ('T-02', 'test', 'B')")
        conn.execute(
            "INSERT INTO ticket_attachments (ticket_id, project_id, attachment_type, name) "
            "VALUES ('T-01', 'test', 'feedbacks_session', 'sess-1')"
        )
        conn.execute(
            "INSERT INTO ticket_attachments (ticket_id, project_id, attachment_type, name) "
            "VALUES ('T-02', 'test', 'feedbacks_session', 'sess-1')"
        )
        count = conn.execute("SELECT COUNT(*) as c FROM ticket_attachments WHERE name = 'sess-1'").fetchone()
        assert count["c"] == 2

    def test_cascade_delete(self, conn):
        conn.execute("INSERT INTO tickets (id, project_id, title) VALUES ('T-01', 'test', 'Test')")
        conn.execute(
            "INSERT INTO ticket_attachments (ticket_id, project_id, attachment_type, name) "
            "VALUES ('T-01', 'test', 'feedbacks_session', 'sess-1')"
        )
        conn.execute("DELETE FROM tickets WHERE id = 'T-01' AND project_id = 'test'")
        count = conn.execute("SELECT COUNT(*) as c FROM ticket_attachments").fetchone()
        assert count["c"] == 0


class TestSettingsTable:
    def test_settings_table_exists(self, conn):
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "settings" in tables

    def test_insert_and_read_setting(self, conn):
        conn.execute("INSERT INTO settings (key, value) VALUES ('feedbacks.enabled', 'true')")
        row = conn.execute("SELECT value FROM settings WHERE key = 'feedbacks.enabled'").fetchone()
        assert row["value"] == "true"

    def test_upsert_setting(self, conn):
        conn.execute("INSERT INTO settings (key, value) VALUES ('foo', 'bar')")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('foo', 'baz')")
        row = conn.execute("SELECT value FROM settings WHERE key = 'foo'").fetchone()
        assert row["value"] == "baz"
