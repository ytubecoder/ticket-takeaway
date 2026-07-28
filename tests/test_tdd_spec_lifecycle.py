"""TDD tests for the OpenSpec close gate (actions.py + conditions.py).

Pure logic, no server. The point of these tests is the claim in the design: the
gate lives in the shared core, so the headless CLI path and the dashboard path
cannot diverge. Anything that could be enforced in only one surface is a bug.

Real `openspec` invocations are exercised in tests/test_openspec_adapter.py
against captured fixtures; here the adapter is stubbed so the gate's own logic
is what's under test.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import actions
from db import init_db

PROJECT = "p"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c


def _ticket(conn, tid="B-1", section="For Review", status="for-review"):
    conn.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, PROJECT, "Knowledge ingestion pipeline", section, status, "desc"),
    )
    return conn.execute(
        "SELECT * FROM tickets WHERE id = ? AND project_id = ?", (tid, PROJECT)
    ).fetchone()


def _pass_verify(conn, tid="B-1", commit="abc1234"):
    rec = actions.VerifyRecord(
        command="tests/run-tests.sh",
        exit_code=0,
        commit=commit,
        ran_at="2026-07-23T02:00:00",
        output_tail="    OK",
    )
    actions.write_readiness_flag(conn, PROJECT, tid, "verified", rec.render())


@pytest.fixture
def at_head(monkeypatch):
    """Pin HEAD so verify's commit check is deterministic."""
    monkeypatch.setattr(actions, "project_path_for", lambda pid: "/tmp/fake-project")
    monkeypatch.setattr(actions, "capture_commit_hash", lambda path: "abc1234")


# ---------------------------------------------------------------------------
# Wire formats must survive the PRODUCT_BACKLOG.md round-trip
# ---------------------------------------------------------------------------


class TestSpecLinkFormat:
    @pytest.mark.parametrize(
        "raw,lane,change,note",
        [
            (
                "A:b-44-knowledge-ingestion-pipeline",
                "A",
                "b-44-knowledge-ingestion-pipeline",
                "",
            ),
            ("B:b-7-thing", "B", "b-7-thing", ""),
            (
                "C:none - rename only, no observable change",
                "C",
                "none",
                "rename only, no observable change",
            ),
            ("C:none — em dash separator", "C", "none", "em dash separator"),
            ("  a:b-1-x  ", "A", "b-1-x", ""),
        ],
    )
    def test_parses_declared_lanes(self, raw, lane, change, note):
        link = actions.parse_spec_link(raw)
        assert (link.lane, link.change, link.note) == (lane, change, note)

    @pytest.mark.parametrize("raw", ["", "   ", "garbage", "D:b-1-x", "b-1-x"])
    def test_undeclared_input_yields_an_empty_link(self, raw):
        assert not actions.parse_spec_link(raw).declared

    def test_render_round_trips_through_parse(self):
        original = actions.SpecLink(lane="C", change="none", note="docs only")
        assert actions.parse_spec_link(original.render()) == original

    def test_multiline_content_reads_only_the_first_line(self):
        # The markdown writer emits continuation lines indented under the flag;
        # the lane must come from the header line alone.
        link = actions.parse_spec_link("A:b-2-x\n    some trailing note\n    more")
        assert (link.lane, link.change) == ("A", "b-2-x")


class TestVerifyRecordFormat:
    def test_round_trips(self):
        rec = actions.VerifyRecord(
            command="pnpm test && pnpm typecheck",
            exit_code=0,
            commit="deadbee",
            ran_at="2026-07-23T02:00:00",
            output_tail="    42 passed",
        )
        assert actions.parse_verify_record(rec.render()) == rec

    def test_failure_is_recorded_not_discarded(self):
        rec = actions.VerifyRecord(
            command="tests/run-tests.sh",
            exit_code=1,
            commit="deadbee",
            ran_at="2026-07-23T02:00:00",
            output_tail="    FAIL: test_x",
        )
        parsed = actions.parse_verify_record(rec.render())
        assert parsed.exit_code == 1 and not parsed.passed
        assert "FAIL: test_x" in parsed.output_tail

    @pytest.mark.parametrize(
        "raw", ["", "not a record", "exit=abc commit=x at=y cmd=z"]
    )
    def test_unparseable_input_is_not_a_pass(self, raw):
        assert not actions.parse_verify_record(raw).passed


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


class TestSpecLinked:
    def test_undeclared_lane_fails(self, conn):
        t = _ticket(conn)
        ok, reasons = actions._spec_linked(conn, t)
        assert not ok and "no spec lane declared" in reasons[0]

    @pytest.mark.parametrize("lane", ["A", "B"])
    def test_lane_a_and_b_require_a_change_name(self, conn, lane):
        t = _ticket(conn)
        actions.write_readiness_flag(
            conn, PROJECT, "B-1", "spec", f"{lane}:none - nope"
        )
        ok, reasons = actions._spec_linked(conn, t)
        assert not ok and "requires an OpenSpec change name" in reasons[0]

    def test_lane_c_without_a_reason_is_refused(self, conn):
        t = _ticket(conn)
        actions.write_readiness_flag(conn, PROJECT, "B-1", "spec", "C:none")
        ok, reasons = actions._spec_linked(conn, t)
        assert not ok and "records no reason" in reasons[0]

    def test_lane_c_with_a_reason_passes(self, conn):
        t = _ticket(conn)
        actions.write_readiness_flag(
            conn,
            PROJECT,
            "B-1",
            "spec",
            "C:none - dependency bump, no behaviour change",
        )
        ok, _ = actions._spec_linked(conn, t)
        assert ok

    def test_lane_a_with_a_change_passes(self, conn):
        t = _ticket(conn)
        actions.write_readiness_flag(
            conn, PROJECT, "B-1", "spec", "A:b-1-knowledge-ingestion-pipeline"
        )
        ok, reasons = actions._spec_linked(conn, t)
        assert ok and "b-1-knowledge-ingestion-pipeline" in reasons[0]


class TestVerifyPassed:
    def test_no_recorded_run_fails(self, conn):
        t = _ticket(conn)
        ok, reasons = actions._verify_passed(conn, t)
        assert not ok and "no verify run recorded" in reasons[0]

    def test_failing_run_fails_and_quotes_the_output(self, conn, at_head):
        t = _ticket(conn)
        rec = actions.VerifyRecord(
            command="tests/run-tests.sh",
            exit_code=1,
            commit="abc1234",
            ran_at="now",
            output_tail="    FAIL: test_thing",
        )
        actions.write_readiness_flag(conn, PROJECT, "B-1", "verified", rec.render())
        ok, reasons = actions._verify_passed(conn, t)
        assert not ok
        assert "exited 1" in reasons[0]
        assert any("FAIL: test_thing" in r for r in reasons)

    def test_passing_run_at_head_passes(self, conn, at_head):
        t = _ticket(conn)
        _pass_verify(conn, commit="abc1234")
        ok, _ = actions._verify_passed(conn, t)
        assert ok

    def test_stale_green_from_an_older_commit_is_refused(self, conn, at_head):
        """A pass recorded three commits ago is not evidence about this code."""
        t = _ticket(conn)
        _pass_verify(conn, commit="0000old")
        ok, reasons = actions._verify_passed(conn, t)
        assert not ok and "re-run verify" in reasons[0]


class TestTestsCoveredNoLongerTraps:
    """`tests_covered` sits in six projects' Backlog → WIP triggers while
    journey_tickets is empty — it was unsatisfiable. Verify evidence fixes it."""

    def test_verify_evidence_satisfies_tests_covered(self, conn, at_head):
        t = _ticket(conn, section="Backlog", status="specified")
        _pass_verify(conn)
        ok, reasons = actions._tests_covered(conn, t)
        assert ok, reasons

    def test_without_any_evidence_it_still_fails_and_says_why(self, conn, at_head):
        t = _ticket(conn, section="Backlog", status="specified")
        ok, reasons = actions._tests_covered(conn, t)
        assert not ok
        assert any("verify" in r for r in reasons), reasons

    def test_the_legacy_no_test_required_path_still_works(self, conn, at_head):
        _ticket(conn, section="Backlog", status="specified")
        conn.execute(
            "UPDATE tickets SET no_test_required = 1, no_test_required_note = 'config only' "
            "WHERE id = 'B-1' AND project_id = ?",
            (PROJECT,),
        )
        t = conn.execute(
            "SELECT * FROM tickets WHERE id='B-1' AND project_id=?", (PROJECT,)
        ).fetchone()
        ok, _ = actions._tests_covered(conn, t)
        assert ok


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestAcceptGate:
    def test_bare_ticket_is_refused_with_specific_reasons(self, conn, at_head):
        t = _ticket(conn)
        gate = actions.evaluate_accept_gate(conn, t)
        assert not gate.passed
        assert any("verify" in f for f in gate.failures)
        assert any("spec lane" in f for f in gate.failures)

    def test_refusal_message_names_the_ticket_and_the_override(self, conn, at_head):
        t = _ticket(conn)
        msg = actions.evaluate_accept_gate(conn, t).refusal_message(PROJECT, "B-1")
        assert "Cannot accept B-1" in msg
        assert "--force" in msg

    def test_verify_alone_is_not_enough(self, conn, at_head):
        t = _ticket(conn)
        _pass_verify(conn)
        gate = actions.evaluate_accept_gate(conn, t)
        assert not gate.passed
        assert any("spec lane" in f for f in gate.failures)

    def test_spec_alone_is_not_enough(self, conn, at_head):
        t = _ticket(conn)
        actions.write_readiness_flag(conn, PROJECT, "B-1", "spec", "C:none - docs only")
        gate = actions.evaluate_accept_gate(conn, t)
        assert not gate.passed
        assert any("verify" in f for f in gate.failures)

    def test_lane_c_closes_through_the_same_gate_as_lane_a(self, conn, at_head):
        """The lane changes how work is described, never how it is closed."""
        t = _ticket(conn)
        _pass_verify(conn)
        actions.write_readiness_flag(
            conn, PROJECT, "B-1", "spec", "C:none - dep bump only"
        )
        gate = actions.evaluate_accept_gate(conn, t)
        assert gate.passed, gate.failures
        # Same evidence standard: verify output is real in both lanes.
        assert any("tests/run-tests.sh" in e for e in gate.evidence)

    def test_lane_a_needs_the_change_to_validate(
        self, conn, at_head, monkeypatch, tmp_path
    ):
        t = _ticket(conn)
        _pass_verify(conn)
        actions.write_readiness_flag(conn, PROJECT, "B-1", "spec", "A:b-1-thing")
        monkeypatch.setattr(actions, "project_path_for", lambda pid: str(tmp_path))
        # No openspec/ root at all -> refused, with a fix instruction.
        gate = actions.evaluate_accept_gate(conn, t)
        assert not gate.passed
        assert any("openspec init" in f for f in gate.failures)


class TestGateIsSurfaceIndependent:
    """§2: the rule lives in the core, so both surfaces inherit it."""

    def test_both_entrypoints_call_the_same_gate(self):
        import inspect

        # serve.py routes accepts through actions.accept_ticket.
        serve_src = (Path(__file__).parent.parent / "src" / "serve.py").read_text(
            encoding="utf-8"
        )
        assert "accept_ticket as _actions_accept_ticket" in serve_src
        assert "_actions_accept_ticket(" in serve_src

        # tickets-cli.py imports the very same symbol.
        cli_src = (Path(__file__).parent.parent / "src" / "tickets-cli.py").read_text(
            encoding="utf-8"
        )
        assert "accept_ticket" in cli_src

        # And the gate is invoked inside accept_ticket itself, not by either caller,
        # which is what makes bypassing it impossible from a new surface.
        assert "evaluate_accept_gate(" in inspect.getsource(actions.accept_ticket)

    def test_both_surfaces_share_one_readiness_flag_allowlist(self):
        import constants

        serve_src = (Path(__file__).parent.parent / "src" / "serve.py").read_text(
            encoding="utf-8"
        )
        cli_src = (Path(__file__).parent.parent / "src" / "tickets-cli.py").read_text(
            encoding="utf-8"
        )
        # Neither surface may hard-code its own set.
        assert 'VALID_READINESS_FLAGS = {"reviewed"}' not in serve_src
        assert 'VALID_FLAGS = {"reviewed"}' not in cli_src
        assert constants.VALID_READINESS_FLAGS == {"reviewed", "spec", "verified"}

    def test_every_allowed_flag_round_trips_through_markdown(self):
        """A flag with no markdown label is silently dropped on regeneration."""
        import constants

        for flag in constants.VALID_READINESS_FLAGS:
            assert flag in constants.READINESS_FLAG_LABELS, flag
            label = constants.READINESS_FLAG_LABELS[flag]
            assert constants.READINESS_LABEL_TO_FLAG[f"{label}:"] == flag


class TestForceOverride:
    def test_force_records_the_reason_rather_than_bypassing_silently(
        self, conn, at_head, tmp_path
    ):
        _ticket(conn)
        actions.accept_ticket(
            conn,
            PROJECT,
            "B-1",
            str(tmp_path),
            "Proj",
            force="hotfix: prod outage, spec to follow in B-2",
        )
        link = actions.spec_link(conn, PROJECT, "B-1")
        assert "accepted with --force" in link.note
        assert "prod outage" in link.note

        events = conn.execute(
            "SELECT event_kind, payload_json FROM activity_events "
            "WHERE subject_id = 'B-1' AND event_kind = 'gate_override'"
        ).fetchall()
        assert len(events) == 1
        assert "prod outage" in events[0]["payload_json"]

    def test_force_notes_the_override_in_the_product_specification(
        self, conn, at_head, tmp_path
    ):
        _ticket(conn)
        actions.accept_ticket(
            conn, PROJECT, "B-1", str(tmp_path), "Proj", force="deliberate"
        )
        spec = (tmp_path / "PRODUCT_SPECIFICATION.md").read_text(encoding="utf-8")
        assert "Gate overridden: deliberate" in spec

    def test_without_force_a_failing_ticket_raises(self, conn, at_head, tmp_path):
        _ticket(conn)
        with pytest.raises(ValueError) as exc:
            actions.accept_ticket(conn, PROJECT, "B-1", str(tmp_path), "Proj")
        assert "Cannot accept B-1" in str(exc.value)
        # Nothing was written — a refused accept must not half-close the ticket.
        row = conn.execute(
            "SELECT section FROM tickets WHERE id='B-1' AND project_id=?", (PROJECT,)
        ).fetchone()
        assert row["section"] == "For Review"
        assert not (tmp_path / "PRODUCT_SPECIFICATION.md").exists()


class TestAcceptWritesEvidence:
    def test_passing_accept_records_the_verify_command_in_the_spec(
        self, conn, at_head, tmp_path
    ):
        _ticket(conn)
        _pass_verify(conn)
        actions.write_readiness_flag(
            conn, PROJECT, "B-1", "spec", "C:none - dep bump only"
        )
        actions.accept_ticket(conn, PROJECT, "B-1", str(tmp_path), "Proj")
        spec = (tmp_path / "PRODUCT_SPECIFICATION.md").read_text(encoding="utf-8")
        assert "Verified: `tests/run-tests.sh` exit 0" in spec
        row = conn.execute(
            "SELECT section, status FROM tickets WHERE id='B-1' AND project_id=?",
            (PROJECT,),
        ).fetchone()
        assert (row["section"], row["status"]) == ("Done", "done")


# ---------------------------------------------------------------------------
# Verify command resolution
# ---------------------------------------------------------------------------


class TestResolveVerifyCommand:
    def test_workflow_toml_wins(self, tmp_path):
        (tmp_path / "WORKFLOW.toml").write_text(
            '[verify]\ncommand = "pnpm test && pnpm typecheck"\ntimeout_ms = 900000\n',
            encoding="utf-8",
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "run-tests.sh").write_text(
            "#!/bin/sh\n", encoding="utf-8"
        )
        cmd, timeout, source = actions.resolve_verify_command(str(tmp_path))
        assert cmd == "pnpm test && pnpm typecheck"
        assert timeout == 900000
        assert source == "WORKFLOW.toml"

    def test_falls_back_to_the_conventional_runner(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "run-tests.sh").write_text(
            "#!/bin/sh\n", encoding="utf-8"
        )
        cmd, _, source = actions.resolve_verify_command(str(tmp_path))
        assert cmd == "tests/run-tests.sh" and source == "tests/run-tests.sh"

    def test_falls_back_to_the_package_json_test_script(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"scripts": {"test": "vitest run"}}', encoding="utf-8"
        )
        cmd, _, source = actions.resolve_verify_command(str(tmp_path))
        assert cmd == "npm test" and source == "package.json test script"

    def test_a_malformed_workflow_toml_does_not_disable_the_fallbacks(self, tmp_path):
        (tmp_path / "WORKFLOW.toml").write_text(
            "this is not = valid toml [[[", encoding="utf-8"
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "run-tests.sh").write_text(
            "#!/bin/sh\n", encoding="utf-8"
        )
        cmd, _, _ = actions.resolve_verify_command(str(tmp_path))
        assert cmd == "tests/run-tests.sh"

    def test_nothing_found_returns_empty_so_the_caller_asks_once(self, tmp_path):
        cmd, _, source = actions.resolve_verify_command(str(tmp_path))
        assert cmd == "" and source == ""


class TestRunVerifyRecordsRealOutput:
    def test_records_exit_code_and_output_for_a_failing_command(
        self, conn, tmp_path, monkeypatch
    ):
        _ticket(conn)
        (tmp_path / "WORKFLOW.toml").write_text(
            '[verify]\ncommand = "echo boom-output && exit 3"\n', encoding="utf-8"
        )
        monkeypatch.setattr(actions, "capture_commit_hash", lambda path: "cafe123")
        rec = actions.run_verify(conn, PROJECT, "B-1", str(tmp_path))
        assert rec.exit_code == 3 and not rec.passed
        assert "boom-output" in rec.output_tail
        # Persisted, so the gate reads evidence rather than re-running commands.
        assert actions.verify_record(conn, PROJECT, "B-1").exit_code == 3

    def test_output_tail_is_indented_to_survive_markdown_regeneration(
        self, conn, tmp_path, monkeypatch
    ):
        _ticket(conn)
        (tmp_path / "WORKFLOW.toml").write_text(
            '[verify]\ncommand = "echo hello"\n', encoding="utf-8"
        )
        monkeypatch.setattr(actions, "capture_commit_hash", lambda path: "cafe123")
        rec = actions.run_verify(conn, PROJECT, "B-1", str(tmp_path))
        assert rec.passed
        assert all(ln.startswith("    ") for ln in rec.output_tail.splitlines() if ln)

    def test_missing_verify_command_raises_with_the_toml_to_write(self, conn, tmp_path):
        _ticket(conn)
        with pytest.raises(ValueError) as exc:
            actions.run_verify(conn, PROJECT, "B-1", str(tmp_path))
        assert "[verify]" in str(exc.value)


# ---------------------------------------------------------------------------
# conditions.py exposes the same predicates to the workflow engine
# ---------------------------------------------------------------------------


class TestConditionsDelegateToActions:
    def test_predicates_are_registered(self):
        from conditions import CONDITION_CATALOG

        for kind in ("spec_linked", "spec_validates", "verify_passed"):
            assert kind in CONDITION_CATALOG
            assert callable(CONDITION_CATALOG[kind]["evaluator"])

    def test_verify_passed_predicate_agrees_with_actions(self, conn, at_head):
        from conditions import build_subject_context, evaluate_condition

        _ticket(conn)
        _pass_verify(conn)
        ctx = build_subject_context(conn, PROJECT, "B-1")
        passed, _ = evaluate_condition({"kind": "verify_passed"}, ctx)
        direct, _ = actions._verify_passed(conn, ctx["ticket_row"])
        assert passed is direct is True

    def test_spec_linked_predicate_agrees_with_actions(self, conn, at_head):
        from conditions import build_subject_context, evaluate_condition

        _ticket(conn)
        ctx = build_subject_context(conn, PROJECT, "B-1")
        passed, reason = evaluate_condition({"kind": "spec_linked"}, ctx)
        assert passed is False
        assert "no spec lane declared" in reason
