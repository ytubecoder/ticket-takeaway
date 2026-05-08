"""TDD tests for migration 15 — collapse Smoke + Tests readiness flags
into acceptance_criteria.

Pure logic. No server, no Playwright. The migration itself is exercised by
running init_db() against a synthetic pre-migration schema.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _build_pre_migration_schema(conn: sqlite3.Connection) -> None:
    """Lay out the minimal schema needed before migration 15 runs.

    We only need tickets, acceptance_criteria, and readiness_flags. The
    `_migrations` row (1..14) is inserted so init_db skips earlier work.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id          TEXT NOT NULL,
            project_id  TEXT NOT NULL,
            title       TEXT NOT NULL,
            priority    TEXT NOT NULL DEFAULT 'medium',
            status      TEXT NOT NULL DEFAULT 'proposed',
            section     TEXT NOT NULL DEFAULT 'Ideas',
            description TEXT NOT NULL DEFAULT '',
            parent      TEXT,
            rationale   TEXT NOT NULL DEFAULT '',
            summary     TEXT NOT NULL DEFAULT '',
            archived    INTEGER NOT NULL DEFAULT 0,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (id, project_id)
        );

        CREATE TABLE IF NOT EXISTS acceptance_criteria (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   TEXT NOT NULL,
            project_id  TEXT NOT NULL,
            text        TEXT NOT NULL,
            checked     INTEGER NOT NULL DEFAULT 0,
            sort_order  INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS readiness_flags (
            ticket_id   TEXT NOT NULL,
            project_id  TEXT NOT NULL,
            flag        TEXT NOT NULL,
            content     TEXT NOT NULL DEFAULT '',
            set_at      TEXT NOT NULL DEFAULT (datetime('now')),
            set_by      TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (ticket_id, project_id, flag)
        );
        """
    )


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _run_init_db(conn):
    from db import init_db
    init_db(conn)


class TestMigration15:
    def test_tests_flag_content_becomes_criteria(self, conn):
        _build_pre_migration_schema(conn)
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES (?, ?, ?)",
            ("B-1", "p", "T"),
        )
        conn.execute(
            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content) "
            "VALUES (?, ?, ?, ?)",
            ("B-1", "p", "tests", "- [ ] unit test foo\n- [x] integration covers bar"),
        )
        conn.commit()

        _run_init_db(conn)

        rows = conn.execute(
            "SELECT text, checked FROM acceptance_criteria "
            "WHERE ticket_id = 'B-1' AND project_id = 'p' ORDER BY sort_order"
        ).fetchall()
        texts = [(r["text"], r["checked"]) for r in rows]
        assert ("unit test foo", 0) in texts
        assert ("integration covers bar", 1) in texts

    def test_smoke_flag_content_becomes_criteria(self, conn):
        _build_pre_migration_schema(conn)
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES (?, ?, ?)",
            ("B-2", "p", "T2"),
        )
        conn.execute(
            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content) "
            "VALUES (?, ?, ?, ?)",
            ("B-2", "p", "smoke", "Open the app\nClick the button"),
        )
        conn.commit()

        _run_init_db(conn)

        rows = conn.execute(
            "SELECT text FROM acceptance_criteria WHERE ticket_id = 'B-2' "
            "AND project_id = 'p' ORDER BY sort_order"
        ).fetchall()
        texts = [r["text"] for r in rows]
        assert "Open the app" in texts
        assert "Click the button" in texts

    def test_tests_and_smoke_flag_rows_deleted(self, conn):
        _build_pre_migration_schema(conn)
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES (?, ?, ?)",
            ("B-3", "p", "T3"),
        )
        conn.executemany(
            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content) "
            "VALUES (?, ?, ?, ?)",
            [
                ("B-3", "p", "tests", "tests body"),
                ("B-3", "p", "smoke", ""),
                ("B-3", "p", "reviewed", "kept"),
            ],
        )
        conn.commit()

        _run_init_db(conn)

        flags = {
            r["flag"] for r in conn.execute(
                "SELECT flag FROM readiness_flags WHERE ticket_id = 'B-3' "
                "AND project_id = 'p'"
            ).fetchall()
        }
        assert "tests" not in flags
        assert "smoke" not in flags
        assert "reviewed" in flags

    def test_dedupe_against_existing_criteria(self, conn):
        _build_pre_migration_schema(conn)
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES (?, ?, ?)",
            ("B-4", "p", "T4"),
        )
        conn.execute(
            "INSERT INTO acceptance_criteria (ticket_id, project_id, text, sort_order) "
            "VALUES (?, ?, ?, ?)",
            ("B-4", "p", "Existing item", 0),
        )
        conn.execute(
            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content) "
            "VALUES (?, ?, ?, ?)",
            ("B-4", "p", "tests", "  Existing item\nNew item"),
        )
        conn.commit()

        _run_init_db(conn)

        rows = conn.execute(
            "SELECT text FROM acceptance_criteria WHERE ticket_id = 'B-4' "
            "AND project_id = 'p' ORDER BY sort_order"
        ).fetchall()
        texts = [r["text"] for r in rows]
        # Dedupe is case-insensitive, whitespace-collapsed
        assert texts.count("Existing item") == 1
        assert "New item" in texts

    def test_idempotent_second_run_is_noop(self, conn):
        _build_pre_migration_schema(conn)
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES (?, ?, ?)",
            ("B-5", "p", "T5"),
        )
        conn.execute(
            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content) "
            "VALUES (?, ?, ?, ?)",
            ("B-5", "p", "tests", "only criterion"),
        )
        conn.commit()

        _run_init_db(conn)
        first = conn.execute(
            "SELECT COUNT(*) AS n FROM acceptance_criteria WHERE ticket_id = 'B-5'"
        ).fetchone()["n"]

        _run_init_db(conn)
        second = conn.execute(
            "SELECT COUNT(*) AS n FROM acceptance_criteria WHERE ticket_id = 'B-5'"
        ).fetchone()["n"]

        assert first == second == 1
