"""TDD tests for OpenSpec surfacing: derive_spec_status, filesystem probes,
spec_status_in predicate, catalog/describe wiring, and serve helpers.

Pure logic — no server process, no openspec CLI subprocess.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from constants import SPEC_STATUSES
from db import init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c


def _ticket(conn, tid="B-13", project_id="proj", title="Dashboard run now"):
    conn.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description, priority) "
        "VALUES (?, ?, ?, 'Backlog', 'specified', 'desc', 'medium')",
        (tid, project_id, title),
    )


def _set_spec_flag(conn, tid, project_id, content, set_by="cli:spec"):
    conn.execute(
        """
        INSERT INTO readiness_flags (ticket_id, project_id, flag, content, set_by)
        VALUES (?, ?, 'spec', ?, ?)
        ON CONFLICT (ticket_id, project_id, flag)
        DO UPDATE SET content = excluded.content, set_by = excluded.set_by
        """,
        (tid, project_id, content, set_by),
    )


def _make_change_tree(root: Path, live_names: list[str], archive_names: list[str] | None = None):
    changes = root / "openspec" / "changes"
    changes.mkdir(parents=True)
    for name in live_names:
        d = changes / name
        d.mkdir()
        (d / "proposal.md").write_text(f"# {name}\n", encoding="utf-8")
        specs = d / "specs" / "foo"
        specs.mkdir(parents=True)
        (specs / "spec.md").write_text("## ADDED\n", encoding="utf-8")
    if archive_names:
        arch = changes / "archive"
        arch.mkdir()
        for name in archive_names:
            d = arch / name
            d.mkdir()
            (d / "proposal.md").write_text(f"# archived {name}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. derive_spec_status matrix
# ---------------------------------------------------------------------------


class TestDeriveSpecStatus:
    def test_all_eight_statuses_produced(self):
        from actions import derive_spec_status

        cases = {
            "undeclared": ("", "", False, False, []),
            "unrecorded_change": ("", "", False, False, ["b-13-x"]),
            "declared_invalid": ("garbage", "", False, False, []),
            "no_delta": ("C:none - rename only", "", False, False, []),
            "linked": ("B:b-13-x", "", True, False, []),
            "linked_missing": ("B:b-13-x", "", False, False, []),
            "archived": ("B:b-13-x", "", False, True, []),
            "forced": ("B:b-13-x", "accept:--force", True, False, []),
        }
        produced = set()
        for expected, args in cases.items():
            got = derive_spec_status(*args)
            assert got == expected, f"expected {expected}, got {got} for {args}"
            produced.add(got)
        assert produced == set(SPEC_STATUSES)

    def test_forced_beats_linked_live_dir(self):
        from actions import derive_spec_status

        assert (
            derive_spec_status(
                "B:b-13-x",
                "accept:--force",
                change_exists_live=True,
                change_has_archive=False,
                discovered=["b-13-x"],
            )
            == "forced"
        )

    def test_archived_beats_linked_missing(self):
        from actions import derive_spec_status

        assert (
            derive_spec_status(
                "B:b-13-x",
                "",
                change_exists_live=False,
                change_has_archive=True,
                discovered=[],
            )
            == "archived"
        )

    def test_set_by_force_alone_triggers_forced(self):
        from actions import derive_spec_status

        assert (
            derive_spec_status(
                "B:b-13-x",
                "accept:--force",
                False,
                False,
                [],
            )
            == "forced"
        )

    def test_note_prefix_force_alone_triggers_forced(self):
        from actions import derive_spec_status

        assert (
            derive_spec_status(
                "C:none - accepted with --force: ship it",
                "cli:spec",
                False,
                False,
                [],
            )
            == "forced"
        )

    def test_lane_with_empty_change_is_declared_invalid(self):
        from actions import derive_spec_status

        assert (
            derive_spec_status("B:", "", False, False, []) == "declared_invalid"
        )

    def test_tuple_order_stable(self):
        assert SPEC_STATUSES == (
            "undeclared",
            "unrecorded_change",
            "declared_invalid",
            "no_delta",
            "linked",
            "linked_missing",
            "archived",
            "forced",
        )


# ---------------------------------------------------------------------------
# 2. matching_change_dirs
# ---------------------------------------------------------------------------


class TestMatchingChangeDirs:
    def test_b13_exact_match_excludes_neighbours_and_archive(self, tmp_path):
        import openspec_adapter as osa

        _make_change_tree(
            tmp_path,
            live_names=[
                "b-1-x",
                "b-13-y",
                "b-130-z",
                "b-13-dashboard-run-now-2026-08-02",
            ],
            archive_names=["2026-01-01-b-13-old"],
        )
        got = osa.matching_change_dirs(tmp_path, "B-13")
        assert got == [
            "b-13-dashboard-run-now-2026-08-02",
            "b-13-y",
        ]
        assert "b-1-x" not in got
        assert "b-130-z" not in got
        assert "2026-01-01-b-13-old" not in got

    def test_empty_when_no_changes_root(self, tmp_path):
        import openspec_adapter as osa

        assert osa.matching_change_dirs(tmp_path, "B-13") == []


# ---------------------------------------------------------------------------
# 3. Containment for read/write_change_doc
# ---------------------------------------------------------------------------


class TestChangeDocContainment:
    @pytest.fixture
    def live_project(self, tmp_path):
        _make_change_tree(tmp_path, live_names=["b-13-y"])
        # also place an archive-only change for archived-write tests
        arch = tmp_path / "openspec" / "changes" / "archive" / "2026-01-01-b-99-only"
        arch.mkdir(parents=True)
        (arch / "proposal.md").write_text("archived\n", encoding="utf-8")
        return tmp_path

    def test_reject_parent_escape(self, live_project):
        import openspec_adapter as osa

        with pytest.raises(ValueError):
            osa.read_change_doc(live_project, "b-13-y", "../x.md")
        with pytest.raises(ValueError):
            osa.write_change_doc(live_project, "b-13-y", "../x.md", "nope")

    def test_reject_absolute_path(self, live_project):
        import openspec_adapter as osa

        abs_path = str(live_project / "openspec" / "changes" / "b-13-y" / "proposal.md")
        with pytest.raises(ValueError):
            osa.read_change_doc(live_project, "b-13-y", abs_path)

    def test_reject_symlink_escape(self, live_project):
        import openspec_adapter as osa

        change = live_project / "openspec" / "changes" / "b-13-y"
        outside = live_project / "secret.md"
        outside.write_text("secret\n", encoding="utf-8")
        link = change / "escape.md"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks not supported")
        with pytest.raises(ValueError):
            osa.read_change_doc(live_project, "b-13-y", "escape.md")

    def test_reject_non_md(self, live_project):
        import openspec_adapter as osa

        change = live_project / "openspec" / "changes" / "b-13-y"
        (change / "notes.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            osa.read_change_doc(live_project, "b-13-y", "notes.txt")

    def test_reject_nonexistent(self, live_project):
        import openspec_adapter as osa

        with pytest.raises(FileNotFoundError):
            osa.read_change_doc(live_project, "b-13-y", "missing.md")

    def test_write_archived_raises(self, live_project):
        import openspec_adapter as osa

        with pytest.raises(osa.ArchivedChangeError):
            osa.write_change_doc(
                live_project, "b-99-only", "proposal.md", "overwrite"
            )

    def test_round_trip_nested_doc(self, live_project):
        import openspec_adapter as osa

        content = "## ADDED Requirements\n- hello\n"
        osa.write_change_doc(live_project, "b-13-y", "specs/foo/spec.md", content)
        assert osa.read_change_doc(live_project, "b-13-y", "specs/foo/spec.md") == content

    def test_change_docs_order(self, live_project):
        import openspec_adapter as osa

        change = live_project / "openspec" / "changes" / "b-13-y"
        (change / "design.md").write_text("d\n", encoding="utf-8")
        (change / "tasks.md").write_text("t\n", encoding="utf-8")
        docs = osa.change_docs(change)
        assert docs[0] == "proposal.md"
        assert "design.md" in docs
        assert "tasks.md" in docs
        assert "specs/foo/spec.md" in docs
        # proposal, design, tasks first in that order
        assert docs.index("proposal.md") < docs.index("design.md") < docs.index("tasks.md")
        assert docs.index("tasks.md") < docs.index("specs/foo/spec.md")

    def test_resolve_prefers_live_then_newest_archive(self, tmp_path):
        import openspec_adapter as osa

        _make_change_tree(
            tmp_path,
            live_names=["b-1-live"],
            archive_names=["2026-01-01-b-2-old", "2026-06-01-b-2-old"],
        )
        live = osa.resolve_change_dir(tmp_path, "b-1-live")
        assert live is not None and live[1] is False
        arch = osa.resolve_change_dir(tmp_path, "b-2-old")
        assert arch is not None and arch[1] is True
        assert arch[0].name == "2026-06-01-b-2-old"
        assert osa.resolve_change_dir(tmp_path, "missing") is None


# ---------------------------------------------------------------------------
# 4. spec_status integration
# ---------------------------------------------------------------------------


class TestSpecStatusIntegration:
    def test_flag_disk_combinations(self, conn, tmp_path, monkeypatch):
        import actions
        import openspec_adapter as osa

        project_id = "proj"
        tid = "B-13"
        _ticket(conn, tid=tid, project_id=project_id)
        _make_change_tree(
            tmp_path,
            live_names=[
                "b-13-linked",
                "b-13-drift",
            ],
            archive_names=["2026-01-01-b-13-archived"],
        )
        monkeypatch.setattr(actions, "project_path_for", lambda pid: str(tmp_path))

        # undeclared
        info = actions.spec_status(conn, project_id, tid)
        # two live dirs matching B-13 → unrecorded_change
        assert info["status"] == "unrecorded_change"
        assert set(info["unrecorded"]) == {"b-13-linked", "b-13-drift"}
        assert info["link"] is None

        # linked + drift dirs remain unrecorded
        _set_spec_flag(conn, tid, project_id, "B:b-13-linked")
        info = actions.spec_status(conn, project_id, tid)
        assert info["status"] == "linked"
        assert info["unrecorded"] == ["b-13-drift"]
        assert info["link"]["lane"] == "B"
        assert info["link"]["change"] == "b-13-linked"
        assert info["set_by"] == "cli:spec"
        assert "linked" in info["detail"].lower() or "b-13-linked" in info["detail"]

        # linked_missing
        _set_spec_flag(conn, tid, project_id, "B:b-13-gone")
        info = actions.spec_status(conn, project_id, tid)
        assert info["status"] == "linked_missing"
        assert "b-13-linked" in info["unrecorded"]

        # archived (no live)
        _set_spec_flag(conn, tid, project_id, "B:b-13-archived")
        info = actions.spec_status(conn, project_id, tid)
        assert info["status"] == "archived"

        # forced via set_by
        _set_spec_flag(
            conn, tid, project_id, "B:b-13-linked", set_by="accept:--force"
        )
        info = actions.spec_status(conn, project_id, tid)
        assert info["status"] == "forced"

        # unknown project path → derive from flag alone
        monkeypatch.setattr(actions, "project_path_for", lambda pid: "")
        _set_spec_flag(conn, tid, project_id, "B:b-13-linked", set_by="cli:spec")
        info = actions.spec_status(conn, project_id, tid)
        assert info["status"] == "linked_missing"  # no disk → not live, not archive
        assert info["unrecorded"] == []

        # silence unused import warning if any
        assert osa.change_exists(tmp_path, "b-13-linked")


# ---------------------------------------------------------------------------
# 5. spec_status_in evaluator (subprocess-free)
# ---------------------------------------------------------------------------


class TestSpecStatusInEvaluator:
    def test_true_and_false_subprocess_free(self, conn, tmp_path, monkeypatch):
        import actions
        import openspec_adapter as osa
        from conditions import evaluate_condition

        project_id = "proj"
        tid = "B-13"
        _ticket(conn, tid=tid, project_id=project_id)
        _make_change_tree(tmp_path, live_names=["b-13-y"])
        _set_spec_flag(conn, tid, project_id, "B:b-13-y")
        monkeypatch.setattr(actions, "project_path_for", lambda pid: str(tmp_path))

        def _boom(*_a, **_k):
            raise AssertionError("openspec_adapter._run must not be called")

        monkeypatch.setattr(osa, "_run", _boom)

        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ? AND project_id = ?",
            (tid, project_id),
        ).fetchone()
        ctx = {
            "ticket": dict(row),
            "ticket_row": row,
            "project_id": project_id,
            "db": conn,
            "active_run": False,
            "automation_subject": None,
        }

        ok, reason = evaluate_condition(
            {"kind": "spec_status_in", "values": ["linked", "archived"]}, ctx
        )
        assert ok is True
        assert "linked" in reason

        ok, reason = evaluate_condition(
            {"kind": "spec_status_in", "values": ["undeclared"]}, ctx
        )
        assert ok is False
        assert "linked" in reason
        assert "undeclared" in reason or "not in" in reason


# ---------------------------------------------------------------------------
# 6. Catalog + describe
# ---------------------------------------------------------------------------


class TestCatalogAndDescribe:
    def test_ui_catalog_options_and_filter_op(self):
        from conditions import ui_catalog

        cat = ui_catalog()
        assert cat["options"]["spec_statuses"] == list(SPEC_STATUSES)
        assert cat["predicate_to_attribute"]["spec_status_in"] == "spec"
        spec_attr = next(a for a in cat["attributes"] if a["key"] == "spec")
        op = next(o for o in spec_attr["filter_ops"] if o["key"] == "status_is_one_of")
        assert op["predicate_kind"] == "spec_status_in"
        assert op["value_control"] == "spec_status_multi_select"

    def test_describe_trigger_english(self):
        from trigger_describe import describe_trigger

        text = describe_trigger(
            {"kind": "spec_status_in", "values": ["linked", "archived"]}
        )
        assert "linked" in text
        assert "archived" in text
        assert "(spec_status_in)" not in text


# ---------------------------------------------------------------------------
# 7. Endpoint helpers
# ---------------------------------------------------------------------------


class _ConnProxy:
    """Delegate to a real connection but ignore close() so helpers don't kill
    the shared in-memory test DB mid-assert."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _NopLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestEndpointHelpers:
    def test_spec_tab_payload_suggested_content(self, conn, tmp_path, monkeypatch):
        import actions
        import serve

        project_id = "proj"
        tid = "B-13"
        _ticket(conn, tid=tid, project_id=project_id)
        _make_change_tree(tmp_path, live_names=["b-13-y", "b-13-z"])
        monkeypatch.setattr(actions, "project_path_for", lambda pid: str(tmp_path))

        proxy = _ConnProxy(conn)
        monkeypatch.setattr(serve, "get_db", lambda: proxy)
        monkeypatch.setattr(serve, "init_db", lambda c: None)
        monkeypatch.setattr(serve, "_db_lock", _NopLock())

        proj = {"id": project_id, "path": str(tmp_path)}
        payload = serve._spec_tab_payload(proj, tid)
        assert payload is not None
        assert payload["status"] == "unrecorded_change"
        names = {u["name"] for u in payload["unrecorded"]}
        assert names == {"b-13-y", "b-13-z"}
        for u in payload["unrecorded"]:
            assert u["suggested_content"] == f"B:{u['name']}"

    def test_doc_write_emits_activity_event(self, conn, tmp_path, monkeypatch):
        import actions
        import serve

        project_id = "proj"
        tid = "B-13"
        _ticket(conn, tid=tid, project_id=project_id)
        _make_change_tree(tmp_path, live_names=["b-13-y"])
        _set_spec_flag(conn, tid, project_id, "B:b-13-y")
        conn.commit()
        monkeypatch.setattr(actions, "project_path_for", lambda pid: str(tmp_path))
        proxy = _ConnProxy(conn)
        monkeypatch.setattr(serve, "get_db", lambda: proxy)
        monkeypatch.setattr(serve, "init_db", lambda c: None)
        monkeypatch.setattr(serve, "_db_lock", _NopLock())

        proj = {"id": project_id, "path": str(tmp_path)}
        body, status = serve._spec_doc_write(
            proj, tid, "proposal.md", "# updated proposal\n"
        )
        assert status == 200
        assert body == {"ok": True}
        row = conn.execute(
            "SELECT event_kind, payload_json FROM activity_events "
            "WHERE subject_id = ? AND event_kind = 'spec_doc_edited'",
            (tid,),
        ).fetchone()
        assert row is not None
        assert "b-13-y" in row["payload_json"]
        assert "proposal.md" in row["payload_json"]

    def test_archived_write_maps_to_409(self, conn, tmp_path, monkeypatch):
        import actions
        import serve

        project_id = "proj"
        tid = "B-99"
        _ticket(conn, tid=tid, project_id=project_id)
        arch_root = tmp_path / "openspec" / "changes" / "archive"
        d = arch_root / "2026-01-01-b-99-only"
        d.mkdir(parents=True)
        (d / "proposal.md").write_text("archived\n", encoding="utf-8")
        _set_spec_flag(conn, tid, project_id, "B:b-99-only")
        conn.commit()
        monkeypatch.setattr(actions, "project_path_for", lambda pid: str(tmp_path))
        proxy = _ConnProxy(conn)
        monkeypatch.setattr(serve, "get_db", lambda: proxy)
        monkeypatch.setattr(serve, "init_db", lambda c: None)
        monkeypatch.setattr(serve, "_db_lock", _NopLock())

        proj = {"id": project_id, "path": str(tmp_path)}
        body, status = serve._spec_doc_write(
            proj, tid, "proposal.md", "should fail\n"
        )
        assert status == 409
        assert body.get("error") == "archived change is read-only"
