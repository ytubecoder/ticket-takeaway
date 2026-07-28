"""E2E tests for the orchestrator interview flow (Lanes A/B/C/D/E integration).

Replaced the original placeholder file. Two runnable tests:

1. `test_full_page_ticket_view_renders_tabs` — Playwright smoke against the new
   `/{project}/tickets/{id}?tab=...` route (Lane B). Confirms the page loads
   and all five tabs are reachable.
2. `test_respond_endpoint_validates_needs_input_state` — API-level validation
   of `POST /api/runs/{id}/respond` (Lane B + Lane A contract). Exercises
   error paths without needing a real agent subprocess.

Three tests remain skipped — the full chat -> propose -> commit flow needs a
real subprocess agent (or substantial mocking), which is out of scope for this
pass.
"""

import sqlite3
import time
from pathlib import Path

import pytest
from conftest import api_delete, api_get, api_post

_LIVE_DB_PATH = Path.home() / ".claude" / "ticket-takeaway" / "tickets.db"


@pytest.fixture()
def fresh_ideas_ticket(dashboard_server):
    """Create a test ticket in Ideas, yield its id, delete on teardown."""
    title = f"e2e-orchestrator-{int(time.time())}"
    status_code, data = api_post(
        dashboard_server, "/api/tickets", {"title": title, "section": "Ideas"}
    )
    assert status_code == 201, f"Failed to create ticket: {data}"
    tid = data["id"]
    yield tid
    api_delete(dashboard_server, f"/api/tickets/{tid}")


def test_full_page_ticket_view_renders_tabs(page, dashboard_server, fresh_ideas_ticket):
    """The new /{project}/tickets/{id} route renders + every tab is reachable."""
    tid = fresh_ideas_ticket
    base = dashboard_server  # already includes /ticket-takeaway

    # Default tab = overview
    page.goto(f"{base}/tickets/{tid}")
    page.wait_for_load_state("networkidle")
    assert tid in page.content(), "ticket id should appear on the page"

    # Each tab should render without 404 and surface its label
    for tab in ("overview", "activity", "runs", "files", "graph"):
        page.goto(f"{base}/tickets/{tid}?tab={tab}")
        page.wait_for_load_state("networkidle")
        body = page.content().lower()
        assert tab in body, f"tab={tab} did not render its label in the page body"


def test_activity_endpoint_returns_wrapped_events(dashboard_server, fresh_ideas_ticket):
    """Lane B: GET /api/tickets/{id}/activity returns {events: [...], next_before: null}."""
    tid = fresh_ideas_ticket
    data = api_get(dashboard_server, f"/api/tickets/{tid}/activity")
    assert "events" in data, "response must be wrapped under 'events' key"
    assert isinstance(data["events"], list)
    # next_before may be None for short histories — just confirm key presence
    assert "next_before" in data


def test_respond_endpoint_validates_needs_input_state(
    dashboard_server, fresh_ideas_ticket
):
    """Lane B: POST /api/runs/{id}/respond rejects runs that aren't in needs_input.

    We inject a stub run directly into the live DB at status='succeeded' and
    confirm the endpoint refuses to dispatch a respond against it. Then we
    flip the status to 'needs_input' (kind='text') and confirm the endpoint
    no longer rejects on state grounds (it may still reject downstream, but
    not at the validation gate).
    """
    tid = fresh_ideas_ticket

    # Insert a stub run row attached to the test ticket
    con = sqlite3.connect(str(_LIVE_DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        # Find the project_id for this ticket
        proj = con.execute(
            "SELECT project_id FROM tickets WHERE id = ?", (tid,)
        ).fetchone()
        assert proj is not None
        project_id = proj["project_id"]

        cur = con.execute(
            """INSERT INTO runs
               (project_id, subject_type, subject_id, runner_kind, status,
                triggered_by, attempt, claim_owner, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                "ticket",
                tid,
                "agent",
                "succeeded",
                "human",
                1,
                "test-owner",
                "{}",
            ),
        )
        run_id = cur.lastrowid
        con.commit()
    finally:
        con.close()

    try:
        # 1. Run is 'succeeded' — respond endpoint should reject
        status_code, _ = api_post(
            dashboard_server,
            f"/api/runs/{run_id}/respond",
            {"kind": "text", "response": "hello"},
        )
        assert status_code in (400, 409), (
            f"respond against 'succeeded' run should reject; got {status_code}"
        )

        # 2. Flip the run to needs_input + kind=text
        con = sqlite3.connect(str(_LIVE_DB_PATH))
        try:
            con.execute(
                "UPDATE runs SET status='needs_input', needs_input_kind='text', "
                "needs_input_prompt=? WHERE id=?",
                ('{"ask":"Test prompt for E2E"}', run_id),
            )
            con.commit()
        finally:
            con.close()

        # The endpoint may still 4xx for other reasons (no live workspace, etc.)
        # but the state-validation check should no longer block it. We accept
        # any non-{400,409} response, OR a 5xx (server-side downstream error)
        # as proof that we passed the state gate.
        status_code, body = api_post(
            dashboard_server,
            f"/api/runs/{run_id}/respond",
            {"kind": "text", "response": "test reply"},
        )
        # Acceptable: 200/202 (resumed), 500 (downstream resume failure on stub run).
        # Unacceptable: 400/409 (state validation rejected — the bug we're guarding).
        assert status_code not in (400, 409), (
            f"state validation should pass for needs_input/text; got {status_code} body={body}"
        )

        # 3. Mismatched kind ('propose' against a text-kind needs_input) MUST reject
        status_code, _ = api_post(
            dashboard_server,
            f"/api/runs/{run_id}/respond",
            {"kind": "propose", "accepted": {}},
        )
        assert status_code in (400, 409), (
            f"mismatched kind should reject; got {status_code}"
        )
    finally:
        # Clean up the stub run
        con = sqlite3.connect(str(_LIVE_DB_PATH))
        try:
            con.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            con.commit()
        finally:
            con.close()


@pytest.mark.skip(
    reason=(
        "Requires real agent subprocess or substantial mocking. "
        "The other tests in this file cover the surface; this is the full chat -> "
        "propose -> commit -> Backlog flow which needs a live `claude` CLI."
    )
)
def test_full_chat_to_propose_to_commit_flow(page, dashboard_server):
    raise NotImplementedError


@pytest.mark.skip(
    reason="Requires real agent subprocess. See comment on previous test."
)
def test_runs_tab_shows_chat_transcript(page, dashboard_server):
    raise NotImplementedError


@pytest.mark.skip(reason="Requires Done->Learnings workflow enabled + real agent.")
def test_done_learnings_workflow_writes_l_flag_content(page, dashboard_server):
    raise NotImplementedError
