"""TDD tests for auto_promote_parents() logic.

Verifies that parent tickets are promoted to For Review when all
their children have resolved statuses, and NOT promoted otherwise.
"""

import pytest
from conftest import gen_mod

Ticket = gen_mod.Ticket
auto_promote_parents = gen_mod.auto_promote_parents


def _make_ticket(id, status="in-progress", section="WIP", parent=None):
    """Helper to create a minimal Ticket for testing."""
    return Ticket(
        id=id, title=f"Test {id}", status=status, section=section, parent=parent
    )


# ---------------------------------------------------------------------------
# Promotion: all children resolved
# ---------------------------------------------------------------------------


def test_all_children_resolved_promotes_parent():
    """Parent in WIP moves to For Review when all children are for-review/bug-fixed/done."""
    parent = _make_ticket("P-01", status="in-progress", section="WIP")
    children = [
        _make_ticket("C-01", status="for-review", parent="P-01"),
        _make_ticket("C-02", status="bug-fixed", parent="P-01"),
        _make_ticket("C-03", status="done", parent="P-01"),
    ]
    by_section = {"WIP": [parent], "Backlog": [], "Bugs": [], "For Review": []}
    child_map = {"P-01": children}

    promoted = auto_promote_parents(by_section, child_map)

    assert "P-01" in promoted
    assert parent not in by_section["WIP"]
    assert parent in by_section["For Review"]


def test_returns_promoted_ids():
    """Return value is the set of promoted parent IDs."""
    p1 = _make_ticket("P-01", section="WIP")
    p2 = _make_ticket("P-02", section="Backlog")
    by_section = {
        "WIP": [p1],
        "Backlog": [p2],
        "Bugs": [],
        "For Review": [],
    }
    child_map = {
        "P-01": [_make_ticket("C-01", status="done", parent="P-01")],
        "P-02": [_make_ticket("C-02", status="done", parent="P-02")],
    }

    promoted = auto_promote_parents(by_section, child_map)

    assert promoted == {"P-01", "P-02"}


# ---------------------------------------------------------------------------
# No promotion: unresolved children
# ---------------------------------------------------------------------------


def test_one_child_unresolved_blocks_promotion():
    """Parent NOT promoted if any child still in progress."""
    parent = _make_ticket("P-01", section="WIP")
    children = [
        _make_ticket("C-01", status="done", parent="P-01"),
        _make_ticket("C-02", status="in-progress", parent="P-01"),
    ]
    by_section = {"WIP": [parent], "Backlog": [], "Bugs": [], "For Review": []}
    child_map = {"P-01": children}

    promoted = auto_promote_parents(by_section, child_map)

    assert "P-01" not in promoted
    assert parent in by_section["WIP"]


def test_parent_no_children_never_promoted():
    """Parent with empty children list is never promoted."""
    parent = _make_ticket("P-01", section="WIP")
    by_section = {"WIP": [parent], "Backlog": [], "Bugs": [], "For Review": []}

    # No children at all — empty child_map
    promoted = auto_promote_parents(by_section, {})

    assert len(promoted) == 0
    assert parent in by_section["WIP"]


# ---------------------------------------------------------------------------
# Idempotency: already in For Review/Done/Icebox
# ---------------------------------------------------------------------------


def test_parent_already_in_review_not_moved():
    """Parent already in For Review stays put (not duplicated)."""
    parent = _make_ticket("P-01", status="for-review", section="For Review")
    children = [_make_ticket("C-01", status="done", parent="P-01")]
    by_section = {"WIP": [], "Backlog": [], "Bugs": [], "For Review": [parent]}
    child_map = {"P-01": children}

    promoted = auto_promote_parents(by_section, child_map)

    # Not in promoted set because it wasn't moved FROM WIP/Backlog/Bugs
    assert "P-01" not in promoted
    # Still in For Review, only once
    assert by_section["For Review"].count(parent) == 1


def test_parent_in_done_not_promoted():
    """Parent in Done is not checked for promotion."""
    parent = _make_ticket("P-01", status="done", section="Done")
    children = [_make_ticket("C-01", status="done", parent="P-01")]
    by_section = {
        "WIP": [],
        "Backlog": [],
        "Bugs": [],
        "For Review": [],
        "Done": [parent],
    }
    child_map = {"P-01": children}

    promoted = auto_promote_parents(by_section, child_map)

    assert "P-01" not in promoted
    assert parent in by_section["Done"]


# ---------------------------------------------------------------------------
# Source sections: promotes from WIP, Backlog, and Bugs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_section", ["WIP", "Backlog", "Bugs"])
def test_promotes_from_source_sections(source_section):
    """Parent is promoted from each of the 3 source sections (WIP, Backlog, Bugs)."""
    parent = _make_ticket("P-01", section=source_section)
    children = [_make_ticket("C-01", status="done", parent="P-01")]
    by_section = {"WIP": [], "Backlog": [], "Bugs": [], "For Review": []}
    by_section[source_section] = [parent]
    child_map = {"P-01": children}

    promoted = auto_promote_parents(by_section, child_map)

    assert "P-01" in promoted
    assert parent not in by_section[source_section]
    assert parent in by_section["For Review"]
