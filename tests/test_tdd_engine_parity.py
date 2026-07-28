"""TDD parity test: legacy _ticket_eligibility vs. DB-workflow 'Backlog → WIP' trigger.

Phase 2 spec requirement: for the 'Backlog → WIP' default system workflow,
evaluate_trigger and _ticket_eligibility must agree on eligible/not-eligible
for every fixture case.

Pure logic, no server, no Playwright, no subprocess.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from actions import (
    ActorContext,
    _ticket_eligibility,  # type: ignore[attr-defined]
    set_automation_mode,
)
from conditions import build_subject_context, evaluate_trigger
from db import init_db
from workflows_seed import DEFAULT_WORKFLOWS

# ---------------------------------------------------------------------------
# The "Backlog → WIP" trigger JSON — lifted from DEFAULT_WORKFLOWS.
# ---------------------------------------------------------------------------

_BACKLOG_TO_WIP_WF = next(
    wf for wf in DEFAULT_WORKFLOWS if wf["name"] == "Backlog → WIP"
)
_BACKLOG_TO_WIP_TRIGGER = _BACKLOG_TO_WIP_WF["trigger_json"]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """In-memory DB with full schema."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c


def _add_ticket(
    conn,
    tid="B-1",
    section="Backlog",
    status="specified",
    description="A real description.",
    project_id="p",
    draft=0,
    archived=0,
    no_test_required=0,
    no_test_required_note="",
):
    conn.execute(
        "INSERT INTO tickets "
        "(id, project_id, title, section, status, description, draft, archived, "
        " no_test_required, no_test_required_note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tid,
            project_id,
            f"Title {tid}",
            section,
            status,
            description,
            draft,
            archived,
            no_test_required,
            no_test_required_note,
        ),
    )


def _add_criteria(conn, tid="B-1", project_id="p"):
    conn.execute(
        "INSERT INTO acceptance_criteria (ticket_id, project_id, text) VALUES (?, ?, ?)",
        (tid, project_id, "Does the thing"),
    )


def _set_auto(conn, tid="B-1", project_id="p"):
    set_automation_mode(conn, project_id, "ticket", tid, "auto", ActorContext.human())


def _set_tests_flag(
    conn, tid="B-1", project_id="p", content="pytest tests/test_foo.py"
):
    """Legacy helper kept as a no-op for parity tests.

    Migration 15 collapsed the tests/smoke readiness flags into acceptance
    criteria, and the Backlog → WIP trigger no longer carries tests_covered.
    Existing tests continue to call this for narrative clarity; we keep the
    function as a no-op so the test surface stays stable.
    """
    return


def _declare_lane(conn, tid="B-1", project_id="p", lane="B"):
    """Declare a spec lane — the entry gate into automation.

    Both _ticket_eligibility and the Backlog → WIP trigger now require
    spec_linked, so every fixture that intends *eligible* must declare a lane,
    exactly as a real ticket must before the Kitchen will dispatch it.
    """
    from actions import SpecLink, write_readiness_flag

    change = f"{tid.lower()}-title-{tid.lower()}"
    write_readiness_flag(
        conn,
        project_id,
        tid,
        "spec",
        SpecLink(lane=lane, change=change).render(),
        set_by="test",
    )


def _get_legacy_result(conn, tid="B-1", project_id="p"):
    """Run the legacy _ticket_eligibility against this ticket."""
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE id = ? AND project_id = ?", (tid, project_id)
    ).fetchone()
    assert ticket is not None, f"Ticket {tid!r} not found"
    return _ticket_eligibility(conn, ticket)


def _get_workflow_result(conn, tid="B-1", project_id="p"):
    """Evaluate the Backlog → WIP trigger against this ticket."""
    try:
        ctx = build_subject_context(conn, project_id, tid)
    except ValueError:
        # Ticket not found — both paths ineligible.
        return (False, ["ticket not found"])
    passed, reasons = evaluate_trigger(_BACKLOG_TO_WIP_TRIGGER, ctx)
    return (passed, reasons)


def _assert_parity(conn, tid="B-1", project_id="p"):
    """Assert that legacy and workflow results agree on eligible/not-eligible."""
    legacy = _get_legacy_result(conn, tid, project_id)
    wf_passed, wf_reasons = _get_workflow_result(conn, tid, project_id)
    assert legacy.eligible == wf_passed, (
        f"PARITY MISMATCH for {tid!r}: "
        f"legacy eligible={legacy.eligible} (reasons={legacy.reasons}) "
        f"vs workflow passed={wf_passed} (reasons={wf_reasons})"
    )
    return legacy, wf_passed, wf_reasons


# ---------------------------------------------------------------------------
# Parity matrix — one test class per fixture case
# ---------------------------------------------------------------------------


class TestParityEligible:
    """Fully eligible — all conditions pass."""

    def test_eligible_ticket_both_pass(self, conn):
        _add_ticket(conn)
        _add_criteria(conn)
        _set_auto(conn)
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is True
        assert wf_passed is True


class TestParityMissingDescription:
    """Missing description — both should block."""

    def test_empty_description_both_block(self, conn):
        _add_ticket(conn, description="")
        _add_criteria(conn)
        _set_auto(conn)
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is False
        assert wf_passed is False


class TestParityZeroCriteria:
    """No acceptance criteria — both should block."""

    def test_no_criteria_both_block(self, conn):
        _add_ticket(conn)
        # Intentionally NOT adding criteria.
        _set_auto(conn)
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is False
        assert wf_passed is False


class TestParityBlockedDep:
    """Unmet dependency (dep not done) — both should block."""

    def test_blocked_dep_both_block(self, conn):
        _add_ticket(conn, tid="B-1")
        _add_ticket(conn, tid="B-2", section="WIP", status="in-progress")
        _add_criteria(conn, tid="B-1")
        _set_auto(conn, tid="B-1")
        _set_tests_flag(conn, tid="B-1")
        conn.execute("INSERT INTO depends VALUES ('B-1', 'p', 'B-2')")
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is False
        assert wf_passed is False


class TestParityNoTestsCovered:
    """After migration 15 the seeded gate is criteria-led — neither path
    blocks on a missing test flag. We keep these cases to show that the two
    paths still agree when no test coverage signal is present.
    """

    def test_no_tests_both_pass(self, conn):
        _add_ticket(conn)
        _add_criteria(conn)
        _set_auto(conn)
        _declare_lane(conn)
        # Intentionally NOT setting any test coverage.
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is True
        assert wf_passed is True

    def test_empty_tests_flag_both_pass(self, conn):
        _add_ticket(conn)
        _add_criteria(conn)
        _set_auto(conn)
        _set_tests_flag(conn, content="   ")  # no-op after migration 15
        _declare_lane(conn)
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is True
        assert wf_passed is True


class TestParityNoSpecLane:
    """No declared spec lane — the entry gate into automation. Both block:
    the Kitchen must not dispatch an implementing agent on free text alone."""

    def test_undeclared_lane_both_block(self, conn):
        _add_ticket(conn)
        _add_criteria(conn)
        _set_auto(conn)
        # Intentionally NOT declaring a lane.
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is False
        assert wf_passed is False
        assert any("spec lane" in r for r in legacy.reasons)

    def test_lane_c_no_change_with_reason_is_enough_to_dispatch(self, conn):
        """A justified lane-C declaration satisfies the entry gate — the lane
        question is 'has intent been declared', not 'is there a delta'."""
        from actions import NO_CHANGE_SENTINEL, SpecLink, write_readiness_flag

        _add_ticket(conn)
        _add_criteria(conn)
        _set_auto(conn)
        write_readiness_flag(
            conn,
            "p",
            "B-1",
            "spec",
            SpecLink(
                lane="C", change=NO_CHANGE_SENTINEL, note="dep bump only"
            ).render(),
            set_by="test",
        )
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is True
        assert wf_passed is True


class TestParityActiveRun:
    """Active run exists — both should block."""

    def test_active_run_both_block(self, conn):
        _add_ticket(conn)
        _add_criteria(conn)
        _set_auto(conn)
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.execute(
            "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, triggered_by) "
            "VALUES ('p', 'ticket', 'B-1', 'agent', 'running', 'human')"
        )
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is False
        assert wf_passed is False


class TestParityManualMode:
    """Manual automation mode — both should block."""

    def test_manual_mode_both_block(self, conn):
        _add_ticket(conn)
        _add_criteria(conn)
        # mode stays 'manual' (default — no automation_subjects row).
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is False
        assert wf_passed is False


class TestParityPausedMode:
    """Paused automation mode — both should block."""

    def test_paused_mode_both_block(self, conn):
        _add_ticket(conn)
        _add_criteria(conn)
        set_automation_mode(
            conn,
            "p",
            "ticket",
            "B-1",
            "paused",
            ActorContext.human(),
            pause_reason="waiting on design",
        )
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is False
        assert wf_passed is False


class TestParityWrongSection:
    """Ideas / Done — both should block (workflow uses section_equals=Backlog)."""

    def test_ideas_section_both_block(self, conn):
        _add_ticket(conn, section="Ideas", status="proposed")
        _add_criteria(conn)
        _set_auto(conn)
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is False
        assert wf_passed is False

    def test_done_section_both_block(self, conn):
        _add_ticket(conn, section="Done", status="done")
        _add_criteria(conn)
        _set_auto(conn)
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is False
        assert wf_passed is False


class TestParityDraft:
    """Draft ticket — legacy blocks; trigger doesn't check draft (dispatch-level filter).

    Known divergence: the 'Backlog → WIP' trigger does NOT include a draft-check
    condition.  Draft tickets are excluded at the dispatch query level
    (_dispatch_via_workflows filters `draft=0`), not by the trigger expression.
    So raw `evaluate_trigger` on a draft ticket can return True, while legacy
    `_ticket_eligibility` returns False.

    We confirm:
    1. Legacy correctly blocks draft tickets.
    2. The dispatch path (tested in test_tdd_engine_workflows.py) filters them out.
    """

    def test_draft_legacy_blocks(self, conn):
        _add_ticket(conn, draft=1)
        _add_criteria(conn)
        _set_auto(conn)
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        legacy = _get_legacy_result(conn)
        assert legacy.eligible is False
        assert any("draft" in r for r in legacy.reasons)

    def test_draft_trigger_passes_raw(self, conn):
        """Raw trigger evaluation (without dispatch pre-filter) passes for a draft ticket.
        This is the documented, expected divergence between the two paths."""
        _add_ticket(conn, draft=1)
        _add_criteria(conn)
        _set_auto(conn)
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        wf_passed, _ = _get_workflow_result(conn)
        # The trigger itself does not check draft — this is expected (filter is at dispatch layer).
        assert wf_passed is True


class TestParityArchived:
    """Archived ticket — legacy blocks; trigger doesn't check archived (dispatch-level filter).

    Same documented divergence as draft: `_dispatch_via_workflows` filters
    `archived=0` in the subjects query; the trigger expression itself doesn't
    include an archived check.
    """

    def test_archived_legacy_blocks(self, conn):
        _add_ticket(conn, archived=1)
        _add_criteria(conn)
        _set_auto(conn)
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        legacy = _get_legacy_result(conn)
        assert legacy.eligible is False
        assert any("archived" in r for r in legacy.reasons)

    def test_archived_trigger_passes_raw(self, conn):
        """Raw trigger evaluation (without dispatch pre-filter) passes for an archived ticket.
        This is the documented, expected divergence between the two paths."""
        _add_ticket(conn, archived=1)
        _add_criteria(conn)
        _set_auto(conn)
        _set_tests_flag(conn)
        _declare_lane(conn)
        conn.commit()

        wf_passed, _ = _get_workflow_result(conn)
        # The trigger itself does not check archived — filter is at dispatch layer.
        assert wf_passed is True


class TestParityDepDone:
    """Done dependency — both should allow (deps_clear passes)."""

    def test_dep_done_both_pass(self, conn):
        _add_ticket(conn, tid="B-1")
        _add_ticket(conn, tid="B-2", section="Done", status="done")
        _add_criteria(conn, tid="B-1")
        _set_auto(conn, tid="B-1")
        _set_tests_flag(conn, tid="B-1")
        _declare_lane(conn, tid="B-1")
        conn.execute("INSERT INTO depends VALUES ('B-1', 'p', 'B-2')")
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is True
        assert wf_passed is True


class TestParityNoTestRequiredBypass:
    """no_test_required is now an opt-in tests_covered predicate.

    After migration 15 the seeded Backlog → WIP gate no longer evaluates
    tests_covered, so no_test_required has no effect on the default eligibility
    paths. We keep this case to confirm both paths still agree (eligible=True)
    when the field is set.
    """

    def test_no_test_required_both_pass(self, conn):
        _add_ticket(conn, no_test_required=1, no_test_required_note="docs-only change")
        _add_criteria(conn)
        _set_auto(conn)
        _declare_lane(conn)
        conn.commit()

        legacy, wf_passed, _ = _assert_parity(conn)
        assert legacy.eligible is True
        assert wf_passed is True


# ---------------------------------------------------------------------------
# Parity summary table — parametrized for quick at-a-glance coverage
# ---------------------------------------------------------------------------

PARITY_CASES = [
    # (name, setup_fn, expected_eligible)
    ("fully_eligible", None, True),  # baseline setup in parametrize body
]


class TestParityCoverage:
    """Meta-test: asserts the trigger has all expected conditions."""

    def test_backlog_to_wip_trigger_has_all_expected_conditions(self):
        trigger = _BACKLOG_TO_WIP_TRIGGER
        assert "all_of" in trigger
        kinds = {c["kind"] for c in trigger["all_of"]}
        expected = {
            "section_equals",
            "automation_mode",
            "has_field",
            "criteria_count_gte",
            "deps_clear",
            "no_active_run",
        }
        assert expected <= kinds, f"Missing conditions: {expected - kinds}"
        # tests_covered was retired from the seeded gate (migration 15) —
        # criteria are now the bar. Users can still add it to bespoke workflows.
        assert "tests_covered" not in kinds

    def test_backlog_to_wip_trigger_section_is_backlog(self):
        trigger = _BACKLOG_TO_WIP_TRIGGER
        sec_cond = next(c for c in trigger["all_of"] if c["kind"] == "section_equals")
        assert sec_cond["value"] == "Backlog"

    def test_backlog_to_wip_trigger_automation_mode_is_auto(self):
        trigger = _BACKLOG_TO_WIP_TRIGGER
        mode_cond = next(c for c in trigger["all_of"] if c["kind"] == "automation_mode")
        assert mode_cond["value"] == "auto"
