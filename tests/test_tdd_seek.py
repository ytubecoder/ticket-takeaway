"""TDD tests for the Seek discovery engine: scanners, dedup, and run_seek."""

import sqlite3
from unittest.mock import patch

from db import init_db
from seek import (
    DiscoveredItem,
    deduplicate,
    run_seek,
    scan_changelog_unreleased,
    scan_code_todos,
    scan_github_issues,
    scan_md_tasks,
    scan_readme_todos,
)

# ===========================================================================
# Helpers
# ===========================================================================


def _make_item(
    title="Test item",
    source_type="md_task",
    source_file="test.md",
    source_line=1,
    raw_text="",
    priority="medium",
    section="Ideas",
):
    return DiscoveredItem(
        title=title,
        source_type=source_type,
        source_file=source_file,
        source_line=source_line,
        raw_text=raw_text or title,
        priority=priority,
        section=section,
    )


def _make_db():
    """Create an in-memory DB with schema initialized."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# ===========================================================================
# scan_md_tasks
# ===========================================================================


def test_scan_md_tasks_finds_unchecked(tmp_path):
    """Unchecked markdown tasks are discovered."""
    (tmp_path / "TODO.md").write_text(
        "# Tasks\n\n- [ ] Implement auth\n- [ ] Write tests\n", encoding="utf-8"
    )
    items = scan_md_tasks(str(tmp_path))
    assert len(items) == 2
    assert items[0].title == "Implement auth"
    assert items[1].title == "Write tests"
    assert items[0].source_type == "md_task"
    assert items[0].source_file == "TODO.md"


def test_scan_md_tasks_skips_checked(tmp_path):
    """Checked markdown tasks (- [x]) are ignored."""
    (tmp_path / "TODO.md").write_text(
        "- [x] Done task\n- [ ] Open task\n", encoding="utf-8"
    )
    items = scan_md_tasks(str(tmp_path))
    assert len(items) == 1
    assert items[0].title == "Open task"


def test_scan_md_tasks_skips_backlog(tmp_path):
    """PRODUCT_BACKLOG.md is excluded from scanning."""
    (tmp_path / "PRODUCT_BACKLOG.md").write_text(
        "- [ ] Should be ignored\n", encoding="utf-8"
    )
    items = scan_md_tasks(str(tmp_path))
    assert len(items) == 0


# ===========================================================================
# scan_readme_todos
# ===========================================================================


def test_scan_readme_todos_extracts_section(tmp_path):
    """Bullets under a TODO/Roadmap header are extracted."""
    (tmp_path / "README.md").write_text(
        "# My Project\n\nSome intro.\n\n## TODO\n\n- Add feature X\n- Fix bug Y\n\n## Other\n\nStuff.\n",
        encoding="utf-8",
    )
    items = scan_readme_todos(str(tmp_path))
    assert len(items) == 2
    assert items[0].title == "Add feature X"
    assert items[1].title == "Fix bug Y"
    assert items[0].source_type == "readme_todo"


def test_scan_readme_todos_no_section(tmp_path):
    """README without a TODO-like header returns empty list."""
    (tmp_path / "README.md").write_text(
        "# My Project\n\nJust a description.\n", encoding="utf-8"
    )
    items = scan_readme_todos(str(tmp_path))
    assert len(items) == 0


# ===========================================================================
# scan_code_todos
# ===========================================================================


def test_scan_code_todos_python(tmp_path):
    """Python TODO comments are discovered."""
    (tmp_path / "app.py").write_text(
        "def main():\n    # TODO: implement this\n    pass\n", encoding="utf-8"
    )
    items = scan_code_todos(str(tmp_path))
    assert len(items) == 1
    assert items[0].title == "implement this"
    assert items[0].priority == "medium"
    assert items[0].source_type == "code_todo"


def test_scan_code_todos_fixme_high_priority(tmp_path):
    """FIXME comments get high priority."""
    (tmp_path / "lib.js").write_text("// FIXME: memory leak here\n", encoding="utf-8")
    items = scan_code_todos(str(tmp_path))
    assert len(items) == 1
    assert items[0].priority == "high"
    assert "memory leak" in items[0].title


def test_scan_code_todos_skips_excluded_dirs(tmp_path):
    """Files inside excluded directories are not scanned."""
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("// TODO: should be ignored\n", encoding="utf-8")
    # Also a valid file at root
    (tmp_path / "main.py").write_text("# TODO: should be found\n", encoding="utf-8")
    items = scan_code_todos(str(tmp_path))
    assert len(items) == 1
    assert items[0].source_file == "main.py"


# ===========================================================================
# scan_changelog_unreleased
# ===========================================================================


def test_scan_changelog_unreleased(tmp_path):
    """Items under [Unreleased] header are extracted."""
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- New dashboard widget\n- API rate limiting\n\n## [1.0.0]\n\n- Initial release\n",
        encoding="utf-8",
    )
    items = scan_changelog_unreleased(str(tmp_path))
    assert len(items) == 2
    assert items[0].title == "New dashboard widget"
    assert items[0].source_type == "changelog"


def test_scan_changelog_no_unreleased(tmp_path):
    """No unreleased section returns empty list."""
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.0.0]\n\n- Initial release\n", encoding="utf-8"
    )
    items = scan_changelog_unreleased(str(tmp_path))
    assert len(items) == 0


# ===========================================================================
# scan_github_issues
# ===========================================================================


def test_scan_github_issues_no_gh(tmp_path):
    """When gh CLI is not found, returns empty list without error."""
    with patch("seek.subprocess.run", side_effect=FileNotFoundError("gh not found")):
        items = scan_github_issues(str(tmp_path))
    assert items == []


# ===========================================================================
# deduplicate
# ===========================================================================


def test_deduplicate_exact_match():
    """Existing ticket title blocks duplicate discovery."""
    items = [_make_item("Add Auth")]
    result = deduplicate(
        items, existing_titles=["Add Auth"], existing_draft_descriptions=[]
    )
    assert len(result) == 0


def test_deduplicate_case_insensitive():
    """Title matching is case-insensitive."""
    items = [_make_item("add auth")]
    result = deduplicate(
        items, existing_titles=["Add Auth"], existing_draft_descriptions=[]
    )
    assert len(result) == 0


def test_deduplicate_source_key():
    """Same file:line in existing draft description blocks re-discovery."""
    items = [
        _make_item(
            "New title", source_type="code_todo", source_file="app.py", source_line=10
        )
    ]
    existing_descs = ["Source: code_todo @ app.py:10\n\nold text"]
    result = deduplicate(
        items, existing_titles=[], existing_draft_descriptions=existing_descs
    )
    assert len(result) == 0


def test_deduplicate_within_batch():
    """Duplicate titles within a single scan batch: only first kept."""
    items = [
        _make_item("Fix auth", source_file="a.py", source_line=1),
        _make_item("Fix auth", source_file="b.py", source_line=2),
    ]
    result = deduplicate(items, existing_titles=[], existing_draft_descriptions=[])
    assert len(result) == 1
    assert result[0].source_file == "a.py"


# ===========================================================================
# run_seek (integration with in-memory DB)
# ===========================================================================


def test_run_seek_creates_drafts(tmp_path):
    """run_seek creates draft tickets in the database."""
    conn = _make_db()
    # Create a markdown file with tasks
    (tmp_path / "TODO.md").write_text(
        "- [ ] Build dashboard\n- [ ] Add tests\n", encoding="utf-8"
    )

    result = run_seek(conn, "test-proj", str(tmp_path), sources=["md_task"])
    assert result["created"] == 2
    assert result["discovered"] == 2

    rows = conn.execute(
        "SELECT * FROM tickets WHERE project_id = ? AND draft = 1", ("test-proj",)
    ).fetchall()
    assert len(rows) == 2


def test_run_seek_idempotent(tmp_path):
    """Running seek twice on the same project creates 0 new tickets the second time."""
    conn = _make_db()
    (tmp_path / "TODO.md").write_text("- [ ] Build dashboard\n", encoding="utf-8")

    r1 = run_seek(conn, "test-proj", str(tmp_path), sources=["md_task"])
    assert r1["created"] == 1

    r2 = run_seek(conn, "test-proj", str(tmp_path), sources=["md_task"])
    assert r2["created"] == 0
    assert r2["skipped_duplicates"] == 1


def test_run_seek_source_in_description(tmp_path):
    """Created draft tickets have 'Source: type @ file:line' in description."""
    conn = _make_db()
    (tmp_path / "TODO.md").write_text("- [ ] Implement feature\n", encoding="utf-8")

    run_seek(conn, "test-proj", str(tmp_path), sources=["md_task"])

    row = conn.execute(
        "SELECT description FROM tickets WHERE project_id = ? AND draft = 1",
        ("test-proj",),
    ).fetchone()
    assert row is not None
    assert row["description"].startswith("Source: md_task @ TODO.md:")
