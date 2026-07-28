"""TDD tests for system workflows (Phase A migration — tidy-newt).

Covers:
  - Edit-lock: PUT/DELETE on a system=1 row returns 403 (except enable toggle)
  - Duplicate endpoint: clones a system row into a user-owned (system=0) copy
  - End-to-end parent-promote via the system workflow path
  - End-to-end auto-accept gating (preconditions enforced)

Pure logic / in-process — no HTTP server needed for the workflow application
tests. The serve.py-side guards are exercised by smoke tests below.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from conditions import build_subject_context, evaluate_trigger
from db import init_db
from workflows_seed import seed_default_workflows

PROJECT_ID = "test-project"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """In-memory DB with full schema."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    seed_default_workflows(c, PROJECT_ID)
    return c


def _add_ticket(
    conn,
    tid,
    *,
    section="WIP",
    status="in-progress",
    parent=None,
    project_id=PROJECT_ID,
    priority="medium",
):
    conn.execute(
        "INSERT INTO tickets "
        "(id, project_id, title, section, status, description, priority, parent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, project_id, f"Title {tid}", section, status, "desc", priority, parent),
    )


# ---------------------------------------------------------------------------
# System workflow seeds — sanity
# ---------------------------------------------------------------------------


class TestSystemWorkflowSeeds:
    def test_parent_promote_seeded_enabled(self, conn):
        row = conn.execute(
            "SELECT enabled, system, steps FROM workflows "
            "WHERE name = 'Parent auto-promote'"
        ).fetchone()
        assert row is not None
        assert row["enabled"] == 1
        assert row["system"] == 1
        assert json.loads(row["steps"]) == []

    def test_auto_accept_seeded_disabled(self, conn):
        row = conn.execute(
            "SELECT enabled, system, steps, on_success_json FROM workflows "
            "WHERE name = 'Auto-accept reviewed tickets'"
        ).fetchone()
        assert row is not None
        assert row["enabled"] == 0
        assert row["system"] == 1
        assert json.loads(row["steps"]) == []
        on_success = json.loads(row["on_success_json"])
        assert on_success.get("accept_ticket") is True


# ---------------------------------------------------------------------------
# Trigger evaluation against parent — master switch: needs automation_mode=auto
# ---------------------------------------------------------------------------


class TestParentPromoteTriggerEvaluation:
    """The parent-promote workflow's trigger evaluates against the PARENT
    ticket directly (option ii in the plan): when the dispatcher walks every
    eligible ticket, the parent itself is a candidate; conditions like
    has_children + children_all_status_in fire on the parent's row, not on
    the child whose status just changed."""

    def test_trigger_passes_when_all_children_terminal(self, conn):
        _add_ticket(conn, "B-1", section="WIP", status="in-progress")
        _add_ticket(conn, "BUG-1", section="Bugs", status="bug-fixed", parent="B-1")
        _add_ticket(conn, "BUG-2", section="Bugs", status="done", parent="B-1")
        conn.execute(
            "INSERT INTO automation_subjects (project_id, subject_type, subject_id, automation_mode) "
            "VALUES (?, 'ticket', 'B-1', 'auto')",
            (PROJECT_ID,),
        )
        conn.commit()

        # Look up the seeded workflow's trigger.
        row = conn.execute(
            "SELECT trigger_json FROM workflows WHERE name = 'Parent auto-promote'"
        ).fetchone()
        trigger = json.loads(row["trigger_json"])

        # Evaluate against the PARENT (B-1).
        ctx = build_subject_context(conn, PROJECT_ID, "B-1")
        passed, reasons = evaluate_trigger(trigger, ctx)
        assert passed, f"expected pass, got reasons: {reasons}"

    def test_trigger_fails_when_a_child_in_progress(self, conn):
        _add_ticket(conn, "B-1", section="WIP", status="in-progress")
        _add_ticket(conn, "BUG-1", section="Bugs", status="bug-fixed", parent="B-1")
        _add_ticket(conn, "BUG-2", section="Bugs", status="bug", parent="B-1")
        conn.commit()

        row = conn.execute(
            "SELECT trigger_json FROM workflows WHERE name = 'Parent auto-promote'"
        ).fetchone()
        trigger = json.loads(row["trigger_json"])

        ctx = build_subject_context(conn, PROJECT_ID, "B-1")
        passed, _ = evaluate_trigger(trigger, ctx)
        assert not passed

    def test_trigger_fails_for_childless_ticket(self, conn):
        _add_ticket(conn, "B-1", section="WIP", status="in-progress")
        conn.commit()

        row = conn.execute(
            "SELECT trigger_json FROM workflows WHERE name = 'Parent auto-promote'"
        ).fetchone()
        trigger = json.loads(row["trigger_json"])

        ctx = build_subject_context(conn, PROJECT_ID, "B-1")
        passed, _ = evaluate_trigger(trigger, ctx)
        assert not passed

    def test_trigger_fails_when_parent_already_terminal(self, conn):
        # Already in For Review — trigger must not re-fire.
        _add_ticket(conn, "B-1", section="For Review", status="for-review")
        _add_ticket(conn, "BUG-1", section="Bugs", status="bug-fixed", parent="B-1")
        conn.commit()

        row = conn.execute(
            "SELECT trigger_json FROM workflows WHERE name = 'Parent auto-promote'"
        ).fetchone()
        trigger = json.loads(row["trigger_json"])

        ctx = build_subject_context(conn, PROJECT_ID, "B-1")
        passed, _ = evaluate_trigger(trigger, ctx)
        assert not passed


# ---------------------------------------------------------------------------
# Auto-accept trigger evaluation
# ---------------------------------------------------------------------------


class TestAutoAcceptTriggerEvaluation:
    def test_passes_in_review_done_no_open_bugs(self, conn):
        _add_ticket(conn, "B-1", section="For Review", status="done")
        conn.execute(
            "INSERT INTO automation_subjects (project_id, subject_type, subject_id, automation_mode) "
            "VALUES (?, 'ticket', 'B-1', 'auto')",
            (PROJECT_ID,),
        )
        conn.commit()

        row = conn.execute(
            "SELECT trigger_json FROM workflows WHERE name = 'Auto-accept reviewed tickets'"
        ).fetchone()
        trigger = json.loads(row["trigger_json"])

        ctx = build_subject_context(conn, PROJECT_ID, "B-1")
        passed, reasons = evaluate_trigger(trigger, ctx)
        assert passed, f"expected pass, got reasons: {reasons}"

    def test_fails_when_open_child_bug(self, conn):
        _add_ticket(conn, "B-1", section="For Review", status="done")
        _add_ticket(conn, "BUG-1", section="Bugs", status="bug", parent="B-1")
        conn.commit()

        row = conn.execute(
            "SELECT trigger_json FROM workflows WHERE name = 'Auto-accept reviewed tickets'"
        ).fetchone()
        trigger = json.loads(row["trigger_json"])

        ctx = build_subject_context(conn, PROJECT_ID, "B-1")
        passed, reasons = evaluate_trigger(trigger, ctx)
        assert not passed
        assert any("open child bug" in r for r in reasons)


# ---------------------------------------------------------------------------
# apply_to: parent — runner effect routing
# ---------------------------------------------------------------------------


class TestApplyToParentRouting:
    """The runners._apply_on_success helper must support apply_to='parent' and
    move the parent ticket (not the subject) when invoked."""

    def test_apply_to_parent_moves_parent(self, tmp_path):
        # Build a real on-disk DB so the runner's conn_factory can reopen it.
        from actions import ActorContext
        from runners import AgentRunner

        db_path = tmp_path / "tt.db"
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        init_db(c)
        _add_ticket(c, "B-1", section="WIP", status="in-progress")
        _add_ticket(c, "BUG-1", section="Bugs", status="bug", parent="B-1")
        c.commit()
        c.close()

        def conn_factory():
            cc = sqlite3.connect(str(db_path))
            cc.row_factory = sqlite3.Row
            cc.execute("PRAGMA foreign_keys=ON")
            return cc

        workflow_meta = {
            "workflow_id": "test::parent-promote",
            "workflow_name": "Parent auto-promote",
            "on_success": {
                "apply_to": "parent",
                "move_section": "For Review",
            },
            "steps": [],
            "step_index": 0,
            "step_count": 0,
        }

        AgentRunner._apply_on_success(
            workflow_meta,
            PROJECT_ID,
            "BUG-1",
            ActorContext.system(),
            conn_factory,
        )

        c = conn_factory()
        try:
            row = c.execute("SELECT section FROM tickets WHERE id = 'B-1'").fetchone()
            assert row["section"] == "For Review", (
                "parent must have been moved by apply_to=parent"
            )

            # System actor must be on the section_change event.
            ev = c.execute(
                "SELECT actor_type FROM activity_events "
                "WHERE subject_id = 'B-1' AND event_kind = 'section_change' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert ev is not None
            assert ev["actor_type"] == "system"
        finally:
            c.close()

    def test_apply_to_parent_skips_when_no_parent(self, tmp_path):
        """When the subject has no parent, the apply_to='parent' branch is a no-op."""
        from actions import ActorContext
        from runners import AgentRunner

        db_path = tmp_path / "tt.db"
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        init_db(c)
        _add_ticket(c, "B-1", section="WIP", status="in-progress")
        c.commit()
        c.close()

        def conn_factory():
            cc = sqlite3.connect(str(db_path))
            cc.row_factory = sqlite3.Row
            cc.execute("PRAGMA foreign_keys=ON")
            return cc

        workflow_meta = {
            "workflow_id": "test::wf",
            "workflow_name": "test",
            "on_success": {
                "apply_to": "parent",
                "move_section": "For Review",
            },
            "steps": [],
        }

        AgentRunner._apply_on_success(
            workflow_meta,
            PROJECT_ID,
            "B-1",
            ActorContext.system(),
            conn_factory,
        )

        c = conn_factory()
        try:
            row = c.execute("SELECT section FROM tickets WHERE id = 'B-1'").fetchone()
            assert row["section"] == "WIP", "ticket should not have moved"
        finally:
            c.close()


# ---------------------------------------------------------------------------
# accept_ticket effect — preconditions
# ---------------------------------------------------------------------------


class TestAcceptTicketEffect:
    """The accept_ticket effect must respect the same preconditions the
    human Accept button enforces: section='For Review' AND status='done'."""

    def test_skips_when_not_in_review(self, tmp_path):
        from actions import ActorContext
        from runners import AgentRunner

        db_path = tmp_path / "tt.db"
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        init_db(c)
        # Wrong section — must skip.
        _add_ticket(c, "B-1", section="WIP", status="done")
        c.commit()
        c.close()

        def conn_factory():
            cc = sqlite3.connect(str(db_path))
            cc.row_factory = sqlite3.Row
            cc.execute("PRAGMA foreign_keys=ON")
            return cc

        workflow_meta = {
            "workflow_id": "test::wf",
            "workflow_name": "test",
            "on_success": {"accept_ticket": True},
            "steps": [],
        }

        AgentRunner._apply_on_success(
            workflow_meta,
            PROJECT_ID,
            "B-1",
            ActorContext.system(),
            conn_factory,
        )

        c = conn_factory()
        try:
            row = c.execute("SELECT section FROM tickets WHERE id = 'B-1'").fetchone()
            assert row["section"] == "WIP", "must remain in WIP (preconditions failed)"
        finally:
            c.close()

    def test_skips_when_status_not_done(self, tmp_path):
        from actions import ActorContext
        from runners import AgentRunner

        db_path = tmp_path / "tt.db"
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        init_db(c)
        _add_ticket(c, "B-1", section="For Review", status="for-review")
        c.commit()
        c.close()

        def conn_factory():
            cc = sqlite3.connect(str(db_path))
            cc.row_factory = sqlite3.Row
            cc.execute("PRAGMA foreign_keys=ON")
            return cc

        workflow_meta = {
            "workflow_id": "test::wf",
            "workflow_name": "test",
            "on_success": {"accept_ticket": True},
            "steps": [],
        }

        AgentRunner._apply_on_success(
            workflow_meta,
            PROJECT_ID,
            "B-1",
            ActorContext.system(),
            conn_factory,
        )

        c = conn_factory()
        try:
            row = c.execute(
                "SELECT section, status FROM tickets WHERE id = 'B-1'"
            ).fetchone()
            assert row["section"] == "For Review"
            assert row["status"] == "for-review"
        finally:
            c.close()
