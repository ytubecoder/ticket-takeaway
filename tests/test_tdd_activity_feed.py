"""TDD for the cross-project activity feed (Follow the Action mode)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import constants


class TestTaxonomy:
    # Every event kind emit_event() is actually called with today (grep'd from
    # actions.py / kitchen.py / runners.py / serve.py / tickets-cli.py / seek.py)
    EMITTED_KINDS = {
        "ticket_created", "section_change", "status_change", "criteria_check",
        "field_changed", "gate_override", "mode_changed", "pause_set",
        "pause_cleared", "kitchen_paused", "kitchen_resumed", "run_started",
        "run_stalled", "run_failed", "run_succeeded", "run_cancelled",
        "run_discarded", "workspace_created", "handoff_recorded",
        "agent_output", "hook_started", "hook_succeeded", "hook_failed",
        "input_provided", "pane_linked", "pane_unlinked",
    }

    def test_every_emitted_kind_has_a_group(self):
        missing = self.EMITTED_KINDS - set(constants.EVENT_KIND_GROUPS)
        assert not missing, f"kinds without a group: {sorted(missing)}"

    def test_every_group_has_a_color(self):
        for kind, group in constants.EVENT_KIND_GROUPS.items():
            assert group in constants.EVENT_GROUP_COLORS, (kind, group)

    def test_precedence_is_known_kinds_and_starts_with_moves(self):
        assert constants.FOLLOW_KIND_PRECEDENCE[0] == "section_change"
        assert constants.FOLLOW_KIND_PRECEDENCE[1] == "ticket_created"
        for k in constants.FOLLOW_KIND_PRECEDENCE:
            assert k in constants.EVENT_KIND_GROUPS, k
