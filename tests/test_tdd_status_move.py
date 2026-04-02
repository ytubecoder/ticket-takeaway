"""TDD tests for compute_status_on_move logic.

Verifies that moving a ticket to a section assigns the correct status and column,
and that commit hash capture is only triggered for Done moves.
"""

import pytest
from conftest import cli_mod


# ---------------------------------------------------------------------------
# Status mapping: each section → expected default status
# ---------------------------------------------------------------------------

STATUS_EXPECTATIONS = [
    ("Ideas", "proposed"),
    ("Backlog", "proposed"),
    ("WIP", "in-progress"),
    ("For Review", "for-review"),
    ("Done", "done"),
    ("Won't Do", "wontdo"),
    ("Icebox", "icebox"),
    ("Bugs", "bug"),
]


@pytest.mark.parametrize("section,expected_status", STATUS_EXPECTATIONS)
def test_status_mapping(section, expected_status):
    """Each section maps to the correct default status."""
    assert cli_mod.DEFAULT_STATUS_BY_SECTION[section] == expected_status


# ---------------------------------------------------------------------------
# Column mapping: each section → expected column slug
# ---------------------------------------------------------------------------

COLUMN_EXPECTATIONS = [
    ("Ideas", "ideas"),
    ("Backlog", "backlog"),
    ("WIP", "wip"),
    ("For Review", "review"),
    ("Done", "done"),
    ("Won't Do", "wontdo"),
    ("Icebox", "icebox"),
    ("Bugs", "bugs"),
]


@pytest.mark.parametrize("section,expected_column", COLUMN_EXPECTATIONS)
def test_column_mapping(section, expected_column):
    """Each section maps to the correct column slug."""
    assert cli_mod.SECTION_TO_COLUMN[section] == expected_column


# ---------------------------------------------------------------------------
# Completeness: every section in SECTION_ORDER has mappings
# ---------------------------------------------------------------------------


def test_all_sections_have_status_mapping():
    """No section in SECTION_ORDER is missing a default status."""
    for section in cli_mod.SECTION_ORDER:
        assert section in cli_mod.DEFAULT_STATUS_BY_SECTION, (
            f"Section '{section}' missing from DEFAULT_STATUS_BY_SECTION"
        )


def test_all_sections_have_column_mapping():
    """No section in SECTION_ORDER is missing a column mapping."""
    for section in cli_mod.SECTION_ORDER:
        assert section in cli_mod.SECTION_TO_COLUMN, (
            f"Section '{section}' missing from SECTION_TO_COLUMN"
        )


# ---------------------------------------------------------------------------
# Commit hash: only captured when moving to Done
# ---------------------------------------------------------------------------


def test_done_is_only_section_capturing_commit():
    """Only the 'Done' section should trigger commit hash capture.

    This is a design rule verified by checking that no other section
    has status 'done' (the status that triggers commit hash capture
    in cmd_move).
    """
    for section, status in cli_mod.DEFAULT_STATUS_BY_SECTION.items():
        if section == "Done":
            assert status == "done"
        else:
            assert status != "done", (
                f"Section '{section}' has status 'done' — only 'Done' should"
            )
