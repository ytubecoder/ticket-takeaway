"""TDD tests for the workflow editor — catalog, linter, and the new on_success actions.

Pure-logic tests that don't need a server. Covers:
  - ui_catalog() shape stability and coverage
  - lint_closed_loop() across the four canonical states (ok/warn/manual/empty)
  - The four new on_success effects (set_automation_mode, set_priority,
    set_is_container, clear_readiness_flag) round-trip through the runner's
    _apply_on_success helper
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------


class TestUiCatalogShape:
    def test_catalog_is_stable_dict(self):
        from conditions import ui_catalog

        cat = ui_catalog()
        assert isinstance(cat, dict)
        for required in (
            "attributes",
            "apply_to_targets",
            "options",
            "effect_to_attribute",
            "predicate_to_attribute",
        ):
            assert required in cat, f"missing key: {required}"

    def test_every_attribute_has_label_and_filter_or_action(self):
        from conditions import ui_catalog

        cat = ui_catalog()
        for attr in cat["attributes"]:
            assert "key" in attr and "label" in attr
            assert isinstance(attr.get("filter_ops", []), list)
            assert isinstance(attr.get("action_ops", []), list)
            # Every attribute exposes at least one filter or action op
            assert len(attr["filter_ops"]) + len(attr["action_ops"]) > 0

    def test_filter_ops_reference_real_predicate_kinds(self):
        from conditions import CONDITION_CATALOG, ui_catalog

        cat = ui_catalog()
        unwired = []  # predicate kinds the catalog references that aren't wired in CONDITION_CATALOG
        for attr in cat["attributes"]:
            for op in attr["filter_ops"]:
                kind = op.get("predicate_kind")
                if kind and kind not in CONDITION_CATALOG:
                    unwired.append((attr["key"], kind))
        # is_container is documented as not yet wired; every other reference must resolve
        unwired_kinds = {k for _, k in unwired}
        assert unwired_kinds <= {"is_container"}, (
            f"catalog references predicate kinds not in CONDITION_CATALOG: {unwired}"
        )

    def test_apply_to_targets_includes_self_and_parent(self):
        from conditions import ui_catalog

        keys = {t["key"] for t in ui_catalog()["apply_to_targets"]}
        assert "self" in keys
        assert "parent" in keys

    def test_effect_to_attribute_covers_new_actions(self):
        from conditions import ui_catalog

        e2a = ui_catalog()["effect_to_attribute"]
        for new_key in (
            "set_automation_mode",
            "set_priority",
            "set_is_container",
            "clear_readiness_flag",
        ):
            assert new_key in e2a, f"effect_to_attribute missing {new_key}"

    def test_accept_ticket_is_a_visible_action_op_under_section(self):
        """accept_ticket effect must surface in the editor (was silently dropped)."""
        from conditions import ui_catalog

        section = next(a for a in ui_catalog()["attributes"] if a["key"] == "section")
        accept_ops = [
            op
            for op in section["action_ops"]
            if op["on_success_key"] == "accept_ticket"
        ]
        assert len(accept_ops) == 1, (
            "Section.action_ops must include an op with on_success_key='accept_ticket' so "
            "the Auto-accept reviewed tickets workflow renders its action in the editor"
        )

    def test_predicate_to_attribute_covers_every_known_predicate(self):
        from conditions import CONDITION_CATALOG, ui_catalog

        p2a = ui_catalog()["predicate_to_attribute"]
        # Every CONDITION_CATALOG kind should map to an attribute (so the
        # linter never silently ignores a real predicate).
        unmapped = [k for k in CONDITION_CATALOG if k not in p2a]
        assert unmapped == [], f"predicate_to_attribute missing: {unmapped}"


# ---------------------------------------------------------------------------
# Linter
# ---------------------------------------------------------------------------


class TestLintClosedLoop:
    def test_manual_workflow_returns_manual_status(self):
        from conditions import lint_closed_loop

        r = lint_closed_loop(None, None)
        assert r["status"] == "manual"
        r2 = lint_closed_loop("", {})
        assert r2["status"] == "manual"
        r3 = lint_closed_loop("null", {})
        assert r3["status"] == "manual"

    def test_no_actions_returns_empty_status(self):
        from conditions import lint_closed_loop

        r = lint_closed_loop({"kind": "section_equals", "value": "Backlog"}, {})
        assert r["status"] == "empty"

    def test_closed_loop_marks_ok(self):
        from conditions import lint_closed_loop

        r = lint_closed_loop(
            {"all_of": [{"kind": "section_in", "values": ["Backlog"]}]},
            {"move_section": "WIP"},
        )
        assert r["status"] == "ok"
        assert "section" in r["shared"]

    def test_warn_when_action_attribute_does_not_match_filter(self):
        from conditions import lint_closed_loop

        r = lint_closed_loop(
            {"all_of": [{"kind": "has_children"}]},
            {"add_tags": ["has-child"]},
        )
        assert r["status"] == "warn"
        assert r["shared"] == []

    def test_parent_auto_promote_real_workflow_is_ok(self):
        """The real Parent auto-promote system workflow must lint as closed-loop."""
        from conditions import lint_closed_loop

        r = lint_closed_loop(
            {
                "all_of": [
                    {"kind": "has_children"},
                    {"kind": "section_in", "values": ["Ideas", "Backlog", "WIP"]},
                    {
                        "kind": "children_all_status_in",
                        "value": ["done", "for-review", "bug-fixed"],
                    },
                    {"kind": "automation_mode", "value": "auto"},
                ]
            },
            {"move_section": "For Review"},
        )
        assert r["status"] == "ok"
        assert "section" in r["shared"]

    def test_string_inputs_are_parsed(self):
        from conditions import lint_closed_loop

        r = lint_closed_loop(
            json.dumps({"kind": "status_equals", "value": "for-review"}),
            json.dumps({"set_status": "done"}),
        )
        assert r["status"] == "ok"
        assert "status" in r["shared"]

    def test_invalid_json_strings_render_as_manual_or_empty(self):
        from conditions import lint_closed_loop

        # Garbage trigger → manual (treated as null)
        r = lint_closed_loop("{not json", {"set_status": "done"})
        assert r["status"] == "manual"


# ---------------------------------------------------------------------------
# New on_success actions — round-trip through _apply_on_success
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_db(tmp_path):
    """File-backed sqlite DB with the schema + one ticket. Yields a callable
    factory that opens a new connection each call (so _apply_on_success's
    own conn.close() doesn't kill our test connection).
    """
    import sqlite3

    from db import init_db

    db_path = tmp_path / "test.db"

    def open_conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    setup = open_conn()
    init_db(setup)
    setup.execute(
        "INSERT INTO tickets (id, project_id, title, priority, status, section, "
        "description, sort_order, draft, is_container) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "B-99",
            "test-proj",
            "Test ticket",
            "medium",
            "in-progress",
            "WIP",
            "Test description",
            0,
            0,
            0,
        ),
    )
    setup.commit()
    setup.close()

    yield open_conn  # callers use fresh_db() to get a query connection


class TestNewActions:
    def test_set_priority_updates_ticket(self, fresh_db):
        from actions import ActorContext
        from runners import AgentRunner

        AgentRunner._apply_on_success(
            workflow_meta={"on_success": {"set_priority": "high"}},
            project_id="test-proj",
            ticket_id="B-99",
            actor=ActorContext.system(),
            conn_factory=fresh_db,
        )
        c = fresh_db()
        row = c.execute("SELECT priority FROM tickets WHERE id = 'B-99'").fetchone()
        c.close()
        assert row["priority"] == "high"

    def test_set_is_container_flips_flag(self, fresh_db):
        from actions import ActorContext
        from runners import AgentRunner

        AgentRunner._apply_on_success(
            workflow_meta={"on_success": {"set_is_container": 1}},
            project_id="test-proj",
            ticket_id="B-99",
            actor=ActorContext.system(),
            conn_factory=fresh_db,
        )
        c = fresh_db()
        row = c.execute("SELECT is_container FROM tickets WHERE id = 'B-99'").fetchone()
        c.close()
        assert row["is_container"] == 1
        # And flips back
        AgentRunner._apply_on_success(
            workflow_meta={"on_success": {"set_is_container": 0}},
            project_id="test-proj",
            ticket_id="B-99",
            actor=ActorContext.system(),
            conn_factory=fresh_db,
        )
        c = fresh_db()
        row = c.execute("SELECT is_container FROM tickets WHERE id = 'B-99'").fetchone()
        c.close()
        assert row["is_container"] == 0

    def test_set_automation_mode_writes_subject(self, fresh_db):
        from actions import ActorContext
        from runners import AgentRunner

        AgentRunner._apply_on_success(
            workflow_meta={"on_success": {"set_automation_mode": "auto"}},
            project_id="test-proj",
            ticket_id="B-99",
            actor=ActorContext.system(),
            conn_factory=fresh_db,
        )
        c = fresh_db()
        row = c.execute(
            "SELECT automation_mode FROM automation_subjects "
            "WHERE project_id = ? AND subject_id = ?",
            ("test-proj", "B-99"),
        ).fetchone()
        c.close()
        assert row is not None
        assert row["automation_mode"] == "auto"

    def test_clear_readiness_flag_removes_row(self, fresh_db):
        from actions import ActorContext
        from runners import AgentRunner

        # Pre-seed a flag row
        seed = fresh_db()
        seed.execute(
            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content, set_by) "
            "VALUES (?, ?, ?, ?, ?)",
            ("B-99", "test-proj", "reviewed", "old learnings", "human"),
        )
        seed.commit()
        seed.close()
        AgentRunner._apply_on_success(
            workflow_meta={"on_success": {"clear_readiness_flag": "L"}},
            project_id="test-proj",
            ticket_id="B-99",
            actor=ActorContext.system(),
            conn_factory=fresh_db,
        )
        c = fresh_db()
        row = c.execute(
            "SELECT 1 FROM readiness_flags "
            "WHERE ticket_id = 'B-99' AND project_id = 'test-proj' AND flag = 'reviewed'"
        ).fetchone()
        c.close()
        assert row is None
