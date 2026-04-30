"""TDD tests for workflows_seed.py — default system workflow seeding.

Pure logic, no server, no Playwright.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import get_db, init_db
from workflows_seed import seed_default_workflows, DEFAULT_WORKFLOWS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """In-memory DB with full schema (all migrations including migration 9)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c


PROJECT_ID = "test-project"


# ---------------------------------------------------------------------------
# Migration 9 — new columns exist
# ---------------------------------------------------------------------------

class TestMigration9:
    def test_workflows_has_system_column(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workflows)").fetchall()}
        assert "system" in cols

    def test_workflows_has_enabled_column(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workflows)").fetchall()}
        assert "enabled" in cols

    def test_workflows_has_trigger_json_column(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workflows)").fetchall()}
        assert "trigger_json" in cols

    def test_workflows_has_on_success_json_column(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workflows)").fetchall()}
        assert "on_success_json" in cols

    def test_workflows_has_subject_type_column(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workflows)").fetchall()}
        assert "subject_type" in cols


# ---------------------------------------------------------------------------
# DEFAULT_WORKFLOWS manifest
# ---------------------------------------------------------------------------

class TestDefaultWorkflowsManifest:
    def test_exactly_six_workflows(self):
        assert len(DEFAULT_WORKFLOWS) == 6

    def test_all_have_name(self):
        for wf in DEFAULT_WORKFLOWS:
            assert wf.get("name"), f"workflow missing name: {wf}"

    def test_all_are_system(self):
        for wf in DEFAULT_WORKFLOWS:
            assert wf["system"] == 1

    def test_auto_fire_workflows_have_trigger_json(self):
        """Plan Check is manual-only (trigger_json=None); all others must have one."""
        for wf in DEFAULT_WORKFLOWS:
            if wf["name"] == "Plan Check":
                assert wf["trigger_json"] is None
            else:
                assert wf.get("trigger_json"), f"{wf['name']!r} missing trigger_json"

    def test_all_have_steps(self):
        for wf in DEFAULT_WORKFLOWS:
            assert wf.get("steps"), f"{wf['name']!r} missing steps"

    def test_review_to_done_is_disabled(self):
        """Review → Done must be disabled by default (never auto-accept)."""
        rv_done = next(wf for wf in DEFAULT_WORKFLOWS if wf["name"] == "Review → Done")
        assert rv_done["enabled"] == 0, "Review → Done must be disabled by default"

    def test_trigger_json_round_trips(self):
        for wf in DEFAULT_WORKFLOWS:
            serialised = json.dumps(wf["trigger_json"])
            parsed = json.loads(serialised)
            assert parsed == wf["trigger_json"]

    def test_steps_are_lists(self):
        for wf in DEFAULT_WORKFLOWS:
            assert isinstance(wf["steps"], list)

    def test_steps_have_prompt_template(self):
        for wf in DEFAULT_WORKFLOWS:
            for step in wf["steps"]:
                assert "prompt_template" in step, f"{wf['name']!r} step missing prompt_template"


# ---------------------------------------------------------------------------
# seed_default_workflows
# ---------------------------------------------------------------------------

class TestSeedDefaultWorkflows:
    def test_inserts_six_system_workflows(self, conn):
        result = seed_default_workflows(conn, PROJECT_ID)
        assert result["inserted"] == 6
        count = conn.execute(
            "SELECT COUNT(*) FROM workflows WHERE system = 1 AND id LIKE ?",
            (f"{PROJECT_ID}::%",),
        ).fetchone()[0]
        assert count == 6

    def test_existing_is_zero_on_first_run(self, conn):
        result = seed_default_workflows(conn, PROJECT_ID)
        assert result["existing"] == 0

    def test_idempotent_second_run(self, conn):
        first = seed_default_workflows(conn, PROJECT_ID)
        second = seed_default_workflows(conn, PROJECT_ID)
        assert first["inserted"] == 6
        assert second["inserted"] == 0
        assert second["existing"] == 6

    def test_idempotent_count_stays_at_six(self, conn):
        seed_default_workflows(conn, PROJECT_ID)
        seed_default_workflows(conn, PROJECT_ID)
        count = conn.execute(
            "SELECT COUNT(*) FROM workflows WHERE system = 1",
        ).fetchone()[0]
        assert count == 6

    def test_different_projects_get_independent_workflows(self, conn):
        seed_default_workflows(conn, "project-a")
        seed_default_workflows(conn, "project-b")
        count_a = conn.execute(
            "SELECT COUNT(*) FROM workflows WHERE system = 1 AND id LIKE 'project-a::%'"
        ).fetchone()[0]
        count_b = conn.execute(
            "SELECT COUNT(*) FROM workflows WHERE system = 1 AND id LIKE 'project-b::%'"
        ).fetchone()[0]
        assert count_a == 6
        assert count_b == 6

    def test_stored_trigger_json_is_valid_json(self, conn):
        """Auto-fire workflows must be dicts; Plan Check stores JSON null."""
        seed_default_workflows(conn, PROJECT_ID)
        rows = conn.execute(
            "SELECT trigger_json FROM workflows WHERE system = 1 AND id LIKE ?",
            (f"{PROJECT_ID}::%",),
        ).fetchall()
        for row in rows:
            parsed = json.loads(row["trigger_json"])
            assert isinstance(parsed, dict) or parsed is None

    def test_stored_steps_is_valid_json_list(self, conn):
        seed_default_workflows(conn, PROJECT_ID)
        rows = conn.execute(
            "SELECT steps FROM workflows WHERE system = 1 AND id LIKE ?",
            (f"{PROJECT_ID}::%",),
        ).fetchall()
        for row in rows:
            parsed = json.loads(row["steps"])
            assert isinstance(parsed, list)
            assert len(parsed) >= 1

    def test_review_to_done_stored_as_disabled(self, conn):
        seed_default_workflows(conn, PROJECT_ID)
        row = conn.execute(
            "SELECT enabled FROM workflows WHERE name = 'Review → Done' AND system = 1",
        ).fetchone()
        assert row is not None
        assert row["enabled"] == 0

    def test_backlog_to_wip_is_enabled(self, conn):
        seed_default_workflows(conn, PROJECT_ID)
        row = conn.execute(
            "SELECT enabled FROM workflows WHERE name = 'Backlog → WIP' AND system = 1",
        ).fetchone()
        assert row is not None
        assert row["enabled"] == 1

    def test_all_workflows_have_subject_type_ticket(self, conn):
        seed_default_workflows(conn, PROJECT_ID)
        rows = conn.execute(
            "SELECT subject_type FROM workflows WHERE system = 1 AND id LIKE ?",
            (f"{PROJECT_ID}::%",),
        ).fetchall()
        for row in rows:
            assert row["subject_type"] == "ticket"

    def test_trigger_json_has_all_of_or_any_of(self, conn):
        """Auto-fire workflows must have all_of/any_of; Plan Check is exempt (null)."""
        seed_default_workflows(conn, PROJECT_ID)
        rows = conn.execute(
            "SELECT name, trigger_json FROM workflows WHERE system = 1 AND id LIKE ?",
            (f"{PROJECT_ID}::%",),
        ).fetchall()
        for row in rows:
            if row["trigger_json"] == "null":
                continue  # Plan Check is manual-only
            parsed = json.loads(row["trigger_json"])
            assert "all_of" in parsed or "any_of" in parsed

    def test_existing_non_system_workflows_untouched(self, conn):
        """Seeder must not modify user-created (system=0) workflows."""
        conn.execute(
            "INSERT INTO workflows (id, name, description, steps) VALUES (?, ?, ?, ?)",
            ("user-wf-1", "My workflow", "custom", "[]"),
        )
        conn.commit()
        seed_default_workflows(conn, PROJECT_ID)
        row = conn.execute(
            "SELECT name FROM workflows WHERE id = 'user-wf-1'"
        ).fetchone()
        assert row is not None
        assert row["name"] == "My workflow"
