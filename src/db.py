"""Database layer for Ticket Takeaway — SQLite connection, schema, and migrations.

Extracted from tickets-cli.py so it can be imported by both the CLI and serve.py.
"""

import sqlite3
from pathlib import Path

from constants import DASHBOARD_DIR, DB_PATH


# Registry path — used by registry helpers in the CLI and other modules.
REGISTRY_PATH = DASHBOARD_DIR / "registry.json"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db(db_path: str = None) -> sqlite3.Connection:
    """Open (or create) the SQLite database with WAL mode and FK support."""
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection):
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tickets (
            id          TEXT NOT NULL,
            project_id  TEXT NOT NULL,
            title       TEXT NOT NULL,
            priority    TEXT NOT NULL DEFAULT 'medium',
            complexity  TEXT NOT NULL DEFAULT 'M',
            status      TEXT NOT NULL DEFAULT 'proposed',
            section     TEXT NOT NULL DEFAULT 'Ideas',
            column      TEXT NOT NULL DEFAULT 'ideas',
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
            sort_order  INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (ticket_id, project_id) REFERENCES tickets(id, project_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS depends (
            ticket_id       TEXT NOT NULL,
            project_id      TEXT NOT NULL,
            depends_on_id   TEXT NOT NULL,
            PRIMARY KEY (ticket_id, project_id, depends_on_id),
            FOREIGN KEY (ticket_id, project_id) REFERENCES tickets(id, project_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS readiness_flags (
            ticket_id   TEXT NOT NULL,
            project_id  TEXT NOT NULL,
            flag        TEXT NOT NULL,
            set_at      TEXT NOT NULL DEFAULT (datetime('now')),
            set_by      TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (ticket_id, project_id, flag),
            FOREIGN KEY (ticket_id, project_id) REFERENCES tickets(id, project_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scheduled_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,
            ticket_id   TEXT NOT NULL,
            project_id  TEXT NOT NULL,
            payload     TEXT NOT NULL DEFAULT '{}',
            fire_at     TEXT NOT NULL,
            fired       INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_tickets_project_section ON tickets(project_id, section);
        CREATE INDEX IF NOT EXISTS idx_criteria_ticket ON acceptance_criteria(ticket_id, project_id);
        CREATE INDEX IF NOT EXISTS idx_depends_ticket ON depends(ticket_id, project_id);
        CREATE INDEX IF NOT EXISTS idx_readiness_ticket ON readiness_flags(ticket_id, project_id);
        CREATE INDEX IF NOT EXISTS idx_scheduled_events_fire ON scheduled_events(fired, fire_at);

        CREATE TABLE IF NOT EXISTS _sync_state (
            project_id    TEXT PRIMARY KEY,
            last_md_hash  TEXT NOT NULL DEFAULT '',
            last_sync_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)

    # Migrate: add commit_hash and release_tag columns if missing
    for col, default in [("commit_hash", "''"), ("release_tag", "''")]:
        try:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Migrate: add content column to readiness_flags if missing
    try:
        conn.execute("ALTER TABLE readiness_flags ADD COLUMN content TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Migrate: dedup rows — keep earliest rowid per (id, project_id)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (version INTEGER PRIMARY KEY)
    """)
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 1").fetchone():
        conn.execute("""
            DELETE FROM tickets WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM tickets GROUP BY UPPER(id), project_id
            )
        """)
        conn.execute("INSERT INTO _migrations (version) VALUES (1)")
        conn.commit()
