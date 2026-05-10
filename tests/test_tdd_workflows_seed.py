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
    def test_exactly_ten_workflows(self):
        # Lane A (factory-talk): added "Done → Learnings extraction" and
        # "Sprint tag rotation", raising the count from 7 to 9.
        # Migration 18: added "Refresh ticket summary" → 10.
        assert len(DEFAULT_WORKFLOWS) == 10

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

    def test_all_have_steps_field(self):
        """Every workflow must have a steps field (list); pure-mutation system
        workflows like parent-promote / auto-accept ship empty step lists."""
        for wf in DEFAULT_WORKFLOWS:
            assert "steps" in wf, f"{wf['name']!r} missing steps key"
            assert isinstance(wf["steps"], list)

    def test_parent_promote_is_enabled(self):
        """Parent auto-promote must be enabled by default (preserves legacy hook)."""
        wf = next(w for w in DEFAULT_WORKFLOWS if w["name"] == "Parent auto-promote")
        assert wf["enabled"] == 1

    def test_auto_accept_is_disabled(self):
        """Auto-accept must be disabled by default (memory: never auto-accept)."""
        wf = next(w for w in DEFAULT_WORKFLOWS if w["name"] == "Auto-accept reviewed tickets")
        assert wf["enabled"] == 0

    def test_parent_promote_has_zero_steps(self):
        wf = next(w for w in DEFAULT_WORKFLOWS if w["name"] == "Parent auto-promote")
        assert wf["steps"] == []

    def test_auto_accept_has_zero_steps(self):
        wf = next(w for w in DEFAULT_WORKFLOWS if w["name"] == "Auto-accept reviewed tickets")
        assert wf["steps"] == []

    def test_trigger_json_round_trips(self):
        for wf in DEFAULT_WORKFLOWS:
            serialised = json.dumps(wf["trigger_json"])
            parsed = json.loads(serialised)
            assert parsed == wf["trigger_json"]

    def test_steps_are_lists(self):
        for wf in DEFAULT_WORKFLOWS:
            assert isinstance(wf["steps"], list)

    def test_steps_have_prompt_template(self):
        """Workflows with steps must define prompt_template per step. Zero-step
        workflows (system mutation rules) are exempt."""
        for wf in DEFAULT_WORKFLOWS:
            for step in wf["steps"]:
                assert "prompt_template" in step, f"{wf['name']!r} step missing prompt_template"


# ---------------------------------------------------------------------------
# seed_default_workflows
# ---------------------------------------------------------------------------

class TestSeedDefaultWorkflows:
    def test_links_ten_system_workflows_on_first_run(self, conn):
        # Lane A raised the count from 7 to 9. Migration 18 added
        # "Refresh ticket summary" → 10. Migration-16 model: seeder
        # creates/updates canonical rows then inserts workflow_projects links.
        result = seed_default_workflows(conn, PROJECT_ID)
        assert result["linked"] == 10
        count = conn.execute(
            "SELECT COUNT(*) FROM workflows WHERE system = 1",
        ).fetchone()[0]
        assert count == 10

    def test_no_links_outstanding_on_second_run(self, conn):
        # Idempotent — second call finds all links already present.
        seed_default_workflows(conn, PROJECT_ID)
        second = seed_default_workflows(conn, PROJECT_ID)
        assert second["linked"] == 0
        assert second["already_linked"] == 10

    def test_idempotent_count_stays_at_ten(self, conn):
        seed_default_workflows(conn, PROJECT_ID)
        seed_default_workflows(conn, PROJECT_ID)
        count = conn.execute(
            "SELECT COUNT(*) FROM workflows WHERE system = 1",
        ).fetchone()[0]
        assert count == 10

    def test_different_projects_get_independent_links(self, conn):
        # Canonical workflow rows are shared; each project gets its own link rows.
        seed_default_workflows(conn, "project-a")
        seed_default_workflows(conn, "project-b")
        count_a = conn.execute(
            "SELECT COUNT(*) FROM workflow_projects WHERE project_id = 'project-a'"
        ).fetchone()[0]
        count_b = conn.execute(
            "SELECT COUNT(*) FROM workflow_projects WHERE project_id = 'project-b'"
        ).fetchone()[0]
        assert count_a == 10
        assert count_b == 10

    def test_stored_trigger_json_is_valid_json(self, conn):
        """Auto-fire workflows must be dicts; Plan Check stores JSON null."""
        seed_default_workflows(conn, PROJECT_ID)
        rows = conn.execute(
            "SELECT trigger_json FROM workflows WHERE system = 1",
        ).fetchall()
        for row in rows:
            parsed = json.loads(row["trigger_json"])
            assert isinstance(parsed, dict) or parsed is None

    def test_stored_steps_is_valid_json_list(self, conn):
        """Steps must always parse as a list. Pure-mutation system workflows
        (parent-promote, auto-accept) ship empty step lists; that's allowed."""
        seed_default_workflows(conn, PROJECT_ID)
        rows = conn.execute(
            "SELECT steps FROM workflows WHERE system = 1",
        ).fetchall()
        for row in rows:
            parsed = json.loads(row["steps"])
            assert isinstance(parsed, list)

    def test_auto_accept_stored_as_disabled(self, conn):
        seed_default_workflows(conn, PROJECT_ID)
        row = conn.execute(
            "SELECT enabled FROM workflows WHERE name = 'Auto-accept reviewed tickets' AND system = 1",
        ).fetchone()
        assert row is not None
        assert row["enabled"] == 0

    def test_parent_promote_stored_as_enabled(self, conn):
        seed_default_workflows(conn, PROJECT_ID)
        row = conn.execute(
            "SELECT enabled FROM workflows WHERE name = 'Parent auto-promote' AND system = 1",
        ).fetchone()
        assert row is not None
        assert row["enabled"] == 1

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
            "SELECT subject_type FROM workflows WHERE system = 1",
        ).fetchall()
        for row in rows:
            assert row["subject_type"] == "ticket"

    def test_trigger_json_has_all_of_or_any_of(self, conn):
        """Auto-fire workflows must have all_of/any_of; Plan Check is exempt (null)."""
        seed_default_workflows(conn, PROJECT_ID)
        rows = conn.execute(
            "SELECT name, trigger_json FROM workflows WHERE system = 1",
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
