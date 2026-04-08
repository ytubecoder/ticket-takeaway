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

    # Migration 2: drop redundant column field (always derived from section)
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 2").fetchone():
        try:
            conn.execute("ALTER TABLE tickets DROP COLUMN column")
        except sqlite3.OperationalError:
            pass  # Column already gone or never existed
        conn.execute("INSERT INTO _migrations (version) VALUES (2)")
        conn.commit()

    # Migration 3: draft tickets, attachments, settings
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 3").fetchone():
        try:
            conn.execute("ALTER TABLE tickets ADD COLUMN draft INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE tickets ADD COLUMN source_attachment_id INTEGER DEFAULT NULL")
        except sqlite3.OperationalError:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ticket_attachments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id       TEXT NOT NULL,
                project_id      TEXT NOT NULL,
                attachment_type TEXT NOT NULL,
                name            TEXT NOT NULL,
                path            TEXT NOT NULL DEFAULT '',
                summary         TEXT NOT NULL DEFAULT '',
                metadata        TEXT NOT NULL DEFAULT '{}',
                triage_status   TEXT NOT NULL DEFAULT '',
                triage_result   TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (ticket_id, project_id) REFERENCES tickets(id, project_id) ON DELETE CASCADE,
                UNIQUE(ticket_id, project_id, name, attachment_type)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_attachments_ticket
                ON ticket_attachments(ticket_id, project_id)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)

        conn.execute("INSERT INTO _migrations (version) VALUES (3)")
        conn.commit()

    # Migration 4: workflow bounce — agents, workflows, workflow runs
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 4").fetchone():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_agents (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                command       TEXT NOT NULL DEFAULT 'claude',
                args          TEXT NOT NULL DEFAULT '[]',
                system_prompt TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflows (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                steps       TEXT NOT NULL DEFAULT '[]',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id            TEXT PRIMARY KEY,
                ticket_id     TEXT NOT NULL,
                project_id    TEXT NOT NULL,
                workflow_id   TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending',
                current_step  INTEGER NOT NULL DEFAULT 0,
                total_steps   INTEGER NOT NULL DEFAULT 0,
                conversation  TEXT NOT NULL DEFAULT '[]',
                started_at    TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at  TEXT,
                FOREIGN KEY (ticket_id, project_id) REFERENCES tickets(id, project_id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workflow_runs_ticket
                ON workflow_runs(ticket_id, project_id)
        """)

        conn.execute("INSERT INTO _migrations (version) VALUES (4)")
        conn.commit()

    # Migration 5: User Journeys — journeys, steps, runs, step results, ticket links
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 5").fetchone():
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS journeys (
                id            TEXT NOT NULL,
                project_id    TEXT NOT NULL,
                title         TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                persona       TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'draft',
                seed_json     TEXT NOT NULL DEFAULT '{}',
                actors_json   TEXT NOT NULL DEFAULT '{"user": {"label": "User"}}',
                viewport_json TEXT NOT NULL DEFAULT '{"width": 1440, "height": 1024}',
                theme         TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (id, project_id)
            );

            CREATE TABLE IF NOT EXISTS journey_steps (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                journey_id    TEXT NOT NULL,
                project_id    TEXT NOT NULL,
                sort_order    INTEGER NOT NULL DEFAULT 0,
                label         TEXT NOT NULL DEFAULT '',
                actor         TEXT NOT NULL DEFAULT 'user',
                action        TEXT NOT NULL,
                target_json   TEXT NOT NULL DEFAULT '{}',
                value         TEXT NOT NULL DEFAULT '',
                key           TEXT NOT NULL DEFAULT '',
                capture_json  TEXT NOT NULL DEFAULT '',
                assert_json   TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (journey_id, project_id) REFERENCES journeys(id, project_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS journey_runs (
                id            TEXT PRIMARY KEY,
                journey_id    TEXT NOT NULL,
                project_id    TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending',
                started_at    TEXT,
                finished_at   TEXT,
                duration_ms   INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                artifact_dir  TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (journey_id, project_id) REFERENCES journeys(id, project_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS journey_step_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT NOT NULL,
                step_id         INTEGER NOT NULL,
                sort_order      INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL,
                error_message   TEXT NOT NULL DEFAULT '',
                screenshot_path TEXT NOT NULL DEFAULT '',
                duration_ms     INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (run_id) REFERENCES journey_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (step_id) REFERENCES journey_steps(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS journey_tickets (
                journey_id  TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                ticket_id   TEXT NOT NULL,
                step_id     INTEGER,
                PRIMARY KEY (journey_id, project_id, ticket_id),
                FOREIGN KEY (journey_id, project_id) REFERENCES journeys(id, project_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_journey_steps_journey
                ON journey_steps(journey_id, project_id);
            CREATE INDEX IF NOT EXISTS idx_journey_runs_journey
                ON journey_runs(journey_id, project_id);
            CREATE INDEX IF NOT EXISTS idx_journey_step_results_run
                ON journey_step_results(run_id);
        """)
        conn.execute("INSERT INTO _migrations (version) VALUES (5)")
        conn.commit()
