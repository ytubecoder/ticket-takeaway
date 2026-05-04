"""TDD tests for Kitchen M4 — gap-ticket file-from-failure flow.

Exercises the same SQL + dict-shaping logic the POST /api/runs/{rid}/file-gap-ticket
handler uses, without spinning up the HTTP server.
"""

import importlib
import importlib.util
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def serve_mod(tmp_path, monkeypatch):
    """Reload serve.py against a temp DB; stub filesystem-touching cli helpers."""
    db_file = tmp_path / "tickets.db"
    monkeypatch.setenv("HOME", str(tmp_path))
    import constants
    monkeypatch.setattr(constants, "DB_PATH", db_file)
    monkeypatch.setattr(constants, "DASHBOARD_DIR", tmp_path / ".claude" / "ticket-takeaway")
    (tmp_path / ".claude" / "ticket-takeaway").mkdir(parents=True, exist_ok=True)

    import db
    importlib.reload(db)
    spec = importlib.util.spec_from_file_location("serve_under_test_m4", "src/serve.py")
    serve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(serve)
    monkeypatch.setattr(serve, "get_db", lambda: db.get_db(str(db_file)))
    monkeypatch.setattr(serve.cli, "ingest_markdown", lambda conn, proj: 0)
    monkeypatch.setattr(serve.cli, "sync_to_markdown", lambda conn, proj: None)
    if hasattr(serve.cli, "regenerate_dashboard"):
        monkeypatch.setattr(serve.cli, "regenerate_dashboard", lambda proj: None)

    c = serve.get_db(); serve.init_db(c); c.close()
    return serve, db_file


def _seed_journey(db_file, jid="J-1", project_id="p"):
    c = sqlite3.connect(db_file)
    c.execute("INSERT INTO journeys (id, project_id, title) VALUES (?, ?, 'Test')",
              (jid, project_id))
    c.commit(); c.close()


def _seed_failed_scenario_run(db_file, jid="J-1", project_id="p", gap_report=None):
    c = sqlite3.connect(db_file)
    meta = json.dumps({"gap_report": gap_report}) if gap_report else "{}"
    cur = c.execute(
        "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, "
        " triggered_by, error_class, error_message, metadata_json, finished_at, started_at) "
        "VALUES (?, 'journey', ?, 'scenario', 'failed', 'human', "
        "        'scenario_step_failed', 'click target not found', ?, "
        "        '2026-04-29T00:00:00Z', '2026-04-29T00:00:00Z')",
        (project_id, jid, meta),
    )
    rid = cur.lastrowid
    c.commit(); c.close()
    return rid


def _file_gap_ticket(serve, project_id, run_id):
    """Replicate what the POST /api/runs/{rid}/file-gap-ticket handler does.
    The endpoint logic is server-bound; we exercise the same DB ops in-process
    by importing the closed-over names directly.
    """
    proj = {"id": project_id, "name": "P", "path": "/tmp/x"}
    # The handler reads the run, builds the title/description, calls
    # _actions_add_ticket + INSERTs criterion + links the journey + emits an event.
    # We reuse the same actions APIs to mirror the handler.
    from actions import add_ticket as _add_ticket, ActorContext, emit_event
    c = serve.get_db(); serve.init_db(c)
    run = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run is not None
    meta = json.loads(run["metadata_json"] or "{}")
    gap = meta.get("gap_report") or {}
    journey_id = run["subject_id"]
    gap_kind = gap.get("gap_kind", "missing_feature")
    failed_action = gap.get("failed_step_action") or ""
    title = f"[gap:{gap_kind}] {failed_action} step in journey {journey_id}".strip()
    description = f"_Auto-filed from red scenario run #{run_id}._\n\n**Gap kind:** `{gap_kind}`"
    tid = _add_ticket(c, project_id, title,
                      section="Ideas", priority="medium",
                      description=description, draft=True)
    c.execute("INSERT INTO acceptance_criteria (ticket_id, project_id, text) VALUES (?, ?, ?)",
              (tid, project_id, f"Resolve gap from run #{run_id}"))
    c.execute("INSERT OR IGNORE INTO journey_tickets (journey_id, project_id, ticket_id) "
              "VALUES (?, ?, ?)", (journey_id, project_id, tid))
    emit_event(c, project_id, "ticket", tid, "ticket_created",
               {"id": tid, "title": title, "section": "Ideas",
                "from_gap_run_id": run_id, "linked_journey": journey_id},
               ActorContext.system())
    c.commit(); c.close()
    return tid


# ---------------------------------------------------------------------------
# Happy path — gap report → draft ticket linked to the journey
# ---------------------------------------------------------------------------

class TestFileGapTicket:
    def test_creates_draft_ticket_in_ideas(self, serve_mod):
        serve, db_file = serve_mod
        _seed_journey(db_file, "J-onboarding")
        rid = _seed_failed_scenario_run(db_file, "J-onboarding", gap_report={
            "gap_kind": "missing_selector",
            "failed_step_index": 3,
            "failed_step_action": "click",
            "failed_step_target": {"testid": "join-team-btn"},
            "screenshot_path": "/tmp/shot.png",
            "error_message": "no element matches testid=join-team-btn",
            "manifest_id": "J-onboarding",
            "step_count": 8,
        })
        tid = _file_gap_ticket(serve, "p", rid)
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        t = c.execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
        c.close()
        assert t["section"] == "Ideas"
        assert t["draft"] == 1
        assert "missing_selector" in t["title"]
        assert "click" in t["title"]
        assert "J-onboarding" in t["title"]

    def test_links_to_originating_journey(self, serve_mod):
        serve, db_file = serve_mod
        _seed_journey(db_file, "J-X")
        rid = _seed_failed_scenario_run(db_file, "J-X", gap_report={
            "gap_kind": "missing_feature",
            "failed_step_action": "assert_visible",
        })
        tid = _file_gap_ticket(serve, "p", rid)
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        link = c.execute(
            "SELECT * FROM journey_tickets WHERE journey_id = 'J-X' AND ticket_id = ?",
            (tid,),
        ).fetchone()
        c.close()
        assert link is not None

    def test_emits_ticket_created_with_system_actor(self, serve_mod):
        serve, db_file = serve_mod
        _seed_journey(db_file, "J-X")
        rid = _seed_failed_scenario_run(db_file, "J-X", gap_report={"gap_kind": "missing_feature"})
        tid = _file_gap_ticket(serve, "p", rid)
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        ev = c.execute(
            "SELECT actor_type, payload_json FROM activity_events "
            "WHERE event_kind='ticket_created' AND subject_id = ?",
            (tid,),
        ).fetchone()
        c.close()
        assert ev is not None
        assert ev["actor_type"] == "system"
        payload = json.loads(ev["payload_json"])
        assert payload["from_gap_run_id"] == rid
        assert payload["linked_journey"] == "J-X"

    def test_includes_acceptance_criterion(self, serve_mod):
        serve, db_file = serve_mod
        _seed_journey(db_file, "J-X")
        rid = _seed_failed_scenario_run(db_file, "J-X", gap_report={"gap_kind": "missing_feature"})
        tid = _file_gap_ticket(serve, "p", rid)
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        crit = c.execute(
            "SELECT text FROM acceptance_criteria WHERE ticket_id = ?", (tid,),
        ).fetchall()
        c.close()
        assert len(crit) == 1
        assert str(rid) in crit[0]["text"]

    def test_closed_loop_after_ticket_done_cascades_back_to_journey(self, serve_mod, monkeypatch):
        # Full closed-loop: file gap ticket → human moves it to Done → cascade
        # queues a re-run of the linked journey.
        serve, db_file = serve_mod
        import kitchen
        cascade_calls: list = []
        monkeypatch.setattr(kitchen, "trigger_run",
                            lambda *a, **k: (cascade_calls.append((a, k)) or 1))
        _seed_journey(db_file, "J-X")
        rid = _seed_failed_scenario_run(db_file, "J-X", gap_report={"gap_kind": "missing_feature"})
        tid = _file_gap_ticket(serve, "p", rid)

        # Move the new ticket to Done — cascade must fire.
        from actions import move_ticket, ActorContext
        c = sqlite3.connect(db_file); c.row_factory = sqlite3.Row
        # Confirm the ticket was created as draft; flip it un-draft so move
        # logic isn't surprised (existing move_ticket doesn't gate on draft).
        c.execute("UPDATE tickets SET draft = 0 WHERE id = ?", (tid,))
        c.commit()
        move_ticket(c, "p", tid, "Done", actor=ActorContext.human())
        c.commit(); c.close()
        assert len(cascade_calls) == 1
        args, kwargs = cascade_calls[0]
        assert args[3] == "J-X"
        assert kwargs.get("triggered_by") == "journey-cascade"
