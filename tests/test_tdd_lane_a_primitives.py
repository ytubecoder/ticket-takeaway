"""TDD tests for Lane A (factory-talk) primitives.

Covers:
  - Migration 17 columns: tickets.is_container, workflow_agents.system,
    runs.needs_input_kind
  - runners._try_parse_marker / _try_parse_handoff (private helpers, importable)
  - conditions.py: has_tag, lacks_tag, lacks_readiness_flag predicates
  - constants.py: GATE_BANNER_BY_SECTION, EVENT_KIND_LABELS, EVENT_KIND_ICONS
  - workflows_seed: new workflows (Done -> Learnings, Sprint tag rotation) and
    system agents (Orchestrator, Worker, Validator)

Pure logic, no server, no Playwright.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from conditions import (
    CONDITION_CATALOG,
    evaluate_condition,
    evaluate_trigger,
)
from constants import (
    EVENT_KIND_ICONS,
    EVENT_KIND_LABELS,
    GATE_BANNER_BY_SECTION,
    VALID_STATUSES_BY_SECTION,
)
from db import init_db
from runners import _try_parse_handoff, _try_parse_marker
from workflows_seed import (
    DEFAULT_AGENTS,
    DEFAULT_WORKFLOWS,
    seed_default_agents,
    seed_default_endpoints,
    seed_default_workflows,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """In-memory DB with full schema including migration 17.

    Endpoints are pre-seeded so that seed_default_agents can satisfy the
    FK constraint on workflow_agents.endpoint_id (added in migration 19).
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    seed_default_endpoints(c)
    return c


PROJECT_ID = "test-project"


def _make_ticket(
    conn,
    tid="B-1",
    project_id=PROJECT_ID,
    section="Backlog",
    status="in-progress",
    description="desc",
):
    conn.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, project_id, f"Title {tid}", section, status, description),
    )
    conn.commit()


def _add_tag(conn, tid, tag, project_id=PROJECT_ID):
    conn.execute(
        "INSERT OR IGNORE INTO ticket_tags (ticket_id, project_id, tag) "
        "VALUES (?, ?, ?)",
        (tid, project_id, tag),
    )
    conn.commit()


def _add_readiness_flag(conn, tid, flag, content="some content", project_id=PROJECT_ID):
    conn.execute(
        "INSERT OR REPLACE INTO readiness_flags (ticket_id, project_id, flag, content) "
        "VALUES (?, ?, ?, ?)",
        (tid, project_id, flag, content),
    )
    conn.commit()


def _make_ctx(conn, tid="B-1", project_id=PROJECT_ID, active_run=False):
    ticket_row = conn.execute(
        "SELECT * FROM tickets WHERE id = ? AND project_id = ?", (tid, project_id)
    ).fetchone()
    assert ticket_row is not None, f"Ticket {tid!r} not found"
    subj = conn.execute(
        "SELECT * FROM automation_subjects "
        "WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ?",
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


# ===========================================================================
# 1. Migration 17 column presence
# ===========================================================================


class TestMigration17Columns:
    def test_tickets_has_is_container_column(self, conn):
        cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(tickets)").fetchall()
        }
        assert "is_container" in cols

    def test_is_container_default_zero(self, conn):
        _make_ticket(conn)
        row = conn.execute(
            "SELECT is_container FROM tickets WHERE id = 'B-1' AND project_id = ?",
            (PROJECT_ID,),
        ).fetchone()
        assert row is not None
        assert row["is_container"] == 0

    def test_workflow_agents_has_system_column(self, conn):
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(workflow_agents)").fetchall()
        }
        assert "system" in cols

    def test_workflow_agent_system_defaults_to_zero(self, conn):
        conn.execute(
            "INSERT INTO workflow_agents (id, name, command, args, system_prompt) "
            "VALUES ('test-agent', 'Test', 'claude', '[]', '')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT system FROM workflow_agents WHERE id = 'test-agent'"
        ).fetchone()
        assert row is not None
        assert row["system"] == 0

    def test_runs_has_needs_input_kind_column(self, conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        assert "needs_input_kind" in cols

    def test_needs_input_kind_nullable(self, conn):
        """needs_input_kind must be NULL on a vanilla queued run (no interaction)."""
        # Insert minimal run row without needs_input_kind
        _make_ticket(conn)
        conn.execute(
            "INSERT INTO runs "
            "(project_id, subject_type, subject_id, runner_kind, status, triggered_by) "
            "VALUES (?, 'ticket', 'B-1', 'agent', 'queued', 'human')",
            (PROJECT_ID,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT needs_input_kind FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["needs_input_kind"] is None


# ===========================================================================
# 2. Marker parsing: _try_parse_marker
# ===========================================================================


class TestTryParseMarker:
    def test_well_formed_ask_marker_parses(self):
        line = '{"ask": "What is the goal?", "context": "filling ticket description"}'
        result = _try_parse_marker(line)
        assert result is not None
        assert result["ask"] == "What is the goal?"

    def test_well_formed_propose_marker_parses(self):
        line = '{"propose": {"description": "Do X", "add_criteria": ["criterion 1"]}}'
        result = _try_parse_marker(line)
        assert result is not None
        assert "propose" in result
        assert result["propose"]["description"] == "Do X"

    def test_ask_marker_with_extra_whitespace_parses(self):
        line = '  {"ask": "Are you sure?"}  '
        result = _try_parse_marker(line)
        assert result is not None
        assert result["ask"] == "Are you sure?"

    def test_malformed_json_returns_none(self):
        line = '{"ask": "missing close brace"'
        assert _try_parse_marker(line) is None

    def test_free_text_returns_none(self):
        line = "This is just normal agent output text."
        assert _try_parse_marker(line) is None

    def test_json_without_ask_or_propose_returns_none(self):
        line = '{"implemented": ["did the thing"], "undone": []}'
        assert _try_parse_marker(line) is None

    def test_json_array_not_object_returns_none(self):
        line = '["ask", "propose"]'
        assert _try_parse_marker(line) is None

    def test_empty_string_returns_none(self):
        assert _try_parse_marker("") is None

    def test_propose_with_empty_criteria_parses(self):
        line = '{"propose": {"description": "", "add_criteria": [], "add_tags": []}}'
        result = _try_parse_marker(line)
        assert result is not None
        assert result["propose"]["add_criteria"] == []

    def test_both_ask_and_propose_keys_both_parse(self):
        """A marker may technically carry both keys; function still returns the dict."""
        line = '{"ask": "X", "propose": {}}'
        result = _try_parse_marker(line)
        assert result is not None
        assert "ask" in result
        assert "propose" in result


# ===========================================================================
# 3. Handoff parsing: _try_parse_handoff
# ===========================================================================


class TestTryParseHandoff:
    def test_well_formed_handoff_all_keys_parses(self):
        text = (
            "Doing some work...\n"
            '{"implemented": ["added tests"], "undone": [], '
            '"commands": [{"cmd": "npm test", "exit_code": 0}], '
            '"issues": [], "procedures_followed": ["ran lint"]}'
        )
        result = _try_parse_handoff(text)
        assert result != {}
        assert result["implemented"] == ["added tests"]
        assert result["commands"] == [{"cmd": "npm test", "exit_code": 0}]
        assert result["procedures_followed"] == ["ran lint"]

    def test_handoff_with_missing_keys_returns_empty_arrays(self):
        """Missing keys are normalised to empty arrays, not KeyError."""
        text = '{"implemented": ["done thing"]}'
        result = _try_parse_handoff(text)
        assert result["implemented"] == ["done thing"]
        assert result["undone"] == []
        assert result["commands"] == []
        assert result["issues"] == []
        assert result["procedures_followed"] == []

    def test_no_json_line_returns_empty_dict(self):
        text = "Agent finished. All good. No JSON here."
        result = _try_parse_handoff(text)
        assert result == {}

    def test_plain_dict_json_not_handoff_returns_empty_dict(self):
        """A JSON object that doesn't contain any handoff keys is not picked up."""
        text = '{"ask": "what to do?", "context": "something"}'
        result = _try_parse_handoff(text)
        assert result == {}

    def test_malformed_json_is_skipped(self):
        text = '{"implemented": ["step"] BROKEN'
        result = _try_parse_handoff(text)
        assert result == {}

    def test_picks_last_valid_handoff_from_stream(self):
        """When multiple handoff-like objects appear, the last valid one wins."""
        text = (
            '{"implemented": ["step 1"], "undone": []}\n'
            "Some extra prose.\n"
            '{"implemented": ["step 2"], "undone": ["thing X"]}'
        )
        result = _try_parse_handoff(text)
        assert result["implemented"] == ["step 2"]
        assert result["undone"] == ["thing X"]

    def test_json_object_earlier_in_stream_not_picked_if_later_is_better(self):
        """Scanning from the tail; the last well-formed handoff wins."""
        text = (
            '{"implemented": ["early"], "undone": []}\n'
            "Normal prose line.\n"
            '{"implemented": ["latest"], "issues": ["a problem"]}'
        )
        result = _try_parse_handoff(text)
        assert result["implemented"] == ["latest"]

    def test_empty_text_returns_empty_dict(self):
        assert _try_parse_handoff("") == {}

    def test_none_text_returns_empty_dict(self):
        assert _try_parse_handoff(None) == {}  # type: ignore[arg-type]

    def test_only_scanning_last_50_lines(self):
        """Lines beyond the 50-line window are not scanned."""
        early_json = '{"implemented": ["too old"], "undone": []}'
        # 60 blank lines push the JSON out of the window
        text = early_json + "\n" * 60 + "no json here"
        result = _try_parse_handoff(text)
        assert result == {}


# ===========================================================================
# 4. Tag predicates: has_tag, lacks_tag
# ===========================================================================


class TestHasTagPredicate:
    def test_has_tag_returns_true_when_ticket_has_all_listed_tags(self, conn):
        _make_ticket(conn)
        _add_tag(conn, "B-1", "foo")
        _add_tag(conn, "B-1", "bar")
        ctx = _make_ctx(conn)
        cond = {"kind": "has_tag", "value": ["foo", "bar"]}
        passed, reason = evaluate_condition(cond, ctx)
        assert passed is True

    def test_has_tag_returns_false_when_ticket_missing_one_tag(self, conn):
        _make_ticket(conn)
        _add_tag(conn, "B-1", "foo")
        ctx = _make_ctx(conn)
        cond = {"kind": "has_tag", "value": ["foo", "missing-tag"]}
        passed, reason = evaluate_condition(cond, ctx)
        assert passed is False
        assert "missing-tag" in reason

    def test_has_tag_vacuously_true_when_no_tags_required(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        cond = {"kind": "has_tag", "value": []}
        passed, reason = evaluate_condition(cond, ctx)
        assert passed is True

    def test_has_tag_false_when_ticket_has_no_tags_at_all(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        cond = {"kind": "has_tag", "value": ["required-tag"]}
        passed, reason = evaluate_condition(cond, ctx)
        assert passed is False

    def test_has_tag_single_string_value_coerced_to_list(self, conn):
        """'value' may be a bare string; predicate coerces to [str]."""
        _make_ticket(conn)
        _add_tag(conn, "B-1", "single")
        ctx = _make_ctx(conn)
        cond = {"kind": "has_tag", "value": "single"}
        passed, _ = evaluate_condition(cond, ctx)
        assert passed is True

    def test_has_tag_registered_in_condition_catalog(self):
        assert "has_tag" in CONDITION_CATALOG


class TestLacksTagPredicate:
    def test_lacks_tag_true_when_ticket_has_none_of_listed_tags(self, conn):
        _make_ticket(conn)
        _add_tag(conn, "B-1", "other-tag")
        ctx = _make_ctx(conn)
        cond = {"kind": "lacks_tag", "value": ["excluded"]}
        passed, reason = evaluate_condition(cond, ctx)
        assert passed is True

    def test_lacks_tag_false_when_ticket_has_a_listed_tag(self, conn):
        _make_ticket(conn)
        _add_tag(conn, "B-1", "excluded")
        ctx = _make_ctx(conn)
        cond = {"kind": "lacks_tag", "value": ["excluded"]}
        passed, reason = evaluate_condition(cond, ctx)
        assert passed is False
        assert "excluded" in reason

    def test_lacks_tag_vacuously_true_when_no_tags_to_exclude(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        cond = {"kind": "lacks_tag", "value": []}
        passed, _ = evaluate_condition(cond, ctx)
        assert passed is True

    def test_lacks_tag_true_when_ticket_has_no_tags_at_all(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        cond = {"kind": "lacks_tag", "value": ["missing"]}
        passed, _ = evaluate_condition(cond, ctx)
        assert passed is True

    def test_lacks_tag_false_when_ticket_has_one_of_several_excluded_tags(self, conn):
        _make_ticket(conn)
        _add_tag(conn, "B-1", "sprint-prev")
        ctx = _make_ctx(conn)
        cond = {"kind": "lacks_tag", "value": ["sprint-prev", "sprint-done"]}
        passed, _ = evaluate_condition(cond, ctx)
        assert passed is False

    def test_lacks_tag_registered_in_condition_catalog(self):
        assert "lacks_tag" in CONDITION_CATALOG


# ===========================================================================
# 5. lacks_readiness_flag predicate
# ===========================================================================


class TestLacksReadinessFlagPredicate:
    def test_lacks_readiness_flag_true_when_flag_not_set(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        cond = {"kind": "lacks_readiness_flag", "flag": "L"}
        passed, reason = evaluate_condition(cond, ctx)
        assert passed is True

    def test_lacks_readiness_flag_false_when_flag_set_with_content(self, conn):
        _make_ticket(conn)
        # L-flag maps to 'reviewed' in DB
        _add_readiness_flag(conn, "B-1", "reviewed", content="Learnings captured here.")
        ctx = _make_ctx(conn)
        cond = {"kind": "lacks_readiness_flag", "flag": "L"}
        passed, reason = evaluate_condition(cond, ctx)
        assert passed is False

    def test_lacks_readiness_flag_true_when_flag_row_has_empty_content(self, conn):
        """A flag row with empty content is treated as not set."""
        _make_ticket(conn)
        _add_readiness_flag(conn, "B-1", "reviewed", content="")
        ctx = _make_ctx(conn)
        cond = {"kind": "lacks_readiness_flag", "flag": "L"}
        passed, _ = evaluate_condition(cond, ctx)
        assert passed is True

    def test_lacks_readiness_flag_true_when_flag_row_has_whitespace_only(self, conn):
        _make_ticket(conn)
        _add_readiness_flag(conn, "B-1", "reviewed", content="   ")
        ctx = _make_ctx(conn)
        cond = {"kind": "lacks_readiness_flag", "flag": "L"}
        passed, _ = evaluate_condition(cond, ctx)
        assert passed is True

    def test_lacks_readiness_flag_registered_in_condition_catalog(self):
        assert "lacks_readiness_flag" in CONDITION_CATALOG


# ===========================================================================
# 6. Constants sanity
# ===========================================================================


class TestGateBannerBySection:
    def test_gate_banner_covers_ideas(self):
        assert "Ideas" in GATE_BANNER_BY_SECTION

    def test_gate_banner_covers_backlog(self):
        assert "Backlog" in GATE_BANNER_BY_SECTION

    def test_gate_banner_covers_wip(self):
        assert "WIP" in GATE_BANNER_BY_SECTION

    def test_gate_banner_covers_for_review(self):
        assert "For Review" in GATE_BANNER_BY_SECTION

    def test_gate_banner_covers_done(self):
        assert "Done" in GATE_BANNER_BY_SECTION

    def test_gate_banner_covers_all_valid_status_sections(self):
        """Every key in VALID_STATUSES_BY_SECTION must have a gate banner."""
        for section in VALID_STATUSES_BY_SECTION:
            assert section in GATE_BANNER_BY_SECTION, (
                f"Section {section!r} in VALID_STATUSES_BY_SECTION "
                "but missing from GATE_BANNER_BY_SECTION"
            )

    def test_gate_banner_values_are_non_empty_strings(self):
        for section, banner in GATE_BANNER_BY_SECTION.items():
            assert isinstance(banner, str) and banner.strip(), (
                f"Gate banner for {section!r} is empty"
            )


class TestEventKindLabelIconParity:
    def test_event_kind_labels_and_icons_have_same_keys(self):
        """Every event kind that has a label must have an icon and vice versa."""
        label_keys = set(EVENT_KIND_LABELS.keys())
        icon_keys = set(EVENT_KIND_ICONS.keys())
        only_in_labels = label_keys - icon_keys
        only_in_icons = icon_keys - label_keys
        assert not only_in_labels, (
            f"Event kinds in EVENT_KIND_LABELS but missing in EVENT_KIND_ICONS: "
            f"{sorted(only_in_labels)}"
        )
        assert not only_in_icons, (
            f"Event kinds in EVENT_KIND_ICONS but missing in EVENT_KIND_LABELS: "
            f"{sorted(only_in_icons)}"
        )

    def test_event_kind_labels_are_non_empty(self):
        for kind, label in EVENT_KIND_LABELS.items():
            assert isinstance(label, str) and label.strip(), (
                f"EVENT_KIND_LABELS[{kind!r}] is empty"
            )

    def test_event_kind_icons_are_non_empty(self):
        for kind, icon in EVENT_KIND_ICONS.items():
            assert isinstance(icon, str) and icon.strip(), (
                f"EVENT_KIND_ICONS[{kind!r}] is empty"
            )

    def test_run_started_label_exists(self):
        assert "run_started" in EVENT_KIND_LABELS

    def test_run_succeeded_label_exists(self):
        assert "run_succeeded" in EVENT_KIND_LABELS

    def test_handoff_recorded_label_exists(self):
        assert "handoff_recorded" in EVENT_KIND_LABELS


# ===========================================================================
# 7. Workflow seed: new workflows and system agents (Lane A additions)
# ===========================================================================


class TestNewWorkflowsInManifest:
    def _find(self, name: str) -> dict:
        matches = [wf for wf in DEFAULT_WORKFLOWS if wf["name"] == name]
        assert matches, f"Workflow {name!r} not found in DEFAULT_WORKFLOWS"
        return matches[0]

    def test_done_learnings_extraction_exists(self):
        self._find("Done → Learnings extraction")

    def test_done_learnings_extraction_is_system(self):
        wf = self._find("Done → Learnings extraction")
        assert wf["system"] == 1

    def test_done_learnings_extraction_is_disabled_by_default(self):
        wf = self._find("Done → Learnings extraction")
        assert wf["enabled"] == 0

    def test_done_learnings_extraction_trigger_targets_done_section(self):
        wf = self._find("Done → Learnings extraction")
        conditions = wf["trigger_json"]["all_of"]
        section_conds = [c for c in conditions if c.get("kind") == "section_equals"]
        assert any(c["value"] == "Done" for c in section_conds)

    def test_done_learnings_extraction_trigger_requires_lacks_readiness_flag(self):
        wf = self._find("Done → Learnings extraction")
        conditions = wf["trigger_json"]["all_of"]
        lacks_conds = [c for c in conditions if c.get("kind") == "lacks_readiness_flag"]
        assert lacks_conds, "Trigger must include lacks_readiness_flag condition"

    def test_done_learnings_extraction_on_success_has_set_readiness_content(self):
        wf = self._find("Done → Learnings extraction")
        effect = wf.get("on_success_json", {})
        assert "set_readiness_content" in effect
        assert effect["set_readiness_content"]["flag"] == "reviewed"
        assert effect["set_readiness_content"]["from"] == "stdout"

    def test_done_learnings_extraction_has_one_step(self):
        wf = self._find("Done → Learnings extraction")
        assert len(wf["steps"]) == 1

    def test_sprint_tag_rotation_exists(self):
        self._find("Sprint tag rotation")

    def test_sprint_tag_rotation_is_system(self):
        wf = self._find("Sprint tag rotation")
        assert wf["system"] == 1

    def test_sprint_tag_rotation_is_disabled_by_default(self):
        wf = self._find("Sprint tag rotation")
        assert wf["enabled"] == 0

    def test_sprint_tag_rotation_trigger_uses_has_tag(self):
        wf = self._find("Sprint tag rotation")
        conditions = wf["trigger_json"]["all_of"]
        has_tag_conds = [c for c in conditions if c.get("kind") == "has_tag"]
        assert has_tag_conds, "Sprint tag rotation trigger must use has_tag"

    def test_sprint_tag_rotation_on_success_has_remove_tags(self):
        wf = self._find("Sprint tag rotation")
        effect = wf.get("on_success_json", {})
        assert "remove_tags" in effect

    def test_sprint_tag_rotation_on_success_has_add_tags(self):
        wf = self._find("Sprint tag rotation")
        effect = wf.get("on_success_json", {})
        assert "add_tags" in effect

    def test_sprint_tag_rotation_has_zero_steps(self):
        """Pure mutation workflow — no agent subprocess."""
        wf = self._find("Sprint tag rotation")
        assert wf["steps"] == []


class TestSystemAgentsInManifest:
    def _find_agent(self, agent_id: str) -> dict:
        matches = [a for a in DEFAULT_AGENTS if a["id"] == agent_id]
        assert matches, f"Agent {agent_id!r} not found in DEFAULT_AGENTS"
        return matches[0]

    def test_orchestrator_agent_exists(self):
        self._find_agent("agent_orchestrator")

    def test_orchestrator_is_system_flagged(self):
        agent = self._find_agent("agent_orchestrator")
        assert agent["system"] == 1

    def test_orchestrator_uses_claude_command(self):
        agent = self._find_agent("agent_orchestrator")
        assert agent["command"] == "claude"

    def test_orchestrator_has_system_prompt_mentioning_ask_marker(self):
        agent = self._find_agent("agent_orchestrator")
        assert "ask" in agent["system_prompt"]

    def test_orchestrator_has_system_prompt_mentioning_propose_marker(self):
        agent = self._find_agent("agent_orchestrator")
        assert "propose" in agent["system_prompt"]

    def test_orchestrator_persist_session_enabled(self):
        """Orchestrator uses multi-turn chat, must persist session."""
        agent = self._find_agent("agent_orchestrator")
        assert agent["persist_session"] == 1

    def test_worker_agent_exists(self):
        self._find_agent("agent_worker")

    def test_worker_is_system_flagged(self):
        agent = self._find_agent("agent_worker")
        assert agent["system"] == 1

    def test_worker_system_prompt_mentions_handoff_json(self):
        agent = self._find_agent("agent_worker")
        assert "implemented" in agent["system_prompt"]

    def test_validator_agent_exists(self):
        self._find_agent("agent_validator")

    def test_validator_is_system_flagged(self):
        agent = self._find_agent("agent_validator")
        assert agent["system"] == 1

    def test_validator_system_prompt_mentions_propose_marker(self):
        agent = self._find_agent("agent_validator")
        assert "propose" in agent["system_prompt"]


class TestSystemAgentSeeding:
    def test_orchestrator_is_seeded_into_db_with_system_flag(self, conn):
        seed_default_agents(conn)
        row = conn.execute(
            "SELECT id, name, system FROM workflow_agents WHERE id = 'agent_orchestrator'"
        ).fetchone()
        assert row is not None
        assert row["name"] == "Orchestrator"
        assert row["system"] == 1

    def test_worker_is_seeded_into_db_with_system_flag(self, conn):
        seed_default_agents(conn)
        row = conn.execute(
            "SELECT system FROM workflow_agents WHERE id = 'agent_worker'"
        ).fetchone()
        assert row is not None
        assert row["system"] == 1

    def test_validator_is_seeded_into_db_with_system_flag(self, conn):
        seed_default_agents(conn)
        row = conn.execute(
            "SELECT system FROM workflow_agents WHERE id = 'agent_validator'"
        ).fetchone()
        assert row is not None
        assert row["system"] == 1

    def test_agent_default_setting_seeded_to_orchestrator(self, conn):
        seed_default_agents(conn)
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'agent.default'"
        ).fetchone()
        assert row is not None
        assert row["value"] == "agent_orchestrator"

    def test_seeding_agents_is_idempotent(self, conn):
        first = seed_default_agents(conn)
        second = seed_default_agents(conn)
        assert second["inserted"] == 0
        # All previously inserted agents are found as existing on second run.
        assert second["existing"] == first["inserted"] + first.get("existing", 0)

    def test_done_learnings_seeded_disabled(self, conn):
        seed_default_workflows(conn, PROJECT_ID)
        row = conn.execute(
            "SELECT enabled FROM workflows WHERE name = 'Done → Learnings extraction' AND system = 1"
        ).fetchone()
        assert row is not None
        assert row["enabled"] == 0

    def test_sprint_tag_rotation_seeded_disabled(self, conn):
        seed_default_workflows(conn, PROJECT_ID)
        row = conn.execute(
            "SELECT enabled FROM workflows WHERE name = 'Sprint tag rotation' AND system = 1"
        ).fetchone()
        assert row is not None
        assert row["enabled"] == 0


# ===========================================================================
# 8. evaluate_trigger integration: tag predicates wired into the catalog
# ===========================================================================


class TestTagPredicatesViaEvaluateTrigger:
    def test_has_tag_trigger_passes_when_tag_present(self, conn):
        _make_ticket(conn)
        _add_tag(conn, "B-1", "sprint-current")
        ctx = _make_ctx(conn)
        trigger = {"all_of": [{"kind": "has_tag", "value": ["sprint-current"]}]}
        passed, failures = evaluate_trigger(trigger, ctx)
        assert passed is True
        assert not failures

    def test_lacks_tag_trigger_passes_when_tag_absent(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        trigger = {"all_of": [{"kind": "lacks_tag", "value": ["sprint-prev"]}]}
        passed, failures = evaluate_trigger(trigger, ctx)
        assert passed is True

    def test_sprint_tag_rotation_trigger_fires_on_matching_ticket(self, conn):
        """Sprint tag rotation's trigger should pass on a ticket with sprint-current
        and automation_mode='auto' (trigger requires all three: has_tag, automation_mode=auto,
        no_active_run)."""
        _make_ticket(conn)
        _add_tag(conn, "B-1", "sprint-current")
        # The trigger includes automation_mode=auto; insert the automation_subjects row.
        conn.execute(
            "INSERT INTO automation_subjects (project_id, subject_type, subject_id, automation_mode) "
            "VALUES (?, 'ticket', 'B-1', 'auto')",
            (PROJECT_ID,),
        )
        conn.commit()
        ctx = _make_ctx(conn)
        wf = next(wf for wf in DEFAULT_WORKFLOWS if wf["name"] == "Sprint tag rotation")
        passed, failures = evaluate_trigger(wf["trigger_json"], ctx)
        assert passed is True

    def test_sprint_tag_rotation_trigger_blocks_when_tag_absent(self, conn):
        _make_ticket(conn)
        ctx = _make_ctx(conn)
        wf = next(wf for wf in DEFAULT_WORKFLOWS if wf["name"] == "Sprint tag rotation")
        passed, failures = evaluate_trigger(wf["trigger_json"], ctx)
        assert passed is False
