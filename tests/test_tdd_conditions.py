"""TDD tests for conditions.py — condition catalog, evaluate_trigger, build_subject_context.

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
from conditions import (
    CONDITION_CATALOG,
    evaluate_condition,
    evaluate_trigger,
    build_subject_context,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """In-memory DB with full schema (all migrations)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c


def _make_ticket(conn, tid="B-1", section="Backlog", status="specified",
                  description="Has description", project_id="p",
                  priority="medium", parent=None, no_test_required=0,
                  no_test_required_note="", commit_hash=""):
    conn.execute(
        "INSERT INTO tickets "
        "(id, project_id, title, section, status, description, priority, parent, "
        " no_test_required, no_test_required_note, commit_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, project_id, f"Title {tid}", section, status, description,
         priority, parent, no_test_required, no_test_required_note, commit_hash),
    )


def _add_criteria(conn, tid="B-1", project_id="p", count=1):
    for i in range(count):
        conn.execute(
            "INSERT INTO acceptance_criteria (ticket_id, project_id, text) VALUES (?, ?, ?)",
            (tid, project_id, f"criterion {i+1}"),
        )


def _make_ctx(conn, tid="B-1", project_id="p", active_run=False):
    ticket_row = conn.execute(
        "SELECT * FROM tickets WHERE id = ? AND project_id = ?", (tid, project_id)
    ).fetchone()
    assert ticket_row is not None, f"Ticket {tid!r} not found"
    subj = conn.execute(
        "SELECT * FROM automation_subjects WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ?",
        (project_id, tid),
    ).fetchone()
    return {
        "ticket": dict(ticket_row),
        "ticket_row": ticket_row,
        "automation_subject": dict(subj) if subj else None,
        "project_id": project_id,
        "db": conn,
        "active_run": active_run,
    }


def _set_automation_mode(conn, tid="B-1", project_id="p", mode="auto"):
    conn.execute(
        """
        INSERT INTO automation_subjects (project_id, subject_type, subject_id, automation_mode)
        VALUES (?, 'ticket', ?, ?)
        ON CONFLICT (project_id, subject_type, subject_id)
        DO UPDATE SET automation_mode = excluded.automation_mode
        """,
        (project_id, tid, mode),
    )


# ---------------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------------

class TestCatalogCompleteness:
    EXPECTED_KINDS = {
        "section_equals", "section_in", "status_equals", "automation_mode",
        "has_field", "criteria_count_gte", "flag_set", "deps_clear",
        "tests_covered", "no_active_run", "tag_includes", "priority_at_least",
        "parent_done",
        # Phase A — children + parent helpers for system workflows
        "children_have_open_bugs", "children_no_open_bugs",
        "children_all_status_in", "children_any_status_in",
        "has_children", "parent_section_not_in",
    }

    def test_all_expected_kinds_present(self):
        assert self.EXPECTED_KINDS <= set(CONDITION_CATALOG.keys())

    def test_each_entry_has_evaluator(self):
        for kind, entry in CONDITION_CATALOG.items():
            assert callable(entry["evaluator"]), f"{kind!r} evaluator is not callable"

    def test_each_entry_has_label(self):
        for kind, entry in CONDITION_CATALOG.items():
            assert isinstance(entry.get("label"), str), f"{kind!r} missing label"


# ---------------------------------------------------------------------------
# Individual condition kinds
# ---------------------------------------------------------------------------

class TestSectionEquals:
    def test_pass(self, conn):
        _make_ticket(conn, section="Backlog")
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "section_equals", "value": "Backlog"}, ctx)
        assert ok
        assert "Backlog" in reason

    def test_fail(self, conn):
        _make_ticket(conn, section="WIP")
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "section_equals", "value": "Backlog"}, ctx)
        assert not ok
        assert "WIP" in reason


class TestSectionIn:
    def test_pass(self, conn):
        _make_ticket(conn, section="WIP")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "section_in", "values": ["Backlog", "WIP"]}, ctx)
        assert ok

    def test_fail(self, conn):
        _make_ticket(conn, section="Ideas")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "section_in", "values": ["Backlog", "WIP"]}, ctx)
        assert not ok


class TestStatusEquals:
    def test_pass(self, conn):
        _make_ticket(conn, status="in-progress")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "status_equals", "value": "in-progress"}, ctx)
        assert ok

    def test_fail(self, conn):
        _make_ticket(conn, status="proposed")
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "status_equals", "value": "in-progress"}, ctx)
        assert not ok
        assert "proposed" in reason


class TestAutomationMode:
    def test_pass_when_auto(self, conn):
        _make_ticket(conn)
        _set_automation_mode(conn, mode="auto")
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "automation_mode", "value": "auto"}, ctx)
        assert ok
        assert "auto" in reason

    def test_fail_when_manual(self, conn):
        _make_ticket(conn)
        # No automation_subjects row → defaults to 'manual'
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "automation_mode", "value": "auto"}, ctx)
        assert not ok
        assert "manual" in reason

    def test_pass_when_manual_wanted(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "automation_mode", "value": "manual"}, ctx)
        assert ok


class TestHasField:
    def test_pass_when_description_set(self, conn):
        _make_ticket(conn, description="A real description")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "has_field", "field": "description"}, ctx)
        assert ok

    def test_fail_when_description_empty(self, conn):
        _make_ticket(conn, description="")
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "has_field", "field": "description"}, ctx)
        assert not ok
        assert "empty" in reason

    def test_fail_when_description_whitespace(self, conn):
        _make_ticket(conn, description="   ")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "has_field", "field": "description"}, ctx)
        assert not ok


class TestCriteriaCountGte:
    def test_pass_when_gte(self, conn):
        _make_ticket(conn)
        _add_criteria(conn, count=2)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "criteria_count_gte", "value": 2}, ctx)
        assert ok
        assert "2" in reason

    def test_pass_exact(self, conn):
        _make_ticket(conn)
        _add_criteria(conn, count=1)
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "criteria_count_gte", "value": 1}, ctx)
        assert ok

    def test_fail_when_less(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "criteria_count_gte", "value": 1}, ctx)
        assert not ok
        assert "0" in reason


class TestFlagSet:
    def test_pass_when_flag_with_content(self, conn):
        # S/T were collapsed into acceptance_criteria (migration 15). The L
        # (Learnings) pane is the surviving flag-style readiness slot.
        _make_ticket(conn)
        conn.execute(
            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content) VALUES (?, ?, ?, ?)",
            ("B-1", "p", "reviewed", "captured what we learned"),
        )
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "flag_set", "flag": "L"}, ctx)
        assert ok

    def test_fail_when_flag_absent(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "flag_set", "flag": "L"}, ctx)
        assert not ok
        assert "not set" in reason

    def test_fail_when_flag_empty_content(self, conn):
        _make_ticket(conn)
        conn.execute(
            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content) VALUES (?, ?, ?, ?)",
            ("B-1", "p", "reviewed", ""),
        )
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "flag_set", "flag": "L"}, ctx)
        assert not ok


class TestDepsClear:
    def test_pass_when_no_deps(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "deps_clear"}, ctx)
        assert ok
        assert "done" in reason.lower()

    def test_pass_when_dep_done(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="B-2", section="Done", status="done")
        conn.execute(
            "INSERT INTO depends (ticket_id, project_id, depends_on_id) VALUES (?, ?, ?)",
            ("B-1", "p", "B-2"),
        )
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "deps_clear"}, ctx)
        assert ok

    def test_fail_when_dep_in_progress(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="B-2", section="WIP", status="in-progress")
        conn.execute(
            "INSERT INTO depends (ticket_id, project_id, depends_on_id) VALUES (?, ?, ?)",
            ("B-1", "p", "B-2"),
        )
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "deps_clear"}, ctx)
        assert not ok
        assert "B-2" in reason


class TestTestsCovered:
    """tests_covered is now an opt-in predicate (migration 15).

    The legacy "tests readiness flag has content" path is gone — content lives
    in acceptance criteria. Surviving paths: linked journey or no_test_required.
    """

    def test_tests_readiness_flag_no_longer_satisfies(self, conn):
        # A bare 'tests' readiness row no longer satisfies the predicate.
        _make_ticket(conn)
        conn.execute(
            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content) VALUES (?, ?, ?, ?)",
            ("B-1", "p", "tests", "unit + integration"),
        )
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "tests_covered"}, ctx)
        assert not ok

    def test_pass_via_no_test_required(self, conn):
        _make_ticket(conn, no_test_required=1, no_test_required_note="CLI-only tool")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "tests_covered"}, ctx)
        assert ok

    def test_fail_when_no_coverage(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "tests_covered"}, ctx)
        assert not ok
        assert reason  # has a reason


class TestNoActiveRun:
    def test_pass_when_no_run(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn, active_run=False)
        ok, reason = evaluate_condition({"kind": "no_active_run"}, ctx)
        assert ok
        assert "no active" in reason

    def test_fail_when_active_run(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn, active_run=True)
        ok, reason = evaluate_condition({"kind": "no_active_run"}, ctx)
        assert not ok
        assert "exists" in reason


class TestTagIncludes:
    def test_pass_when_tag_present(self, conn):
        _make_ticket(conn)
        conn.execute(
            "INSERT INTO ticket_tags (ticket_id, project_id, tag) VALUES (?, ?, ?)",
            ("B-1", "p", "ux"),
        )
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "tag_includes", "value": "ux"}, ctx)
        assert ok

    def test_fail_when_tag_absent(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "tag_includes", "value": "ux"}, ctx)
        assert not ok
        assert "ux" in reason


class TestPriorityAtLeast:
    def test_pass_equal(self, conn):
        _make_ticket(conn, priority="high")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "priority_at_least", "value": "high"}, ctx)
        assert ok

    def test_pass_above(self, conn):
        _make_ticket(conn, priority="critical")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "priority_at_least", "value": "high"}, ctx)
        assert ok

    def test_fail_below(self, conn):
        _make_ticket(conn, priority="low")
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "priority_at_least", "value": "high"}, ctx)
        assert not ok
        assert "low" in reason


class TestParentDone:
    def test_pass_when_no_parent(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "parent_done"}, ctx)
        assert ok
        assert "no parent" in reason

    def test_pass_when_parent_done(self, conn):
        _make_ticket(conn, tid="B-1", parent="B-2")
        _make_ticket(conn, tid="B-2", section="Done")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "parent_done"}, ctx)
        assert ok

    def test_fail_when_parent_not_done(self, conn):
        _make_ticket(conn, tid="B-1", parent="B-2")
        _make_ticket(conn, tid="B-2", section="WIP")
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "parent_done"}, ctx)
        assert not ok
        assert "B-2" in reason


class TestChildrenHaveOpenBugs:
    def test_false_when_no_children(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "children_have_open_bugs"}, ctx)
        assert not ok
        assert "no open" in reason

    def test_true_when_open_bug_child(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="BUG-1", parent="B-1", status="bug")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "children_have_open_bugs"}, ctx)
        assert ok

    def test_false_when_all_children_terminal(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="BUG-1", parent="B-1", status="done")
        _make_ticket(conn, tid="BUG-2", parent="B-1", status="bug-fixed")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "children_have_open_bugs"}, ctx)
        assert not ok


class TestChildrenNoOpenBugs:
    def test_true_when_no_children(self, conn):
        """Vacuously true: no children → no open bugs."""
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "children_no_open_bugs"}, ctx)
        assert ok

    def test_false_when_open_bug_child(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="BUG-1", parent="B-1", status="bug")
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "children_no_open_bugs"}, ctx)
        assert not ok
        assert "open" in reason

    def test_true_when_all_children_terminal(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="BUG-1", parent="B-1", status="done")
        _make_ticket(conn, tid="BUG-2", parent="B-1", status="for-review")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "children_no_open_bugs"}, ctx)
        assert ok


class TestChildrenAllStatusIn:
    def test_vacuous_true_no_children(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition(
            {"kind": "children_all_status_in", "value": ["done"]}, ctx,
        )
        assert ok
        assert "no children" in reason

    def test_true_all_children_match(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="B-2", parent="B-1", status="done")
        _make_ticket(conn, tid="B-3", parent="B-1", status="for-review")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition(
            {"kind": "children_all_status_in", "value": ["done", "for-review", "bug-fixed"]},
            ctx,
        )
        assert ok

    def test_false_one_child_outside(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="B-2", parent="B-1", status="done")
        _make_ticket(conn, tid="B-3", parent="B-1", status="in-progress")
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition(
            {"kind": "children_all_status_in", "value": ["done", "for-review"]},
            ctx,
        )
        assert not ok
        assert "in-progress" in reason


class TestChildrenAnyStatusIn:
    def test_false_no_children(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition(
            {"kind": "children_any_status_in", "value": ["done"]}, ctx,
        )
        assert not ok
        assert "no children" in reason

    def test_true_one_child_matches(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="B-2", parent="B-1", status="done")
        _make_ticket(conn, tid="B-3", parent="B-1", status="in-progress")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition(
            {"kind": "children_any_status_in", "value": ["done"]}, ctx,
        )
        assert ok

    def test_false_no_match(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="B-2", parent="B-1", status="in-progress")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition(
            {"kind": "children_any_status_in", "value": ["done", "for-review"]},
            ctx,
        )
        assert not ok


class TestHasChildren:
    def test_false_when_no_children(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "has_children"}, ctx)
        assert not ok

    def test_true_when_one_child(self, conn):
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="B-2", parent="B-1")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition({"kind": "has_children"}, ctx)
        assert ok


class TestParentSectionNotIn:
    def test_vacuous_true_no_parent(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition(
            {"kind": "parent_section_not_in", "value": ["Done"]}, ctx,
        )
        assert ok

    def test_true_when_parent_in_other_section(self, conn):
        _make_ticket(conn, tid="B-1", parent="B-2")
        _make_ticket(conn, tid="B-2", section="Backlog")
        ctx = _make_ctx(conn)
        ok, _ = evaluate_condition(
            {"kind": "parent_section_not_in", "value": ["Done", "Won't Do", "For Review"]},
            ctx,
        )
        assert ok

    def test_false_when_parent_in_excluded_section(self, conn):
        _make_ticket(conn, tid="B-1", parent="B-2")
        _make_ticket(conn, tid="B-2", section="For Review")
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition(
            {"kind": "parent_section_not_in", "value": ["Done", "For Review"]},
            ctx,
        )
        assert not ok
        assert "For Review" in reason


class TestUnknownCondition:
    def test_fail_unknown_kind(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"kind": "nonexistent_condition"}, ctx)
        assert not ok
        assert "unknown" in reason

    def test_fail_missing_kind(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, reason = evaluate_condition({"section": "Backlog"}, ctx)
        assert not ok
        assert "kind" in reason


# ---------------------------------------------------------------------------
# evaluate_trigger
# ---------------------------------------------------------------------------

class TestEvaluateTrigger:
    def test_none_trigger_passes(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, failures = evaluate_trigger(None, ctx)
        assert ok
        assert failures == []

    def test_empty_dict_passes(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, failures = evaluate_trigger({}, ctx)
        assert ok

    def test_all_of_all_pass(self, conn):
        _make_ticket(conn, section="Backlog", description="desc")
        ctx = _make_ctx(conn)
        trigger = {
            "all_of": [
                {"kind": "section_equals", "value": "Backlog"},
                {"kind": "has_field", "field": "description"},
            ]
        }
        ok, failures = evaluate_trigger(trigger, ctx)
        assert ok
        assert failures == []

    def test_all_of_one_fail(self, conn):
        _make_ticket(conn, section="WIP", description="desc")
        ctx = _make_ctx(conn)
        trigger = {
            "all_of": [
                {"kind": "section_equals", "value": "Backlog"},
                {"kind": "has_field", "field": "description"},
            ]
        }
        ok, failures = evaluate_trigger(trigger, ctx)
        assert not ok
        assert len(failures) == 1

    def test_all_of_both_fail(self, conn):
        _make_ticket(conn, section="WIP", description="")
        ctx = _make_ctx(conn)
        trigger = {
            "all_of": [
                {"kind": "section_equals", "value": "Backlog"},
                {"kind": "has_field", "field": "description"},
            ]
        }
        ok, failures = evaluate_trigger(trigger, ctx)
        assert not ok
        assert len(failures) == 2

    def test_any_of_first_passes(self, conn):
        _make_ticket(conn, section="Backlog")
        ctx = _make_ctx(conn)
        trigger = {
            "any_of": [
                {"kind": "section_equals", "value": "Backlog"},
                {"kind": "section_equals", "value": "WIP"},
            ]
        }
        ok, _ = evaluate_trigger(trigger, ctx)
        assert ok

    def test_any_of_second_passes(self, conn):
        _make_ticket(conn, section="WIP")
        ctx = _make_ctx(conn)
        trigger = {
            "any_of": [
                {"kind": "section_equals", "value": "Backlog"},
                {"kind": "section_equals", "value": "WIP"},
            ]
        }
        ok, _ = evaluate_trigger(trigger, ctx)
        assert ok

    def test_any_of_none_pass(self, conn):
        _make_ticket(conn, section="Ideas")
        ctx = _make_ctx(conn)
        trigger = {
            "any_of": [
                {"kind": "section_equals", "value": "Backlog"},
                {"kind": "section_equals", "value": "WIP"},
            ]
        }
        ok, failures = evaluate_trigger(trigger, ctx)
        assert not ok
        assert len(failures) == 2

    def test_nested_all_of_in_any_of(self, conn):
        _make_ticket(conn, section="Backlog", description="desc", status="specified")
        ctx = _make_ctx(conn)
        trigger = {
            "any_of": [
                {
                    "all_of": [
                        {"kind": "section_equals", "value": "Backlog"},
                        {"kind": "has_field", "field": "description"},
                    ]
                },
                {"kind": "section_equals", "value": "WIP"},
            ]
        }
        ok, _ = evaluate_trigger(trigger, ctx)
        assert ok

    def test_json_string_trigger(self, conn):
        _make_ticket(conn, section="Backlog")
        ctx = _make_ctx(conn)
        trigger_str = json.dumps({"all_of": [{"kind": "section_equals", "value": "Backlog"}]})
        ok, _ = evaluate_trigger(trigger_str, ctx)
        assert ok

    def test_invalid_json_string_fails(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        ok, failures = evaluate_trigger("not-json", ctx)
        assert not ok
        assert failures


# ---------------------------------------------------------------------------
# Parity tests: conditions produce same results as actions._deps_clear / _tests_covered
# ---------------------------------------------------------------------------

class TestParityWithActions:
    """deps_clear and tests_covered evaluators must match actions.py exactly."""

    def test_deps_clear_parity_no_deps(self, conn):
        from actions import _deps_clear
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        cond_ok, _ = evaluate_condition({"kind": "deps_clear"}, ctx)
        actions_ok, _ = _deps_clear(conn, "p", "B-1")
        assert cond_ok == actions_ok

    def test_deps_clear_parity_with_blocking_dep(self, conn):
        from actions import _deps_clear
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="B-2", section="WIP", status="in-progress")
        conn.execute(
            "INSERT INTO depends (ticket_id, project_id, depends_on_id) VALUES (?, ?, ?)",
            ("B-1", "p", "B-2"),
        )
        ctx = _make_ctx(conn)
        cond_ok, _ = evaluate_condition({"kind": "deps_clear"}, ctx)
        actions_ok, _ = _deps_clear(conn, "p", "B-1")
        assert cond_ok == actions_ok

    def test_deps_clear_parity_dep_done(self, conn):
        from actions import _deps_clear
        _make_ticket(conn, tid="B-1")
        _make_ticket(conn, tid="B-2", section="Done", status="done")
        conn.execute(
            "INSERT INTO depends (ticket_id, project_id, depends_on_id) VALUES (?, ?, ?)",
            ("B-1", "p", "B-2"),
        )
        ctx = _make_ctx(conn)
        cond_ok, _ = evaluate_condition({"kind": "deps_clear"}, ctx)
        actions_ok, _ = _deps_clear(conn, "p", "B-1")
        assert cond_ok == actions_ok

    def test_tests_covered_parity_no_coverage(self, conn):
        from actions import _tests_covered
        _make_ticket(conn)
        ticket_row = conn.execute(
            "SELECT * FROM tickets WHERE id = 'B-1' AND project_id = 'p'"
        ).fetchone()
        ctx = _make_ctx(conn)
        cond_ok, _ = evaluate_condition({"kind": "tests_covered"}, ctx)
        actions_ok, _ = _tests_covered(conn, ticket_row)
        assert cond_ok == actions_ok

    def test_tests_covered_parity_with_flag(self, conn):
        from actions import _tests_covered
        _make_ticket(conn)
        conn.execute(
            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content) VALUES (?, ?, ?, ?)",
            ("B-1", "p", "tests", "has tests"),
        )
        ticket_row = conn.execute(
            "SELECT * FROM tickets WHERE id = 'B-1' AND project_id = 'p'"
        ).fetchone()
        ctx = _make_ctx(conn)
        cond_ok, _ = evaluate_condition({"kind": "tests_covered"}, ctx)
        actions_ok, _ = _tests_covered(conn, ticket_row)
        assert cond_ok == actions_ok

    def test_tests_covered_parity_no_test_required(self, conn):
        from actions import _tests_covered
        _make_ticket(conn, no_test_required=1, no_test_required_note="CLI-only")
        ticket_row = conn.execute(
            "SELECT * FROM tickets WHERE id = 'B-1' AND project_id = 'p'"
        ).fetchone()
        ctx = _make_ctx(conn)
        cond_ok, _ = evaluate_condition({"kind": "tests_covered"}, ctx)
        actions_ok, _ = _tests_covered(conn, ticket_row)
        assert cond_ok == actions_ok


# ---------------------------------------------------------------------------
# build_subject_context
# ---------------------------------------------------------------------------

class TestBuildSubjectContext:
    def test_returns_expected_keys(self, conn):
        _make_ticket(conn)
        ctx = build_subject_context(conn, "p", "B-1")
        assert "ticket" in ctx
        assert "project_id" in ctx
        assert "db" in ctx
        assert "active_run" in ctx
        assert "automation_subject" in ctx

    def test_ticket_id_matches(self, conn):
        _make_ticket(conn)
        ctx = build_subject_context(conn, "p", "B-1")
        assert ctx["ticket"]["id"] == "B-1"

    def test_active_run_false_by_default(self, conn):
        _make_ticket(conn)
        ctx = build_subject_context(conn, "p", "B-1")
        assert ctx["active_run"] is False

    def test_active_run_true_when_running(self, conn):
        _make_ticket(conn)
        conn.execute(
            "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, triggered_by) "
            "VALUES ('p', 'ticket', 'B-1', 'agent', 'running', 'human')"
        )
        ctx = build_subject_context(conn, "p", "B-1")
        assert ctx["active_run"] is True

    def test_automation_subject_populated(self, conn):
        _make_ticket(conn)
        _set_automation_mode(conn, mode="auto")
        ctx = build_subject_context(conn, "p", "B-1")
        assert ctx["automation_subject"] is not None
        assert ctx["automation_subject"]["automation_mode"] == "auto"

    def test_missing_ticket_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            build_subject_context(conn, "p", "NONEXISTENT")
