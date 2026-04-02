"""TDD tests for auto_promote_parents() logic.

Verifies that parent tickets are promoted to the review column when all
their children have resolved statuses, and NOT promoted otherwise.
"""

import pytest
from conftest import gen_mod

Ticket = gen_mod.Ticket
auto_promote_parents = gen_mod.auto_promote_parents


def _make_ticket(id, status="in-progress", column="wip", parent=None):
    """Helper to create a minimal Ticket for testing."""
    return Ticket(id=id, title=f"Test {id}", status=status, column=column, parent=parent)


# ---------------------------------------------------------------------------
# Promotion: all children resolved
# ---------------------------------------------------------------------------


def test_all_children_resolved_promotes_parent():
    """Parent in WIP moves to review when all children are for-review/bug-fixed/done."""
    parent = _make_ticket("P-01", status="in-progress", column="wip")
    children = [
        _make_ticket("C-01", status="for-review", parent="P-01"),
        _make_ticket("C-02", status="bug-fixed", parent="P-01"),
        _make_ticket("C-03", status="done", parent="P-01"),
    ]
    by_column = {"wip": [parent], "backlog": [], "bugs": [], "review": []}
    child_map = {"P-01": children}

    promoted = auto_promote_parents(by_column, child_map)

    assert "P-01" in promoted
    assert parent not in by_column["wip"]
    assert parent in by_column["review"]


def test_returns_promoted_ids():
    """Return value is the set of promoted parent IDs."""
    p1 = _make_ticket("P-01", column="wip")
    p2 = _make_ticket("P-02", column="backlog")
    by_column = {
        "wip": [p1], "backlog": [p2], "bugs": [], "review": [],
    }
    child_map = {
        "P-01": [_make_ticket("C-01", status="done", parent="P-01")],
        "P-02": [_make_ticket("C-02", status="done", parent="P-02")],
    }

    promoted = auto_promote_parents(by_column, child_map)

    assert promoted == {"P-01", "P-02"}


# ---------------------------------------------------------------------------
# No promotion: unresolved children
# ---------------------------------------------------------------------------


def test_one_child_unresolved_blocks_promotion():
    """Parent NOT promoted if any child still in progress."""
    parent = _make_ticket("P-01", column="wip")
    children = [
        _make_ticket("C-01", status="done", parent="P-01"),
        _make_ticket("C-02", status="in-progress", parent="P-01"),
    ]
    by_column = {"wip": [parent], "backlog": [], "bugs": [], "review": []}
    child_map = {"P-01": children}

    promoted = auto_promote_parents(by_column, child_map)

    assert "P-01" not in promoted
    assert parent in by_column["wip"]


def test_parent_no_children_never_promoted():
    """Parent with empty children list is never promoted."""
    parent = _make_ticket("P-01", column="wip")
    by_column = {"wip": [parent], "backlog": [], "bugs": [], "review": []}

    # No children at all — empty child_map
    promoted = auto_promote_parents(by_column, {})

    assert len(promoted) == 0
    assert parent in by_column["wip"]


# ---------------------------------------------------------------------------
# Idempotency: already in review/done/icebox
# ---------------------------------------------------------------------------


def test_parent_already_in_review_not_moved():
    """Parent already in review column stays put (not duplicated)."""
    parent = _make_ticket("P-01", status="for-review", column="review")
    children = [_make_ticket("C-01", status="done", parent="P-01")]
    by_column = {"wip": [], "backlog": [], "bugs": [], "review": [parent]}
    child_map = {"P-01": children}

    promoted = auto_promote_parents(by_column, child_map)

    # Not in promoted set because it wasn't moved FROM wip/backlog/bugs
    assert "P-01" not in promoted
    # Still in review, only once
    assert by_column["review"].count(parent) == 1


def test_parent_in_done_not_promoted():
    """Parent in done column is not checked for promotion."""
    parent = _make_ticket("P-01", status="done", column="done")
    children = [_make_ticket("C-01", status="done", parent="P-01")]
    by_column = {"wip": [], "backlog": [], "bugs": [], "review": [], "done": [parent]}
    child_map = {"P-01": children}

    promoted = auto_promote_parents(by_column, child_map)

    assert "P-01" not in promoted
    assert parent in by_column["done"]


# ---------------------------------------------------------------------------
# Source columns: promotes from wip, backlog, and bugs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_col", ["wip", "backlog", "bugs"])
def test_promotes_from_source_columns(source_col):
    """Parent is promoted from each of the 3 source columns (wip, backlog, bugs)."""
    parent = _make_ticket("P-01", column=source_col)
    children = [_make_ticket("C-01", status="done", parent="P-01")]
    by_column = {"wip": [], "backlog": [], "bugs": [], "review": []}
    by_column[source_col] = [parent]
    child_map = {"P-01": children}

    promoted = auto_promote_parents(by_column, child_map)

    assert "P-01" in promoted
    assert parent not in by_column[source_col]
    assert parent in by_column["review"]
