"""TDD tests for Kitchen M1a — schema, helpers, eligibility, mode actions,
spine event emission, internal side-effect rule, and module skeletons.

Pure logic. No server, no Playwright. See docs/KITCHEN.md.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from actions import (
    ActorContext,
    accept_ticket,
    eligibility,
    emit_event,
    move_ticket,
    set_automation_mode,
    set_no_test_required,
    update_ticket,
    utcnow_iso,
)
from db import init_db


@pytest.fixture
def conn():
    """In-memory DB with full schema initialised through migration #6."""
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
    with_criteria=True,
    project_id="p",
):
    conn.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, project_id, f"Title for {tid}", section, status, description),
    )
    if with_criteria:
        conn.execute(
            "INSERT INTO acceptance_criteria (ticket_id, project_id, text) VALUES (?, ?, ?)",
            (tid, project_id, "Does the thing"),
        )


def _declare_lane(conn, tid="B-1", project_id="p"):
    """Entry gate into automation: eligibility requires a declared spec lane."""
    conn.execute(
        "INSERT OR REPLACE INTO readiness_flags (ticket_id, project_id, flag, content, set_by) "
        "VALUES (?, ?, 'spec', ?, 'test')",
        (tid, project_id, f"B:{tid.lower()}-test-change"),
    )


# ---------------------------------------------------------------------------
# Migration #6 — schema present, CHECK constraints fire
# ---------------------------------------------------------------------------


class TestMigration:
    def test_new_tables_exist(self, conn):
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"automation_subjects", "runs", "activity_events"} <= names

    def test_no_test_required_columns_added(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tickets)").fetchall()}
        assert "no_test_required" in cols
        assert "no_test_required_note" in cols

    def test_partial_unique_index_present(self, conn):
        idx = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "one_active_run_per_subject" in idx

    def test_check_rejects_bad_automation_mode(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO automation_subjects "
                "(project_id, subject_type, subject_id, automation_mode) "
                "VALUES ('p', 'ticket', 'B-1', 'bogus')"
            )

    def test_check_rejects_bad_subject_type(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO automation_subjects "
                "(project_id, subject_type, subject_id) VALUES ('p', 'banana', 'B-1')"
            )

    def test_check_rejects_bad_run_status(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, triggered_by) "
                "VALUES ('p', 'ticket', 'B-1', 'agent', 'bogus', 'human')"
            )

    def test_check_rejects_bad_triggered_by(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, triggered_by) "
                "VALUES ('p', 'ticket', 'B-1', 'agent', 'queued', 'magic')"
            )

    def test_check_allows_null_retry_kind(self, conn):
        # Round 3 schema fix: NULL must pass. (`IN (NULL, ...)` would have let bad strings through.)
        conn.execute(
            "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, triggered_by, retry_kind) "
            "VALUES ('p', 'ticket', 'B-1', 'agent', 'succeeded', 'human', NULL)"
        )

    def test_check_rejects_bad_retry_kind(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, triggered_by, retry_kind) "
                "VALUES ('p', 'ticket', 'B-2', 'agent', 'succeeded', 'human', 'bogus')"
            )

    def test_check_rejects_bad_actor_type(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO activity_events "
                "(project_id, subject_type, subject_id, actor_type, event_kind, payload_json, occurred_at) "
                "VALUES ('p', 'ticket', 'B-1', 'robot', 'mode_changed', '{}', '2026-04-29T00:00:00Z')"
            )


# ---------------------------------------------------------------------------
# Partial unique index — one active run per subject
# ---------------------------------------------------------------------------


class TestActiveRunUniqueness:
    def _new_active_run(self, conn, status="queued", subject_id="B-1"):
        conn.execute(
            "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, triggered_by) "
            "VALUES ('p', 'ticket', ?, 'agent', ?, 'human')",
            (subject_id, status),
        )

    def test_second_active_run_rejected(self, conn):
        self._new_active_run(conn, "preparing")
        with pytest.raises(sqlite3.IntegrityError):
            self._new_active_run(conn, "queued")

    def test_active_then_terminal_then_active_allowed(self, conn):
        # Start one, finish it, then start another — totally legal (history accumulates).
        self._new_active_run(conn, "preparing")
        conn.execute("UPDATE runs SET status = 'succeeded' WHERE subject_id = 'B-1'")
        self._new_active_run(conn, "queued")  # must not raise
        rows = conn.execute(
            "SELECT status FROM runs WHERE subject_id = 'B-1' ORDER BY id"
        ).fetchall()
        assert [r[0] for r in rows] == ["succeeded", "queued"]

    def test_needs_input_blocks_new_active(self, conn):
        # Per §8: needs_input doesn't consume capacity, but it IS active — same subject can't start another.
        self._new_active_run(conn, "needs_input")
        with pytest.raises(sqlite3.IntegrityError):
            self._new_active_run(conn, "queued")

    def test_different_subjects_unaffected(self, conn):
        self._new_active_run(conn, "running", subject_id="B-1")
        self._new_active_run(
            conn, "running", subject_id="B-2"
        )  # different subject — fine


# ---------------------------------------------------------------------------
# ActorContext + utcnow_iso + emit_event
# ---------------------------------------------------------------------------


class TestActorContext:
    def test_human_factory(self):
        a = ActorContext.human("alice")
        assert a.actor_type == "human"
        assert a.actor_id == "alice"

    def test_human_factory_no_user(self):
        a = ActorContext.human()
        assert a.actor_type == "human"
        assert a.actor_id is None

    def test_agent_factory_coerces_to_str(self):
        a = ActorContext.agent(42)
        assert a.actor_type == "agent"
        assert a.actor_id == "42"

    def test_system_factory(self):
        a = ActorContext.system()
        assert a.actor_type == "system"
        assert a.actor_id is None

    def test_frozen(self):
        a = ActorContext.human()
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            a.actor_type = "agent"  # type: ignore[misc]


class TestUtcNowIso:
    def test_format_includes_offset(self):
        s = utcnow_iso()
        # Must be ISO-8601 with timezone (either +00:00 or Z) so reads are unambiguous.
        assert "T" in s
        assert s.endswith("+00:00") or s.endswith("Z")


class TestEmitEvent:
    def test_writes_one_row(self, conn):
        emit_event(
            conn,
            "p",
            "ticket",
            "B-1",
            "mode_changed",
            {"before": "manual", "after": "auto"},
            ActorContext.human("alice"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT actor_type, actor_id, event_kind, payload_json FROM activity_events"
        ).fetchone()
        assert row["actor_type"] == "human"
        assert row["actor_id"] == "alice"
        assert row["event_kind"] == "mode_changed"
        assert json.loads(row["payload_json"]) == {"before": "manual", "after": "auto"}

    def test_same_transaction_atomicity(self, conn):
        # If we don't commit, the audit row must NOT be visible after rollback.
        # This proves emit_event participates in the caller's transaction
        # rather than opening one of its own.
        emit_event(
            conn,
            "p",
            "ticket",
            "B-1",
            "mode_changed",
            {"before": "manual", "after": "auto"},
            ActorContext.human(),
        )
        conn.rollback()
        n = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
        assert n == 0

    def test_system_actor_id_null(self, conn):
        emit_event(
            conn,
            "p",
            "ticket",
            "B-1",
            "section_change",
            {"before": "WIP", "after": "For Review"},
            ActorContext.system(),
        )
        conn.commit()
        row = conn.execute(
            "SELECT actor_type, actor_id FROM activity_events"
        ).fetchone()
        assert row["actor_type"] == "system"
        assert row["actor_id"] is None


# ---------------------------------------------------------------------------
# Eligibility — each gate
# ---------------------------------------------------------------------------


class TestEligibilityTicket:
    def test_default_mode_blocks(self, conn):
        _add_ticket(conn)
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible
        assert any("automation_mode" in x for x in r.reasons)

    def test_auto_with_criteria_eligible(self, conn):
        """After migration 15 the seeded gate is criteria-led — automation auto
        plus at least one acceptance criterion is enough. Tests are no longer
        a default gate (users can opt in via tests_covered in their workflows)."""
        _add_ticket(conn)
        _declare_lane(conn)
        conn.commit()
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert r.eligible, r.reasons

    def test_no_test_required_satisfies(self, conn):
        """no_test_required has no effect on the default gate now (the seeded
        Backlog → WIP trigger no longer evaluates tests_covered) but a ticket
        marked NTR with criteria is still eligible."""
        _add_ticket(conn)
        _declare_lane(conn)
        conn.commit()
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        set_no_test_required(
            conn, "p", "B-1", True, "Pure docs change", ActorContext.human()
        )
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert r.eligible, r.reasons

    def test_paused_blocks(self, conn):
        _add_ticket(conn)
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(
            conn,
            "p",
            "ticket",
            "B-1",
            "paused",
            ActorContext.human(),
            pause_reason="waiting",
        )
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible

    def test_section_outside_active_range_blocks(self, conn):
        # Ticket in Ideas can't be eligible no matter what.
        _add_ticket(conn, section="Ideas", status="proposed")
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible
        assert any("Ideas" in x or "Backlog/WIP/For Review" in x for x in r.reasons)

    def test_draft_blocks(self, conn):
        _add_ticket(conn)
        conn.commit()
        conn.execute("UPDATE tickets SET draft = 1 WHERE id = 'B-1'")
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible
        assert any("draft" in x for x in r.reasons)

    def test_archived_blocks(self, conn):
        _add_ticket(conn)
        conn.commit()
        conn.execute("UPDATE tickets SET archived = 1 WHERE id = 'B-1'")
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible

    def test_no_description_blocks(self, conn):
        _add_ticket(conn, description="")
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible
        assert any("description" in x for x in r.reasons)

    def test_no_criteria_blocks(self, conn):
        _add_ticket(conn, with_criteria=False)
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible
        assert any("criteria" in x for x in r.reasons)

    def test_unmet_dependency_blocks(self, conn):
        _add_ticket(conn, tid="B-1")
        _add_ticket(conn, tid="B-2", section="WIP", status="in-progress")
        conn.commit()
        conn.execute("INSERT INTO depends VALUES ('B-1', 'p', 'B-2')")
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible
        assert any("B-2" in x for x in r.reasons)

    def test_dep_done_clears(self, conn):
        _add_ticket(conn, tid="B-1")
        _declare_lane(conn, tid="B-1")
        _add_ticket(conn, tid="B-2", section="Done", status="done")
        conn.commit()
        conn.execute("INSERT INTO depends VALUES ('B-1', 'p', 'B-2')")
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert r.eligible, r.reasons

    def test_wontdo_dep_does_not_clear(self, conn):
        # Per docs/KITCHEN.md §7: wontdo does NOT clear deps; must be removed explicitly.
        _add_ticket(conn, tid="B-1")
        _add_ticket(conn, tid="B-2", section="Won't Do", status="wontdo")
        conn.commit()
        conn.execute("INSERT INTO depends VALUES ('B-1', 'p', 'B-2')")
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible

    def test_archived_dep_blocks(self, conn):
        _add_ticket(conn, tid="B-1")
        _add_ticket(conn, tid="B-2", section="Done", status="done")
        conn.commit()
        conn.execute("UPDATE tickets SET archived = 1 WHERE id = 'B-2'")
        conn.execute("INSERT INTO depends VALUES ('B-1', 'p', 'B-2')")
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible
        assert any("archived" in x for x in r.reasons)

    def test_active_run_blocks(self, conn):
        _add_ticket(conn)
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "x", ActorContext.human())
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.execute(
            "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, triggered_by) "
            "VALUES ('p', 'ticket', 'B-1', 'agent', 'running', 'human')"
        )
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible
        assert any("active run" in x for x in r.reasons)

    def test_criteria_alone_satisfies_eligibility(self, conn):
        """After migration 15 the seeded gate is criteria-led; no test flag required."""
        _add_ticket(conn)
        _declare_lane(conn)
        conn.commit()
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert r.eligible, r.reasons

    def test_no_criteria_blocks_eligibility(self, conn):
        """Tickets with no acceptance criteria are not eligible."""
        _add_ticket(conn, with_criteria=False)
        conn.commit()
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        r = eligibility(conn, "p", "ticket", "B-1")
        assert not r.eligible
        assert any("criteria" in x for x in r.reasons)


class TestEligibilityUnknown:
    def test_unknown_subject_type(self, conn):
        r = eligibility(conn, "p", "wat", "X")
        assert not r.eligible
        assert "wat" in r.reasons[0]

    def test_investigation_not_implemented_in_m1a(self, conn):
        r = eligibility(conn, "p", "investigation", "X")
        assert not r.eligible


# ---------------------------------------------------------------------------
# Mode actions
# ---------------------------------------------------------------------------


class TestSetAutomationMode:
    def test_lazy_creates_subject_row(self, conn):
        set_automation_mode(
            conn, "p", "ticket", "B-1", "auto", ActorContext.human("alice")
        )
        conn.commit()
        row = conn.execute("SELECT automation_mode FROM automation_subjects").fetchone()
        assert row["automation_mode"] == "auto"

    def test_invalid_mode_rejected(self, conn):
        with pytest.raises(ValueError):
            set_automation_mode(
                conn, "p", "ticket", "B-1", "bogus", ActorContext.human()
            )

    def test_paused_without_reason_accepted(self, conn):
        # Pause is optional — pause_reason can be omitted entirely.
        set_automation_mode(conn, "p", "ticket", "B-1", "paused", ActorContext.human())
        conn.commit()
        row = conn.execute(
            "SELECT automation_mode, pause_reason FROM automation_subjects"
        ).fetchone()
        assert row["automation_mode"] == "paused"
        assert row["pause_reason"] is None

    def test_paused_with_whitespace_only_reason_normalised_to_null(self, conn):
        # Whitespace-only reason is treated as no reason.
        set_automation_mode(
            conn,
            "p",
            "ticket",
            "B-1",
            "paused",
            ActorContext.human(),
            pause_reason="   ",
        )
        conn.commit()
        row = conn.execute("SELECT pause_reason FROM automation_subjects").fetchone()
        assert row["pause_reason"] is None

    def test_first_set_to_auto_emits_mode_changed(self, conn):
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        e = conn.execute(
            "SELECT event_kind, payload_json FROM activity_events"
        ).fetchone()
        assert e["event_kind"] == "mode_changed"
        assert json.loads(e["payload_json"]) == {"before": "manual", "after": "auto"}

    def test_setting_paused_emits_pause_set(self, conn):
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        set_automation_mode(
            conn,
            "p",
            "ticket",
            "B-1",
            "paused",
            ActorContext.human(),
            pause_reason="waiting on Stripe",
        )
        conn.commit()
        e = conn.execute(
            "SELECT event_kind, payload_json FROM activity_events ORDER BY id DESC"
        ).fetchone()
        assert e["event_kind"] == "pause_set"
        payload = json.loads(e["payload_json"])
        assert payload["reason"] == "waiting on Stripe"
        assert payload["before"] == "auto"
        assert payload["after"] == "paused"

    def test_clearing_paused_emits_pause_cleared(self, conn):
        set_automation_mode(
            conn, "p", "ticket", "B-1", "paused", ActorContext.human(), pause_reason="x"
        )
        conn.commit()
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        e = conn.execute(
            "SELECT event_kind, payload_json FROM activity_events ORDER BY id DESC"
        ).fetchone()
        assert e["event_kind"] == "pause_cleared"
        payload = json.loads(e["payload_json"])
        assert payload["before"] == "paused"
        assert payload["after"] == "auto"
        assert payload["prior_reason"] == "x"

    def test_no_op_emits_nothing(self, conn):
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        n_before = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        n_after = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
        assert n_before == n_after

    def test_clearing_pause_clears_reason_field(self, conn):
        set_automation_mode(
            conn, "p", "ticket", "B-1", "paused", ActorContext.human(), pause_reason="x"
        )
        conn.commit()
        set_automation_mode(conn, "p", "ticket", "B-1", "auto", ActorContext.human())
        conn.commit()
        row = conn.execute(
            "SELECT automation_mode, pause_reason FROM automation_subjects"
        ).fetchone()
        assert row["automation_mode"] == "auto"
        assert row["pause_reason"] is None


class TestSetNoTestRequired:
    def test_enabled_requires_note(self, conn):
        _add_ticket(conn)
        conn.commit()
        with pytest.raises(ValueError):
            set_no_test_required(conn, "p", "B-1", True, "", ActorContext.human())

    def test_enabled_whitespace_note_rejected(self, conn):
        _add_ticket(conn)
        conn.commit()
        with pytest.raises(ValueError):
            set_no_test_required(conn, "p", "B-1", True, "   ", ActorContext.human())

    def test_disabled_clears_note(self, conn):
        _add_ticket(conn)
        conn.commit()
        set_no_test_required(conn, "p", "B-1", True, "rationale", ActorContext.human())
        conn.commit()
        set_no_test_required(conn, "p", "B-1", False, "ignored", ActorContext.human())
        conn.commit()
        row = conn.execute(
            "SELECT no_test_required, no_test_required_note FROM tickets WHERE id='B-1'"
        ).fetchone()
        assert row["no_test_required"] == 0
        assert row["no_test_required_note"] == ""


# ---------------------------------------------------------------------------
# Spine event emission on existing mutations
# ---------------------------------------------------------------------------


class TestSpineEventsOnExistingActions:
    def test_move_ticket_emits_section_and_status(self, conn):
        _add_ticket(conn, section="Backlog", status="specified")
        conn.commit()
        move_ticket(conn, "p", "B-1", "WIP", actor=ActorContext.human("alice"))
        conn.commit()
        kinds = [
            r[0]
            for r in conn.execute(
                "SELECT event_kind FROM activity_events ORDER BY id"
            ).fetchall()
        ]
        assert "section_change" in kinds
        assert "status_change" in kinds

    def test_move_to_same_section_emits_nothing(self, conn):
        _add_ticket(conn, section="WIP", status="in-progress")
        conn.commit()
        move_ticket(conn, "p", "B-1", "WIP", actor=ActorContext.human())
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
        assert n == 0

    def test_update_status_emits_status_change(self, conn):
        _add_ticket(conn, section="WIP", status="in-progress")
        conn.commit()
        update_ticket(
            conn, "p", "B-1", status="blocked", actor=ActorContext.human("alice")
        )
        conn.commit()
        e = conn.execute(
            "SELECT event_kind, payload_json FROM activity_events ORDER BY id DESC"
        ).fetchone()
        assert e["event_kind"] == "status_change"
        assert json.loads(e["payload_json"]) == {
            "before": "in-progress",
            "after": "blocked",
        }

    def test_update_title_does_not_emit_in_m1a(self, conn):
        # Title edits land in M1b's field_changed vocabulary, not M1a.
        _add_ticket(conn)
        conn.commit()
        update_ticket(conn, "p", "B-1", title="New title", actor=ActorContext.human())
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
        assert n == 0

    def test_check_criteria_emits_criteria_check(self, conn):
        _add_ticket(conn, with_criteria=True)
        conn.commit()
        update_ticket(conn, "p", "B-1", check_criteria=1, actor=ActorContext.human())
        conn.commit()
        e = conn.execute(
            "SELECT event_kind, payload_json FROM activity_events ORDER BY id DESC"
        ).fetchone()
        assert e["event_kind"] == "criteria_check"
        payload = json.loads(e["payload_json"])
        assert payload["before"] is False
        assert payload["after"] is True

    def test_check_already_checked_emits_nothing(self, conn):
        _add_ticket(conn, with_criteria=True)
        conn.commit()
        conn.execute("UPDATE acceptance_criteria SET checked = 1")
        conn.commit()
        update_ticket(conn, "p", "B-1", check_criteria=1, actor=ActorContext.human())
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
        assert n == 0

    def test_accept_ticket_emits_section_and_status(self, conn, tmp_path):
        _add_ticket(conn, section="For Review", status="for-review")
        conn.commit()
        # This test is about event emission, not the close gate — bypass it
        # explicitly. Gate coverage lives in tests/test_tdd_spec_lifecycle.py.
        accept_ticket(
            conn,
            "p",
            "B-1",
            str(tmp_path),
            "p",
            actor=ActorContext.human("alice"),
            force="test fixture: exercising event emission",
        )
        conn.commit()
        kinds = [
            r[0]
            for r in conn.execute(
                "SELECT event_kind FROM activity_events WHERE subject_id='B-1' ORDER BY id"
            ).fetchall()
        ]
        assert "section_change" in kinds
        assert "status_change" in kinds


# ---------------------------------------------------------------------------
# Internal-side-effect rule (§9): cascaded mutations emit too, with system actor.
# ---------------------------------------------------------------------------


class TestInternalSideEffects:
    def test_parent_auto_promote_no_longer_synchronous(self, conn):
        """Phase A migration (tidy-newt): parent-promote moved to a system
        workflow. update_ticket() must NOT promote the parent inline anymore —
        the workflow dispatcher does it on the next tick.

        See tests/test_tdd_system_workflows.py for the end-to-end coverage.
        """
        conn.execute(
            "INSERT INTO tickets (id, project_id, title, section, status, description) "
            "VALUES ('B-1', 'p', 'Parent', 'WIP', 'in-progress', 'desc')"
        )
        conn.execute(
            "INSERT INTO tickets (id, project_id, title, section, status, description, parent) "
            "VALUES ('BUG-1', 'p', 'Child A', 'Bugs', 'bug', 'd', 'B-1')"
        )
        conn.execute(
            "INSERT INTO tickets (id, project_id, title, section, status, description, parent) "
            "VALUES ('BUG-2', 'p', 'Child B', 'Bugs', 'bug', 'd', 'B-1')"
        )
        conn.commit()

        update_ticket(
            conn, "p", "BUG-1", status="bug-fixed", actor=ActorContext.human()
        )
        conn.commit()
        update_ticket(
            conn, "p", "BUG-2", status="bug-fixed", actor=ActorContext.human()
        )
        conn.commit()

        # Parent must still be in WIP (no synchronous cascade).
        parent = conn.execute("SELECT section FROM tickets WHERE id = 'B-1'").fetchone()
        assert parent["section"] == "WIP", (
            "parent should NOT be promoted by update_ticket — workflow handles it"
        )

        # No system section_change event should have fired for the parent.
        rows = conn.execute(
            "SELECT subject_id FROM activity_events "
            "WHERE subject_id='B-1' AND event_kind='section_change' "
            "AND actor_type='system'"
        ).fetchall()
        assert len(rows) == 0, "no synchronous parent-promote event expected"

    # NOTE: test_scheduled_auto_accept_emits_system_event removed — Phase A
    # migration (tidy-newt) moved auto-accept into a system workflow. The
    # scheduled_events table + 30s poller stay in place dormant for future
    # delayed-effect support. See workflows_seed.py "Auto-accept reviewed
    # tickets" entry (disabled by default).


# ---------------------------------------------------------------------------
# Module skeletons importable
# ---------------------------------------------------------------------------


class TestModuleSkeletons:
    def test_kitchen_lifecycle(self):
        import kitchen

        kitchen.start(get_db=lambda: None, settings={"kitchen_poll_seconds": 0.05})
        kitchen.stop()
        # Idempotent stop
        kitchen.stop()

    def test_workspaces_path_deterministic(self):
        from workspaces import _sanitize_key, workspace_path_for

        p = workspace_path_for("proj", "ticket", "B-42")
        assert "proj" in str(p) and "ticket" in str(p) and "B-42" in str(p)
        assert _sanitize_key("a/b*c") == "a_b_c"

    def test_runner_abc(self):
        from runners import Runner

        # Can't instantiate the ABC
        with pytest.raises(TypeError):
            Runner()  # type: ignore[abstract]
