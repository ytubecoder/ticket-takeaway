"""Database layer for Ticket Takeaway — SQLite connection, schema, and migrations.

Extracted from tickets-cli.py so it can be imported by both the CLI and serve.py.
"""

import json
import sqlite3
from datetime import datetime, timezone

from constants import DASHBOARD_DIR, DB_PATH

# Registry path — used by registry helpers in the CLI and other modules.
REGISTRY_PATH = DASHBOARD_DIR / "registry.json"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_db(db_path: str | None = None) -> sqlite3.Connection:
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
            conn.execute(
                f"ALTER TABLE tickets ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Migrate: add content column to readiness_flags if missing
    try:
        conn.execute(
            "ALTER TABLE readiness_flags ADD COLUMN content TEXT NOT NULL DEFAULT ''"
        )
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
            conn.execute(
                "ALTER TABLE tickets ADD COLUMN draft INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE tickets ADD COLUMN source_attachment_id INTEGER DEFAULT NULL"
            )
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

    # Migration 6: ticket tags
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 6").fetchone():
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ticket_tags (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                tag         TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (ticket_id, project_id) REFERENCES tickets(id, project_id) ON DELETE CASCADE,
                UNIQUE(ticket_id, project_id, tag)
            );

            CREATE INDEX IF NOT EXISTS idx_tags_ticket ON ticket_tags(ticket_id, project_id);
            CREATE INDEX IF NOT EXISTS idx_tags_project ON ticket_tags(project_id, tag);
        """)
        conn.execute("INSERT INTO _migrations (version) VALUES (6)")
        conn.commit()

    # Migration 7: ticket branches (GitHub branch awareness)
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 7").fetchone():
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ticket_branches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                branch_name TEXT NOT NULL,
                remote      TEXT NOT NULL DEFAULT 'origin',
                pr_number   INTEGER,
                pr_status   TEXT NOT NULL DEFAULT '',
                pr_url      TEXT NOT NULL DEFAULT '',
                ahead       INTEGER NOT NULL DEFAULT 0,
                behind      INTEGER NOT NULL DEFAULT 0,
                auto_linked INTEGER NOT NULL DEFAULT 0,
                last_synced TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(ticket_id, project_id, branch_name)
            );

            CREATE INDEX IF NOT EXISTS idx_branches_ticket ON ticket_branches(ticket_id, project_id);
            CREATE INDEX IF NOT EXISTS idx_branches_project ON ticket_branches(project_id);
        """)
        conn.execute("INSERT INTO _migrations (version) VALUES (7)")
        conn.commit()

    # Migration 8: Kitchen — automation intent, run facts, activity audit.
    # See docs/KITCHEN.md §6 for the full rationale. Numbered 8 because
    # main shipped migrations 6 (ticket_tags) and 7 (ticket_branches) while
    # the kitchen branch was in flight; the kitchen schema is independent
    # of those so the renumber is purely about avoiding the version-key
    # collision in the _migrations table.
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 8").fetchone():
        # Eligibility bypass for tickets that genuinely don't need tests.
        # CHECK constraints on ALTER ADD COLUMN are version-dependent in SQLite;
        # the (0,1) invariant is enforced by actions.py helpers.
        for col, decl in [
            ("no_test_required", "INTEGER NOT NULL DEFAULT 0"),
            ("no_test_required_note", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        conn.executescript("""
            -- Intent: how the human (or agent) wants this subject treated.
            CREATE TABLE IF NOT EXISTS automation_subjects (
                project_id      TEXT NOT NULL,
                subject_type    TEXT NOT NULL CHECK (subject_type IN ('ticket','journey','investigation')),
                subject_id      TEXT NOT NULL,
                automation_mode TEXT NOT NULL DEFAULT 'manual'
                                CHECK (automation_mode IN ('manual','auto','paused')),
                pause_reason    TEXT,
                watched_at      TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                created_by      TEXT,
                updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_by      TEXT,
                PRIMARY KEY (project_id, subject_type, subject_id)
            );

            -- Facts: every execution attempt against any subject.
            CREATE TABLE IF NOT EXISTS runs (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id         TEXT NOT NULL,
                subject_type       TEXT NOT NULL CHECK (subject_type IN ('ticket','journey','investigation')),
                subject_id         TEXT NOT NULL,
                runner_kind        TEXT NOT NULL CHECK (runner_kind IN ('agent','scenario','gap_analyzer','noop')),
                status             TEXT NOT NULL CHECK (status IN
                                     ('queued','preparing','running','needs_input',
                                      'succeeded','failed','stalled','cancelled')),
                workspace_path     TEXT,
                thread_id          TEXT,

                claimed_at         TEXT,
                claim_owner        TEXT,
                heartbeat_at       TEXT,

                started_at         TEXT,
                finished_at        TEXT,
                duration_ms        INTEGER,

                exit_code          INTEGER,
                error_class        TEXT,
                error_message      TEXT,
                summary            TEXT,
                metadata_json      TEXT NOT NULL DEFAULT '{}',
                evidence_dir       TEXT,
                evidence_status    TEXT NOT NULL DEFAULT 'live'
                                   CHECK (evidence_status IN ('live','summarised','pruned')),

                needs_input_prompt TEXT,

                attempt            INTEGER NOT NULL DEFAULT 1,
                parent_run_id      INTEGER,
                retry_kind         TEXT CHECK (retry_kind IS NULL OR retry_kind IN ('resume','fresh')),
                triggered_by       TEXT NOT NULL CHECK (triggered_by IN
                                     ('human','run-now','journey-cascade','retry','scheduled','pr-merge'))
            );

            CREATE INDEX IF NOT EXISTS runs_subject_latest
                ON runs (project_id, subject_type, subject_id, id DESC);
            CREATE INDEX IF NOT EXISTS runs_active
                ON runs (status) WHERE status IN ('queued','preparing','running','needs_input');
            CREATE INDEX IF NOT EXISTS runs_evidence_age
                ON runs (finished_at, evidence_status);

            -- DURABILITY: at most one active run per subject. Makes "is this
            -- subject currently being worked?" derivable from runs without a
            -- denormalized cache, and prevents double-dispatch under any race.
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_subject
                ON runs (project_id, subject_type, subject_id)
                WHERE status IN ('queued','preparing','running','needs_input');

            -- Audit: every state-changing event with actor attribution.
            CREATE TABLE IF NOT EXISTS activity_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id       TEXT NOT NULL,
                subject_type     TEXT NOT NULL CHECK (subject_type IN ('ticket','journey','investigation')),
                subject_id       TEXT NOT NULL,
                actor_type       TEXT NOT NULL CHECK (actor_type IN ('human','agent','system')),
                actor_id         TEXT,
                event_kind       TEXT NOT NULL,
                payload_json     TEXT NOT NULL DEFAULT '{}',
                occurred_at      TEXT NOT NULL DEFAULT (datetime('now')),
                discarded_run_id INTEGER
            );

            CREATE INDEX IF NOT EXISTS activity_subject
                ON activity_events (project_id, subject_type, subject_id, occurred_at DESC);
            CREATE INDEX IF NOT EXISTS activity_run
                ON activity_events (actor_type, actor_id) WHERE actor_type = 'agent';
        """)
        conn.execute("INSERT INTO _migrations (version) VALUES (8)")
        conn.commit()

    # Migration 9: Unified workflow triggers — add columns to workflows table
    # so system workflows can carry declarative trigger/outcome JSON.
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 9").fetchone():
        for col, decl in [
            ("system", "INTEGER DEFAULT 0"),
            ("enabled", "INTEGER DEFAULT 1"),
            ("trigger_json", "TEXT"),
            ("on_success_json", "TEXT"),
            ("subject_type", "TEXT DEFAULT 'ticket'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE workflows ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # Column already exists
        conn.execute("INSERT INTO _migrations (version) VALUES (9)")
        conn.commit()

    # Migration 10: Scope workflows to a project. Backfill existing system rows
    # whose IDs follow the `{project_id}::sys::{slug}` pattern; user-created
    # rows with NULL project_id remain shared until edited.
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 10").fetchone():
        try:
            conn.execute("ALTER TABLE workflows ADD COLUMN project_id TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            UPDATE workflows
               SET project_id = substr(id, 1, instr(id, '::sys::') - 1)
             WHERE system = 1 AND project_id IS NULL AND instr(id, '::sys::') > 0
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS workflows_project ON workflows (project_id)"
        )
        conn.execute("INSERT INTO _migrations (version) VALUES (10)")
        conn.commit()

    # Migration 11: Plan Check — persist_session on agents, session_ids on runs.
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 11").fetchone():
        try:
            conn.execute(
                "ALTER TABLE workflow_agents ADD COLUMN persist_session INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            conn.execute(
                "ALTER TABLE workflow_runs ADD COLUMN session_ids TEXT DEFAULT '{}'"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.execute("INSERT INTO _migrations (version) VALUES (11)")
        conn.commit()

    # Migration 12 (Phase A — tidy-newt): widen the runs.runner_kind CHECK
    # constraint to include 'noop'. The NoopRunner handles zero-step system
    # workflows like Parent auto-promote and Auto-accept reviewed tickets —
    # pure mutation rules with no agent subprocess. SQLite can't ALTER a
    # CHECK constraint in place, so we rebuild the table preserving every
    # existing column (whatever migrations 8/9/10/11 added).
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 12").fetchone():
        existing_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()
        if existing_sql_row and "'noop'" not in (existing_sql_row["sql"] or ""):
            # Discover the actual column list dynamically so we don't lose any
            # field added by future migrations (or by older ones not in the
            # canonical CREATE TABLE above on this DB).
            old_cols_rows = conn.execute("PRAGMA table_info(runs)").fetchall()
            col_names = [c["name"] for c in old_cols_rows]
            col_list_sql = ", ".join(col_names)

            # Build the new CREATE statement by replacing the old CHECK with
            # the widened one. We do this textually because SQLite preserves
            # the original CREATE in sqlite_master, including any extra
            # columns added by later migrations. This keeps the rebuild
            # forward-compatible: we lose nothing.
            new_create_sql = existing_sql_row["sql"].replace(
                "CHECK (runner_kind IN ('agent','scenario','gap_analyzer'))",
                "CHECK (runner_kind IN ('agent','scenario','gap_analyzer','noop'))",
            )
            # Rename the table reference to runs_new for the rebuild.
            new_create_sql = new_create_sql.replace(
                "CREATE TABLE runs",
                "CREATE TABLE runs_new",
                1,
            )
            new_create_sql = new_create_sql.replace(
                'CREATE TABLE "runs"',
                "CREATE TABLE runs_new",
                1,
            )

            # Defer FK checks for the swap (best-effort; no FKs target runs).
            fk_was_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                # Defensive: clean up any orphan runs_new from a prior aborted
                # migration attempt before recreating it.
                conn.execute("DROP TABLE IF EXISTS runs_new")
                conn.execute(new_create_sql)
                conn.execute(
                    f"INSERT INTO runs_new ({col_list_sql}) "
                    f"SELECT {col_list_sql} FROM runs"
                )
                conn.execute("DROP TABLE runs")
                conn.execute("ALTER TABLE runs_new RENAME TO runs")
                # Recreate canonical indexes (CREATE IF NOT EXISTS — no-op if
                # the rebuild preserved them, which it doesn't because indexes
                # are tied to the old table name).
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS runs_subject_latest "
                    "ON runs (project_id, subject_type, subject_id, id DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS runs_active "
                    "ON runs (status) WHERE status IN "
                    "('queued','preparing','running','needs_input')"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS runs_evidence_age "
                    "ON runs (finished_at, evidence_status)"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_subject "
                    "ON runs (project_id, subject_type, subject_id) "
                    "WHERE status IN ('queued','preparing','running','needs_input')"
                )
            finally:
                if fk_was_on:
                    conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO _migrations (version) VALUES (12)")
        conn.commit()

    # Migration 13 (Phase B — tidy-newt): retire the `complexity` column.
    # Its only remaining use was decorative (kanban size filter). We back-fill
    # the human-meaningful values into ticket_tags so the labels users have
    # applied (~239 tickets) survive, then drop the column. SQLite 3.35+
    # supports DROP COLUMN natively; Python 3.10+ ships with 3.45+.
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 13").fetchone():
        cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()
        }
        if "complexity" in cols:
            conn.execute(
                "INSERT OR IGNORE INTO ticket_tags (ticket_id, project_id, tag) "
                "SELECT id, project_id, "
                "  CASE complexity "
                "    WHEN 'S'  THEN 'Small' "
                "    WHEN 'M'  THEN 'Medium' "
                "    WHEN 'L'  THEN 'Large' "
                "    WHEN 'XL' THEN 'XL' "
                "  END "
                "FROM tickets "
                "WHERE complexity IN ('S','M','L','XL')"
            )
            conn.execute("ALTER TABLE tickets DROP COLUMN complexity")
        conn.execute("INSERT INTO _migrations (version) VALUES (13)")
        conn.commit()

    # Migration 14 — rename automation_mode value 'held' → 'paused' and the
    # accompanying column 'hold_reason' → 'pause_reason'. The UX rationale:
    # 'held' is ambiguous (problem? deliberate?), 'paused' signals user intent.
    # SQLite can't modify CHECK constraints in place, so we rebuild the table.
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 14").fetchone():
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(automation_subjects)").fetchall()
        }
        if "hold_reason" in cols and "pause_reason" not in cols:
            conn.executescript("""
                CREATE TABLE automation_subjects_new (
                    project_id      TEXT NOT NULL,
                    subject_type    TEXT NOT NULL CHECK (subject_type IN ('ticket','journey','investigation')),
                    subject_id      TEXT NOT NULL,
                    automation_mode TEXT NOT NULL DEFAULT 'manual'
                                    CHECK (automation_mode IN ('manual','auto','paused')),
                    pause_reason    TEXT,
                    watched_at      TEXT,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    created_by      TEXT,
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_by      TEXT,
                    PRIMARY KEY (project_id, subject_type, subject_id)
                );
                INSERT INTO automation_subjects_new
                  (project_id, subject_type, subject_id, automation_mode,
                   pause_reason, watched_at, created_at, created_by,
                   updated_at, updated_by)
                SELECT
                  project_id, subject_type, subject_id,
                  CASE automation_mode WHEN 'held' THEN 'paused' ELSE automation_mode END,
                  hold_reason, watched_at, created_at, created_by,
                  updated_at, updated_by
                FROM automation_subjects;
                DROP TABLE automation_subjects;
                ALTER TABLE automation_subjects_new RENAME TO automation_subjects;
            """)
        # Rename event names so historical activity reads in the new vocab.
        conn.execute(
            "UPDATE activity_events SET event_kind = 'pause_set' "
            "WHERE event_kind = 'hold_set'"
        )
        conn.execute(
            "UPDATE activity_events SET event_kind = 'pause_cleared' "
            "WHERE event_kind = 'hold_cleared'"
        )
        conn.execute("INSERT INTO _migrations (version) VALUES (14)")
        conn.commit()

    # Migration 15 — collapse the Smoke and Tests readiness flags into
    # acceptance_criteria. The DCSTL readiness model becomes DCL: Description,
    # Criteria, Learnings. Migrates non-empty 'tests' / 'smoke' content into
    # discrete criteria rows (deduped against existing criteria, case-insensitive),
    # then deletes those readiness_flags rows. Empty 'smoke' rows are also
    # removed. The `tickets.no_test_required*` columns are left in place; their
    # only consumer was `_tests_covered`, which is no longer wired into any
    # default workflow gate.
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 15").fetchone():
        flag_rows = conn.execute(
            "SELECT ticket_id, project_id, flag, content FROM readiness_flags "
            "WHERE flag IN ('tests', 'smoke')"
        ).fetchall()
        for fr in flag_rows:
            ticket_id = fr["ticket_id"]
            project_id = fr["project_id"]
            content = (fr["content"] or "").strip()
            if not content:
                continue
            existing_rows = conn.execute(
                "SELECT text, checked, sort_order FROM acceptance_criteria "
                "WHERE ticket_id = ? AND project_id = ?",
                (ticket_id, project_id),
            ).fetchall()
            existing_norm = {
                " ".join((r["text"] or "").split()).lower() for r in existing_rows
            }
            next_order = max((r["sort_order"] for r in existing_rows), default=-1) + 1
            for raw_line in content.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                checked = 0
                stripped = line
                if stripped.startswith(("- ", "* ", "• ")):
                    stripped = stripped[2:].lstrip()
                if stripped[:4].lower() in ("[ ] ", "[x] "):
                    checked = 1 if stripped[1].lower() == "x" else 0
                    stripped = stripped[4:].lstrip()
                stripped = stripped.strip()
                if not stripped:
                    continue
                norm = " ".join(stripped.split()).lower()
                if norm in existing_norm:
                    continue
                conn.execute(
                    "INSERT INTO acceptance_criteria "
                    "(ticket_id, project_id, text, checked, sort_order) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ticket_id, project_id, stripped, checked, next_order),
                )
                existing_norm.add(norm)
                next_order += 1
        conn.execute("DELETE FROM readiness_flags WHERE flag IN ('tests', 'smoke')")
        conn.execute("INSERT INTO _migrations (version) VALUES (15)")
        conn.commit()

    # Migration 16: First-class workflows + workflow_projects join table.
    #
    # Before: each project had its own copy of every system workflow (e.g. 5
    # projects × 8 templates = 40 rows). Editing one meant editing N copies.
    #
    # After: one canonical workflow row per logical template; project linkage
    # lives in `workflow_projects(workflow_id, project_id, enabled)`. Per-project
    # enable/disable state moves into the join table; the workflow body is
    # edit-once-applies-everywhere.
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 16").fetchone():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_projects (
                workflow_id TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (workflow_id, project_id),
                FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS wf_proj_workflow ON workflow_projects (workflow_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS wf_proj_project ON workflow_projects (project_id)"
        )

        conn.execute("""
            INSERT OR IGNORE INTO workflow_projects (workflow_id, project_id, enabled)
            SELECT id, project_id, COALESCE(enabled, 1)
            FROM workflows
            WHERE project_id IS NOT NULL
        """)

        canonical_rows = conn.execute("""
            SELECT name, MIN(id) AS canonical_id
            FROM workflows
            WHERE system = 1
            GROUP BY name
        """).fetchall()
        for r in canonical_rows:
            name = r["name"]
            canonical_id = r["canonical_id"]
            dup_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM workflows WHERE system = 1 AND name = ? AND id <> ?",
                    (name, canonical_id),
                ).fetchall()
            ]
            if not dup_ids:
                continue
            placeholders = ",".join("?" * len(dup_ids))
            conn.execute(
                f"""
                INSERT OR REPLACE INTO workflow_projects (workflow_id, project_id, enabled, created_at)
                SELECT ?, project_id, MAX(enabled), MIN(created_at)
                FROM workflow_projects
                WHERE workflow_id IN ({placeholders})
                GROUP BY project_id
            """,
                (canonical_id, *dup_ids),
            )
            conn.execute(
                f"DELETE FROM workflow_projects WHERE workflow_id IN ({placeholders})",
                dup_ids,
            )
            conn.execute(
                f"UPDATE workflow_runs SET workflow_id = ? WHERE workflow_id IN ({placeholders})",
                (canonical_id, *dup_ids),
            )
            conn.execute(
                f"DELETE FROM workflows WHERE id IN ({placeholders})",
                dup_ids,
            )

        conn.execute("UPDATE workflows SET project_id = NULL WHERE system = 1")

        conn.execute("INSERT INTO _migrations (version) VALUES (16)")
        conn.commit()

    # Migration 17 — Factory-talk primitives.
    #
    # Three idempotent ALTER TABLE additions:
    #   1. tickets.is_container       — cosmetic flag for container/epic rendering
    #   2. workflow_agents.system     — mirrors workflows.system; agents can now
    #                                  be system-flagged (Orchestrator/Worker/Validator)
    #   3. runs.needs_input_kind      — distinguishes the two interactive pause
    #                                  shapes: 'text' (free-text reply) vs 'propose'
    #                                  (structured patch accept/decline). NULL on all
    #                                  non-interactive runs.
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 17").fetchone():
        cols_tickets = {
            row["name"] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()
        }
        if "is_container" not in cols_tickets:
            conn.execute(
                "ALTER TABLE tickets ADD COLUMN is_container INTEGER NOT NULL DEFAULT 0"
            )

        cols_agents = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(workflow_agents)").fetchall()
        }
        if "system" not in cols_agents:
            conn.execute(
                "ALTER TABLE workflow_agents ADD COLUMN system INTEGER NOT NULL DEFAULT 0"
            )

        cols_runs = {
            row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "needs_input_kind" not in cols_runs:
            conn.execute("ALTER TABLE runs ADD COLUMN needs_input_kind TEXT")

        conn.execute("INSERT INTO _migrations (version) VALUES (17)")
        conn.commit()

    # Migration 18 — Ticket one-liner summary cache.
    #
    # Two columns on tickets:
    #   1. summary_oneliner — short status sentence cached for cheap rendering
    #                         in the ticket detail overlay (regenerated by the
    #                         "Refresh ticket summary" system workflow).
    #   2. summary_hash     — SHA-256 (hex prefix) of the summary input fields.
    #                         The workflow trigger fires when this differs from
    #                         a freshly computed hash, i.e. content has changed
    #                         since the last summary. Empty hash = never summarised.
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 18").fetchone():
        cols_tickets = {
            row["name"] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()
        }
        if "summary_oneliner" not in cols_tickets:
            conn.execute(
                "ALTER TABLE tickets ADD COLUMN summary_oneliner TEXT NOT NULL DEFAULT ''"
            )
        if "summary_hash" not in cols_tickets:
            conn.execute(
                "ALTER TABLE tickets ADD COLUMN summary_hash TEXT NOT NULL DEFAULT ''"
            )

        conn.execute("INSERT INTO _migrations (version) VALUES (18)")
        conn.commit()

    # Migration 19: Backfill ticket_created activity events for existing tickets
    # that predate the centralised emit in actions.add_ticket. Without this, the
    # Activity tab on older tickets has no "origin" entry and looks like the
    # ticket appeared from nowhere. We try to recover the origin where we can:
    # - Drafts whose description starts with "Source: <type> @ <file>:<line>"
    #   were created by Seek; we tag origin='seek' with the parsed metadata.
    # - Everything else gets origin='backfill' (unknown — predates tracking).
    if not conn.execute("SELECT 1 FROM _migrations WHERE version = 19").fetchone():
        import re as _re

        seek_re = _re.compile(r"^Source:\s+(\S+)\s+@\s+(.+):(\d+)")
        rows = conn.execute(
            "SELECT t.id, t.project_id, t.section, t.title, t.draft, "
            "       t.description, t.created_at "
            "FROM tickets t "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM activity_events e "
            "  WHERE e.subject_type = 'ticket' AND e.subject_id = t.id "
            "    AND e.project_id = t.project_id AND e.event_kind = 'ticket_created'"
            ")"
        ).fetchall()
        for r in rows:
            payload = {
                "id": r["id"],
                "title": r["title"],
                "section": r["section"],
                "draft": bool(r["draft"]),
            }
            seek_match = seek_re.match(r["description"] or "")
            if seek_match and r["draft"]:
                payload["origin"] = "seek"
                payload["source_type"] = seek_match.group(1)
                payload["source_file"] = seek_match.group(2)
                try:
                    payload["source_line"] = int(seek_match.group(3))
                except ValueError:
                    payload["source_line"] = seek_match.group(3)
            else:
                payload["origin"] = "backfill"
            conn.execute(
                "INSERT INTO activity_events "
                "(project_id, subject_type, subject_id, actor_type, actor_id, "
                " event_kind, payload_json, occurred_at) "
                "VALUES (?, 'ticket', ?, 'system', NULL, 'ticket_created', ?, ?)",
                (
                    r["project_id"],
                    r["id"],
                    json.dumps(payload, ensure_ascii=False),
                    r["created_at"] or datetime.now(timezone.utc).isoformat(),
                ),
            )
        conn.execute("INSERT INTO _migrations (version) VALUES (19)")
        conn.commit()

    _apply_migration_20(conn)
    _apply_migration_21(conn)
    _apply_migration_23(conn)


def _apply_migration_20(conn) -> None:
    """Migration 20: add endpoints table + workflow_agents.endpoint_id,
    backfill data from existing workflow_agents.

    All work happens in a single transaction. The _migrations row is
    inserted last, before the implicit commit, so partial state is
    impossible.
    """
    if conn.execute("SELECT 1 FROM _migrations WHERE version = 20").fetchone():
        return

    # 1. Create endpoints table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS endpoints (
            id             TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            endpoint_type  TEXT NOT NULL CHECK (endpoint_type IN
                             ('cli','anthropic_api','openai_api',
                              'gemini_api','ssh_cli')),
            provider       TEXT,
            model          TEXT,
            base_url       TEXT,
            api_key_env    TEXT,
            command        TEXT,
            args           TEXT NOT NULL DEFAULT '[]',
            prompt_mode    TEXT NOT NULL DEFAULT 'template'
                             CHECK (prompt_mode IN ('template','stdin')),
            timeout_s      INTEGER NOT NULL DEFAULT 120,
            capabilities   TEXT NOT NULL DEFAULT '{}',
            session_config TEXT NOT NULL DEFAULT '{}',
            system         INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # 2. Add endpoint_id column to workflow_agents (idempotent guard)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(workflow_agents)").fetchall()}
    if "endpoint_id" not in cols:
        conn.execute(
            "ALTER TABLE workflow_agents ADD COLUMN endpoint_id TEXT "
            "REFERENCES endpoints(id) ON DELETE SET NULL"
        )

    # 3. Data backfill (Task 7 fills this in — for now just stub it)
    _backfill_endpoints_from_agents(conn)

    # 4. Record version (last step in transaction)
    conn.execute("INSERT INTO _migrations (version) VALUES (20)")
    conn.commit()


def _backfill_endpoints_from_agents(conn) -> None:
    """Backfill endpoints from workflow_agents rows. Pin known system
    runtimes to canonical seeded ids; create synthesised endpoints for
    everything else. Idempotent — skips rows whose endpoint_id is set.

    See spec section 'Data migration' for the full contract.
    """
    import json as _json
    import logging as _logging

    log = _logging.getLogger("migration20.backfill")

    # Import canonical mappings lazily to avoid circular imports
    try:
        from workflows_seed import KNOWN_CLI_MAPPINGS
    except Exception:
        KNOWN_CLI_MAPPINGS = {}

    def _pinned_build_argv(command, args_list):
        """Migration-local copy of today's _build_agent_cmd transformation
        for known commands, with '{prompt}' as the literal prompt token.

        Returns the args-only portion (command stripped), suitable for
        storing in endpoints.args.
        """
        cmd = (command or "").lower()
        base = list(args_list or [])
        if cmd == "claude":
            return base + ["-p", "{prompt}", "--output-format", "json"]
        if cmd == "codex":
            return base + ["{prompt}"]
        return base + ["{prompt}"]

    counters = {
        "created": 0,
        "reused": 0,
        "remapped": 0,
        "malformed_args": 0,
        "collisions": 0,
    }

    agents = conn.execute("""
        SELECT id, command, args, system, persist_session
        FROM workflow_agents
        WHERE endpoint_id IS NULL
    """).fetchall()

    # First pass: compute per-agent tuple
    plan = []  # list of (agent_id, command, raw_args_tuple, eff_argv_tuple, system, persist)
    for agent_id, command, args_text, system_flag, persist in agents:
        try:
            raw_args = _json.loads(args_text or "[]")
            if not isinstance(raw_args, list) or not all(
                isinstance(x, str) for x in raw_args
            ):
                raise ValueError("args is not a list of strings")
        except Exception as e:
            log.warning(
                f"agent_id={agent_id} has malformed "
                f"args={args_text!r}, defaulting to [] ({e})"
            )
            raw_args = []
            counters["malformed_args"] += 1
        eff_argv = _pinned_build_argv(command, raw_args)
        plan.append(
            (
                agent_id,
                command,
                tuple(raw_args),
                tuple(eff_argv),
                system_flag or 0,
                persist or 0,
            )
        )

    # Group by (command, effective_argv, system)
    groups = {}
    for entry in plan:
        agent_id, command, raw_args, eff_argv, sysflag, persist = entry
        key = (command, eff_argv, sysflag)
        groups.setdefault(key, []).append(entry)

    # Resolve each group to an endpoint id (canonical if known, else create)
    for (command, eff_argv, sysflag), members in groups.items():
        # Try canonical mapping (system rows only). KNOWN_CLI_MAPPINGS
        # uses RAW args (pre _build_agent_cmd transformation), so look
        # up by the raw args of any group member (all members share it).
        canonical_id = None
        if sysflag == 1:
            canonical_id = KNOWN_CLI_MAPPINGS.get(
                (command, members[0][2])
            )  # members[0][2] = raw_args tuple

        if canonical_id:
            endpoint_id = canonical_id
            # Ensure the row exists as a placeholder; the seed pass that
            # runs at server startup will upsert the canonical fields.
            # For now insert a minimal row so the FK is valid.
            existing = conn.execute(
                "SELECT 1 FROM endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
            if not existing:
                conn.execute(
                    """
                    INSERT INTO endpoints
                      (id, name, endpoint_type, command, args, system)
                    VALUES (?, ?, 'cli', ?, ?, 1)
                """,
                    (endpoint_id, endpoint_id, command, _json.dumps(list(eff_argv))),
                )
                counters["created"] += 1
            else:
                counters["reused"] += 1
        else:
            # Synthesise a new endpoint
            endpoint_id = _synthesise_endpoint_id(
                conn, command, list(eff_argv), sysflag
            )
            sessions = 1 if any(p for *_, p in members) else 0
            session_config = {}
            if command == "claude":
                session_config = {
                    "resume_args": list(eff_argv) + ["--resume", "{session_id}"],
                    "session_id_regex": r'"session_id"\s*:\s*"([0-9a-f-]+)"',
                }
            elif command == "codex":
                session_config = {
                    "resume_args": ["exec", "resume", "{session_id}"],
                    "session_id_regex": r"Session(?:\s+ID)?\s*:\s*([0-9a-f-]+)",
                    "session_id_fallback_dir": "~/.codex/sessions/",
                }
            conn.execute(
                """
                INSERT INTO endpoints
                  (id, name, endpoint_type, command, args,
                   capabilities, session_config, system)
                VALUES (?, ?, 'cli', ?, ?, ?, ?, ?)
            """,
                (
                    endpoint_id,
                    _synth_name(command, list(eff_argv)),
                    command,
                    _json.dumps(list(eff_argv)),
                    _json.dumps({"sessions": bool(sessions)}),
                    _json.dumps(session_config),
                    sysflag,
                ),
            )
            counters["created"] += 1

        # Remap every member agent to this endpoint
        for agent_id, *_ in members:
            conn.execute(
                "UPDATE workflow_agents SET endpoint_id = ? WHERE id = ?",
                (endpoint_id, agent_id),
            )
            counters["remapped"] += 1

    log.info(
        f"endpoints backfill: created={counters['created']} "
        f"reused={counters['reused']} "
        f"agents_remapped={counters['remapped']} "
        f"malformed_args_defaulted={counters['malformed_args']} "
        f"id_collisions_resolved={counters['collisions']}"
    )


def _synthesise_endpoint_id(conn, command, eff_argv, sysflag) -> str:
    """Generate a unique slugified id from command + args. On collision,
    append -2, -3, ..."""
    import re as _re

    base = command or "endpoint"
    if eff_argv and len(eff_argv) > 0:
        # Use first non-placeholder arg if available
        non_placeholder = next(
            (a for a in eff_argv if a and not (a.startswith("{") and a.endswith("}"))),
            None,
        )
        if non_placeholder:
            base = f"{base}-{non_placeholder}"
    slug = _re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-").lower()
    if sysflag == 0:
        slug = f"usr-{slug}"
    candidate = slug
    n = 2
    while conn.execute("SELECT 1 FROM endpoints WHERE id = ?", (candidate,)).fetchone():
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


def _synth_name(command, eff_argv) -> str:
    summary = " ".join(
        a for a in eff_argv if not (a and a.startswith("{") and a.endswith("}"))
    )[:50]
    return f"{command} {summary}".strip()


def _apply_migration_21(conn) -> None:
    """Migration 21: bookmarks + recents per project."""
    if conn.execute("SELECT 1 FROM _migrations WHERE version = 21").fetchone():
        return

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticket_bookmarks (
            project_id TEXT NOT NULL,
            ticket_id  TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (project_id, ticket_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ticket_bookmarks_project
        ON ticket_bookmarks (project_id, created_at DESC)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticket_recents (
            project_id   TEXT NOT NULL,
            ticket_id    TEXT NOT NULL,
            last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (project_id, ticket_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ticket_recents_project
        ON ticket_recents (project_id, last_seen_at DESC)
    """)

    conn.execute("INSERT INTO _migrations (version) VALUES (21)")
    conn.commit()


def _apply_migration_23(conn) -> None:
    """Migration 23: pane_links — bind tmux panes to tickets so TT can show
    a live tail of the pane and detect "needs attention" events.  See
    docs/superpowers/specs/2026-05-12-pane-link-design.md.

    Numbered 23 (not 20/21/22) to avoid collision with main's
    endpoints=20 and bookmarks=21.  Migration 22 was never used.
    """
    if conn.execute("SELECT 1 FROM _migrations WHERE version = 23").fetchone():
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pane_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            pane_address TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            pane_descriptor TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            last_captured_at INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            attention_state TEXT NOT NULL DEFAULT 'none',
            attention_detected_at INTEGER,
            tail_text TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pane_links_ticket ON pane_links(project_id, ticket_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pane_links_host_status ON pane_links(host, status)"
    )
    conn.execute("INSERT INTO _migrations (version) VALUES (23)")
    conn.commit()
