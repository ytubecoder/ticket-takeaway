"""TDD tests for Kitchen M1b — the audit-completion expansion.

Verifies that each direct-write helper in serve.py emits the correct M1b
event with a proper {before, after} payload. Uses a temp DB with the
serve.py module imported once and patched to bypass markdown sync /
dashboard regen filesystem side effects (which would otherwise touch
the user's real registry).
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Fixtures — load serve.py once, patch its filesystem-touching helpers,
# point its DB at a per-test temp file.
# ---------------------------------------------------------------------------

@pytest.fixture
def serve_mod(tmp_path, monkeypatch):
    """Import serve.py with cli.sync/regen stubbed and DB redirected to temp."""
    import importlib.util

    # Redirect the DB BEFORE serve imports (it caches DB_PATH at import time).
    db_file = tmp_path / "tickets.db"
    monkeypatch.setenv("HOME", str(tmp_path))  # keeps any ~/.claude lookups inside tmp
    # Force constants.DB_PATH to point at our temp DB before db.py / serve.py read it.
    import constants
    monkeypatch.setattr(constants, "DB_PATH", db_file)
    monkeypatch.setattr(constants, "DASHBOARD_DIR", tmp_path / ".claude" / "ticket-takeaway")
    (tmp_path / ".claude" / "ticket-takeaway").mkdir(parents=True, exist_ok=True)

    # Reimport db so its module-level imports of constants.DB_PATH are fresh.
    import db
    import importlib
    importlib.reload(db)

    spec = importlib.util.spec_from_file_location("serve_under_test", "src/serve.py")
    serve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(serve)
    # Override get_db so it always uses our temp file (DB_PATH was already cached).
    monkeypatch.setattr(serve, "get_db", lambda: db.get_db(str(db_file)))

    # Stub out filesystem-touching cli helpers so tests don't depend on a real project.
    monkeypatch.setattr(serve.cli, "ingest_markdown", lambda conn, proj: 0)
    monkeypatch.setattr(serve.cli, "sync_to_markdown", lambda conn, proj: None)
    if hasattr(serve.cli, "regenerate_dashboard"):
        monkeypatch.setattr(serve.cli, "regenerate_dashboard", lambda proj: None)

    # Initialise schema.
    conn = serve.get_db()
    serve.init_db(conn)
    conn.close()

    return serve, db_file


def _proj():
    return {"id": "p", "name": "P", "path": "/tmp/no-such-project"}


def _seed(conn, tid="B-1", section="Backlog", status="specified", description="d"):
    conn.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description) "
        "VALUES (?, 'p', ?, ?, ?, ?)",
        (tid, f"Title {tid}", section, status, description),
    )


def _events(conn, subject_id=None, kind=None):
    sql = "SELECT event_kind, actor_type, payload_json FROM activity_events WHERE 1=1"
    args: list = []
    if subject_id is not None:
        sql += " AND subject_id = ?"
        args.append(subject_id)
    if kind is not None:
        sql += " AND event_kind = ?"
        args.append(kind)
    sql += " ORDER BY id"
    return conn.execute(sql, args).fetchall()


# ---------------------------------------------------------------------------
# field_changed (scalar field updates)
# ---------------------------------------------------------------------------

class TestFieldChanged:
    def test_title_emits_field_changed(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        ok = serve._update_ticket_field(_proj(), "B-1", "title", "New Title")
        assert ok
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "field_changed")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload == {"field": "title", "before": "Title B-1", "after": "New Title"}

    def test_status_emits_status_change_not_field_changed(self, serve_mod):
        # status updates use the M1a status_change vocabulary, not field_changed.
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        serve._update_ticket_field(_proj(), "B-1", "status", "ready")
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        kinds = [r["event_kind"] for r in _events(c, "B-1")]
        assert "status_change" in kinds
        assert "field_changed" not in kinds

    def test_no_op_emits_nothing(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        serve._update_ticket_field(_proj(), "B-1", "title", "Title B-1")  # same value
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        assert len(_events(c, "B-1")) == 0


# ---------------------------------------------------------------------------
# Criteria — added / removed / changed
# ---------------------------------------------------------------------------

class TestCriteriaEvents:
    def test_add_emits_criteria_added(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        serve._add_criterion(_proj(), "B-1", "Does a thing")
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "criteria_added")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["text"] == "Does a thing"
        assert "criterion_id" in payload

    def test_remove_emits_criteria_removed(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        serve._add_criterion(_proj(), "B-1", "First crit")
        serve._remove_criterion(_proj(), "B-1", 0)
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "criteria_removed")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["text"] == "First crit"

    def test_text_edit_emits_criteria_changed(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        serve._add_criterion(_proj(), "B-1", "old text")
        serve._update_criterion_text(_proj(), "B-1", 0, "new text")
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "criteria_changed")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload == {"criterion_id": payload["criterion_id"], "before": "old text", "after": "new text"}


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

class TestDependencyChanged:
    def test_emits_with_sorted_before_after(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c, "B-1"); _seed(c, "B-2"); _seed(c, "B-3")
        c.execute("INSERT INTO depends (ticket_id, project_id, depends_on_id) VALUES ('B-1', 'p', 'B-2')")
        c.commit(); c.close()
        serve._update_depends(_proj(), "B-1", ["B-3"])
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "dependency_changed")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload == {"before": ["B-2"], "after": ["B-3"]}

    def test_no_change_emits_nothing(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c, "B-1"); _seed(c, "B-2")
        c.execute("INSERT INTO depends VALUES ('B-1', 'p', 'B-2')")
        c.commit(); c.close()
        serve._update_depends(_proj(), "B-1", ["B-2"])  # same set
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        assert len(_events(c, "B-1", "dependency_changed")) == 0


# ---------------------------------------------------------------------------
# Ticket lifecycle — ticket_created / ticket_deleted
# ---------------------------------------------------------------------------

class TestTicketLifecycle:
    def test_create_emits_ticket_created(self, serve_mod):
        serve, db_file = serve_mod
        result = serve._create_ticket(_proj(), "Brand new", {"section": "Ideas"})
        assert result is not None
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, kind="ticket_created")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["title"] == "Brand new"
        assert payload["section"] == "Ideas"
        assert "id" in payload

    def test_delete_emits_ticket_deleted_with_snapshot(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        ok = serve._delete_ticket(_proj(), "B-1")
        assert ok
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "ticket_deleted")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        snap = payload["snapshot"]
        assert snap["id"] == "B-1"
        assert snap["title"] == "Title B-1"
        assert snap["section"] == "Backlog"


# ---------------------------------------------------------------------------
# Readiness flags
# ---------------------------------------------------------------------------

class TestReadinessChanged:
    def test_set_flag_emits_readiness_changed(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        ok = serve._toggle_readiness(_proj(), "B-1", "tests")
        assert ok
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "readiness_changed")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["flag"] == "tests"
        assert payload["before"]["present"] is False
        assert payload["after"]["present"] is True

    def test_clear_flag_emits_readiness_changed(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        serve._toggle_readiness(_proj(), "B-1", "tests")  # set
        serve._toggle_readiness(_proj(), "B-1", "tests")  # clear (toggle)
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "readiness_changed")
        assert len(rows) == 2
        clear_payload = json.loads(rows[1]["payload_json"])
        assert clear_payload["before"]["present"] is True
        assert clear_payload["after"]["present"] is False

    def test_content_update_emits_readiness_changed_with_content(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        serve._update_readiness_content(_proj(), "B-1", "tests", "pytest tests/")
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "readiness_changed")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["after"]["content"] == "pytest tests/"


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

class TestAttachmentEvents:
    def test_add_emits_attachment_added(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        result = serve._add_attachment("p", "B-1", "feedbacks", "session-1")
        assert result is not None
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "attachment_added")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["kind"] == "feedbacks"
        assert payload["label"] == "session-1"

    def test_delete_emits_attachment_removed(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        result = serve._add_attachment("p", "B-1", "feedbacks", "session-1")
        att_id = result["id"]
        ok = serve._delete_attachment("p", "B-1", att_id)
        assert ok
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c, "B-1", "attachment_removed")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["attachment_id"] == att_id
        assert payload["label"] == "session-1"


# ---------------------------------------------------------------------------
# Wire-format invariant — every mutable event has {before, after} keys
# (or per-event invertable equivalent). Same DB the M1b tests build up.
# ---------------------------------------------------------------------------

class TestHistoryEndpointPayload:
    """Verify the GET /api/tickets/{id}/history endpoint returns the right shape.

    We don't boot the HTTP server — we exercise the same SQL path the handler
    uses and assert the row→dict transformation produces the expected JSON.
    """

    def test_endpoint_returns_events_newest_first(self, serve_mod):
        serve, db_file = serve_mod
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c); c.commit(); c.close()
        proj = _proj()
        # Drive a few mutations so we have history.
        serve._update_ticket_field(proj, "B-1", "title", "X1")
        serve._update_ticket_field(proj, "B-1", "title", "X2")
        serve._add_criterion(proj, "B-1", "C1")
        # Replicate the endpoint's SQL + dict-shaping logic to assert the
        # payload format the dashboard JS will consume.
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, actor_type, actor_id, event_kind, payload_json, occurred_at, discarded_run_id "
            "FROM activity_events WHERE project_id='p' AND subject_type='ticket' AND subject_id='B-1' "
            "ORDER BY id DESC"
        ).fetchall()
        events = [{
            "id": r["id"], "actor_type": r["actor_type"], "actor_id": r["actor_id"],
            "event_kind": r["event_kind"], "payload": json.loads(r["payload_json"]),
            "occurred_at": r["occurred_at"], "discarded_run_id": r["discarded_run_id"],
        } for r in rows]
        # Newest first: criteria_added came last
        assert events[0]["event_kind"] == "criteria_added"
        # Each event has the documented top-level keys
        for e in events:
            assert {"id", "actor_type", "actor_id", "event_kind", "payload",
                    "occurred_at", "discarded_run_id"} <= set(e.keys())


class TestWireFormatInvariant:
    """Drive every M1b emission path once, then assert payload shape rules."""

    REQUIRES_BEFORE_AFTER = {
        "field_changed", "criteria_changed", "dependency_changed", "readiness_changed",
        # M1a kinds — already covered by their own tests but checked here for
        # the cross-cutting invariant.
        "section_change", "status_change", "criteria_check",
    }

    def test_invariant(self, serve_mod):
        serve, db_file = serve_mod
        # Run a representative slice.
        proj = _proj()
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        _seed(c, "B-1"); _seed(c, "B-2"); c.commit(); c.close()
        serve._update_ticket_field(proj, "B-1", "title", "X")
        serve._update_ticket_field(proj, "B-1", "status", "ready")
        serve._add_criterion(proj, "B-1", "first")
        serve._update_criterion_text(proj, "B-1", 0, "second")
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        c.execute("INSERT INTO depends VALUES ('B-1', 'p', 'B-2')"); c.commit()
        c.close()
        serve._update_depends(proj, "B-1", [])
        serve._toggle_readiness(proj, "B-1", "tests")

        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        rows = _events(c)
        for r in rows:
            payload = json.loads(r["payload_json"])
            if r["event_kind"] in self.REQUIRES_BEFORE_AFTER:
                assert "before" in payload, f"{r['event_kind']} missing 'before': {payload}"
                assert "after" in payload, f"{r['event_kind']} missing 'after': {payload}"
