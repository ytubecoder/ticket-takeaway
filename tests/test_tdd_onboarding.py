"""TDD tests for project onboarding: seed_project, scaffold_project, _get_managed_files."""

import os
import sqlite3

import pytest
from conftest import cli_mod

from db import init_db

seed_project = cli_mod.seed_project
scaffold_project = cli_mod.scaffold_project


# ===========================================================================
# seed_project
# ===========================================================================


@pytest.fixture
def temp_project(tmp_path):
    """Create a temp project dir and return a project dict + in-memory DB."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    project = {
        "id": "test-proj",
        "name": "Test Project",
        "path": str(tmp_path),
        "active": True,
    }
    return project, conn, tmp_path


def test_seed_project_with_valid_backlog(temp_project):
    """seed_project imports tickets from a well-formed PRODUCT_BACKLOG.md."""
    project, conn, tmp_path = temp_project
    backlog = tmp_path / "PRODUCT_BACKLOG.md"
    backlog.write_text(
        "# Product Backlog\n\n"
        "## Backlog\n\n"
        "### B-01: First ticket\n"
        "Priority: high | Status: proposed\n"
        "A description.\n"
        "- [ ] Criterion one\n\n"
        "### B-02: Second ticket\n"
        "Priority: low | Status: proposed\n\n",
        encoding="utf-8",
    )
    count = seed_project(conn, project)
    assert count == 2
    rows = conn.execute(
        "SELECT id FROM tickets WHERE project_id = ?", ("test-proj",)
    ).fetchall()
    assert len(rows) == 2


def test_seed_project_no_backlog(temp_project):
    """seed_project returns 0 when no PRODUCT_BACKLOG.md exists."""
    project, conn, _tmp_path = temp_project
    count = seed_project(conn, project)
    assert count == 0


def test_seed_project_empty_backlog(temp_project):
    """seed_project returns 0 for an empty backlog file."""
    project, conn, tmp_path = temp_project
    (tmp_path / "PRODUCT_BACKLOG.md").write_text("# Empty\n\n", encoding="utf-8")
    count = seed_project(conn, project)
    assert count == 0


def test_seed_project_is_idempotent(temp_project):
    """Running seed_project twice doesn't duplicate tickets."""
    project, conn, tmp_path = temp_project
    backlog = tmp_path / "PRODUCT_BACKLOG.md"
    backlog.write_text(
        "# Backlog\n\n## Backlog\n\n### B-01: Ticket\nPriority: medium | Status: proposed\n\n",
        encoding="utf-8",
    )
    seed_project(conn, project)
    seed_project(conn, project)
    rows = conn.execute(
        "SELECT id FROM tickets WHERE project_id = ?", ("test-proj",)
    ).fetchall()
    assert len(rows) == 1


# ===========================================================================
# scaffold_project
# ===========================================================================


def test_scaffold_creates_backlog(temp_project):
    """scaffold_project creates PRODUCT_BACKLOG.md when it doesn't exist."""
    project, conn, tmp_path = temp_project
    scaffold_project(conn, project)
    backlog = tmp_path / "PRODUCT_BACKLOG.md"
    assert backlog.exists()
    content = backlog.read_text(encoding="utf-8")
    assert "# Product Backlog" in content
    # Should have section headers
    assert "## Backlog" in content
    assert "## Ideas" in content


def test_scaffold_creates_spec(temp_project):
    """scaffold_project creates PRODUCT_SPECIFICATION.md when it doesn't exist."""
    project, conn, tmp_path = temp_project
    scaffold_project(conn, project)
    spec = tmp_path / "PRODUCT_SPECIFICATION.md"
    assert spec.exists()
    content = spec.read_text(encoding="utf-8")
    assert "Product Specification" in content
    assert "Test Project" in content


def test_scaffold_does_not_overwrite_existing(temp_project):
    """scaffold_project preserves existing files."""
    project, conn, tmp_path = temp_project
    backlog = tmp_path / "PRODUCT_BACKLOG.md"
    backlog.write_text("# My custom backlog\n", encoding="utf-8")
    spec = tmp_path / "PRODUCT_SPECIFICATION.md"
    spec.write_text("# My custom spec\n", encoding="utf-8")
    scaffold_project(conn, project)
    assert backlog.read_text(encoding="utf-8") == "# My custom backlog\n"
    assert spec.read_text(encoding="utf-8") == "# My custom spec\n"


# ===========================================================================
# _get_managed_files (via serve.py)
# ===========================================================================

# Import serve module's function
import importlib.util

_serve_path = os.path.join(os.path.dirname(__file__), "..", "src", "serve.py")
_serve_spec = importlib.util.spec_from_file_location("serve", _serve_path)
# We can't easily import serve.py (it has side effects), so test the data structure directly
# Instead, test the constant and logic inline

from serve import _MANAGED_FILES, _get_managed_files


def test_managed_files_constant_has_entries():
    """_MANAGED_FILES has the expected entries."""
    paths = [m[0] for m in _MANAGED_FILES]
    assert "PRODUCT_BACKLOG.md" in paths
    assert "PRODUCT_SPECIFICATION.md" in paths
    assert "docs/sdlc-dashboard.html" in paths


def test_get_managed_files_returns_correct_structure(tmp_path):
    """_get_managed_files returns dicts with path, description, exists, gitignored."""
    project = {"id": "test", "path": str(tmp_path)}
    result = _get_managed_files(project)
    assert isinstance(result, list)
    assert len(result) == len(_MANAGED_FILES)
    for item in result:
        assert "path" in item
        assert "description" in item
        assert "exists" in item
        assert "gitignored" in item


def test_get_managed_files_detects_existing(tmp_path):
    """_get_managed_files correctly detects existing files."""
    project = {"id": "test", "path": str(tmp_path)}
    (tmp_path / "PRODUCT_BACKLOG.md").write_text("# test\n", encoding="utf-8")
    result = _get_managed_files(project)
    backlog_entry = next(f for f in result if f["path"] == "PRODUCT_BACKLOG.md")
    assert backlog_entry["exists"] is True
    assert backlog_entry["gitignored"] is False
    spec_entry = next(f for f in result if f["path"] == "PRODUCT_SPECIFICATION.md")
    assert spec_entry["exists"] is False
