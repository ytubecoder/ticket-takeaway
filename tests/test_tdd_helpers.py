"""TDD tests for helper functions: resolve_section, auto_generate_id, compute_dependency_state."""

import sqlite3

import pytest
from conftest import cli_mod, gen_mod
from actions import auto_generate_id
from db import init_db

Ticket = gen_mod.Ticket
compute_dependency_state = gen_mod.compute_dependency_state
resolve_section = cli_mod.resolve_section


# ===========================================================================
# resolve_section
# ===========================================================================


def test_resolve_section_exact():
    """Exact match: 'WIP' → 'WIP'."""
    assert resolve_section("WIP") == "WIP"


def test_resolve_section_case_insensitive():
    """Case-insensitive: 'wip' → 'WIP'."""
    assert resolve_section("wip") == "WIP"


def test_resolve_section_case_insensitive_for_review():
    """Case-insensitive: 'for review' → 'For Review'."""
    assert resolve_section("for review") == "For Review"


def test_resolve_section_alias_review():
    """Alias: 'review' → 'For Review'."""
    assert resolve_section("review") == "For Review"


def test_resolve_section_alias_for_review_hyphenated():
    """Alias: 'for-review' → 'For Review'."""
    assert resolve_section("for-review") == "For Review"


def test_resolve_section_alias_wontdo():
    """Alias: 'wontdo' → \"Won't Do\"."""
    assert resolve_section("wontdo") == "Won't Do"


def test_resolve_section_alias_wont_do_hyphenated():
    """Alias: 'wont-do' → \"Won't Do\"."""
    assert resolve_section("wont-do") == "Won't Do"


def test_resolve_section_unknown_exits():
    """Unknown section name causes SystemExit."""
    with pytest.raises(SystemExit):
        resolve_section("nonexistent-section")


# ===========================================================================
# auto_generate_id
# ===========================================================================


@pytest.fixture()
def mem_db():
    """In-memory SQLite database with schema initialized."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_auto_generate_id_empty_db(mem_db):
    """Empty DB produces first ID: B-01 for Backlog."""
    result = auto_generate_id(mem_db, "test-project", "Backlog")
    assert result == "B-01"


def test_auto_generate_id_increments(mem_db):
    """Existing B-01 → next is B-02."""
    mem_db.execute(
        "INSERT INTO tickets (id, project_id, title) VALUES (?, ?, ?)",
        ("B-01", "test-project", "First"),
    )
    result = auto_generate_id(mem_db, "test-project", "Backlog")
    assert result == "B-02"


def test_auto_generate_id_bug_prefix(mem_db):
    """Bugs section uses 'BUG-' prefix."""
    result = auto_generate_id(mem_db, "test-project", "Bugs")
    assert result == "BUG-01"


def test_auto_generate_id_zero_padded(mem_db):
    """IDs are zero-padded to 2 digits: 'B-01' not 'B-1'."""
    result = auto_generate_id(mem_db, "test-project", "Backlog")
    assert result == "B-01"
    assert "-01" in result  # not "-1"


def test_auto_generate_id_ideas_prefix(mem_db):
    """Ideas section uses 'I-' prefix."""
    result = auto_generate_id(mem_db, "test-project", "Ideas")
    assert result == "I-01"


def test_auto_generate_id_done_prefix(mem_db):
    """Done section uses 'R-' prefix (released)."""
    result = auto_generate_id(mem_db, "test-project", "Done")
    assert result == "R-01"


def test_auto_generate_id_skips_gaps(mem_db):
    """If B-01 and B-03 exist, next is B-04 (uses max, doesn't fill gaps)."""
    mem_db.execute(
        "INSERT INTO tickets (id, project_id, title) VALUES (?, ?, ?)",
        ("B-01", "test-project", "First"),
    )
    mem_db.execute(
        "INSERT INTO tickets (id, project_id, title) VALUES (?, ?, ?)",
        ("B-03", "test-project", "Third"),
    )
    result = auto_generate_id(mem_db, "test-project", "Backlog")
    assert result == "B-04"


# ===========================================================================
# compute_dependency_state
# ===========================================================================


def test_dependency_state_no_deps():
    """Ticket with no deps is always resolved."""
    tickets = [Ticket(id="T-01", title="Test")]
    result = compute_dependency_state(tickets)
    assert result["T-01"]["deps_resolved"] is True
    assert result["T-01"]["blocking_deps"] == []


def test_dependency_state_all_done():
    """All deps in done/released/wont-do → resolved."""
    tickets = [
        Ticket(id="T-01", title="Dep", status="done"),
        Ticket(id="T-02", title="Dep", status="released"),
        Ticket(id="T-03", title="Main", depends=["T-01", "T-02"]),
    ]
    result = compute_dependency_state(tickets)
    assert result["T-03"]["deps_resolved"] is True


def test_dependency_state_one_blocking():
    """One dep in-progress → blocked."""
    tickets = [
        Ticket(id="T-01", title="Done", status="done"),
        Ticket(id="T-02", title="WIP", status="in-progress"),
        Ticket(id="T-03", title="Main", depends=["T-01", "T-02"]),
    ]
    result = compute_dependency_state(tickets)
    assert result["T-03"]["deps_resolved"] is False
    assert result["T-03"]["blocking_deps"] == ["T-02"]


def test_dependency_state_missing_dep():
    """Missing dep (not in ticket list) blocks the ticket."""
    tickets = [
        Ticket(id="T-01", title="Main", depends=["MISSING-01"]),
    ]
    result = compute_dependency_state(tickets)
    assert result["T-01"]["deps_resolved"] is False
    assert result["T-01"]["blocking_deps"] == ["MISSING-01"]


def test_dependency_state_wont_do_resolves():
    """A dep with status 'wont-do' counts as resolved."""
    tickets = [
        Ticket(id="T-01", title="Cancelled", status="wont-do"),
        Ticket(id="T-02", title="Main", depends=["T-01"]),
    ]
    result = compute_dependency_state(tickets)
    assert result["T-02"]["deps_resolved"] is True
