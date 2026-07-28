"""Smoke tests for Phase 3C Workflow Conversation Feed UI.

Uses the live_page Playwright fixture (real server, no mocked routes).

Covers:
 1. section-workflow-feed hidden when ticket has no workflow_runs
 2. Section becomes visible when runs exist
 3. At least one turn renders when runs have conversation entries
 4. Compact and Full toggle buttons exist
 5. Toggling Full activates its button and deactivates Compact
 6. Compact mode shows tool-call chip when content has tool calls
 7. Each turn has a role badge with the expected CSS class
 8. Streaming turn renders wf-feed-streaming-dot while streaming=true
 9. needs_input panel appears when latest run is in needs_input status;
    Send disabled until textarea has content
10. localStorage persists the feed mode across page load
"""

import json
import sqlite3
import uuid

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DB_PATH_KEY = "ticket-takeaway"


def _db_path():
    """Return the deployed DB path used by serve.py."""
    import os
    import sys

    # Make sure src is on path
    src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from constants import DB_PATH

    return DB_PATH


def _seed_workflow_run(
    ticket_id: str,
    project_id: str = "ticket-takeaway",
    *,
    status: str = "succeeded",
    conversation: list | None = None,
    run_id: str | None = None,
) -> str:
    """Insert a workflow_run row directly into the DB and return the run id."""
    if run_id is None:
        run_id = "test-wf-" + uuid.uuid4().hex[:8]
    if conversation is None:
        conversation = []
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    # Ensure a workflow row exists (needed for FK if enforced)
    wf_id = "test-wf-placeholder"
    conn.execute(
        "INSERT OR IGNORE INTO workflows "
        "(id, name, description, steps, project_id) VALUES (?,?,?,?,?)",
        (wf_id, "Test WF", "", "[]", project_id),
    )
    conn.execute(
        "INSERT INTO workflow_runs "
        "(id, ticket_id, project_id, workflow_id, status, conversation, started_at) "
        "VALUES (?,?,?,?,?,?,datetime('now'))",
        (run_id, ticket_id, project_id, wf_id, status, json.dumps(conversation)),
    )
    conn.commit()
    conn.close()
    return run_id


def _delete_workflow_run(run_id: str):
    conn = sqlite3.connect(_db_path())
    conn.execute("DELETE FROM workflow_runs WHERE id = ?", (run_id,))
    conn.commit()
    conn.close()


def _get_first_ticket_id(page) -> str:
    """Return the data-item-id of the first kanban card on the page."""
    return page.evaluate(
        "document.querySelector('.card[data-item-id]')?.dataset?.itemId || ''"
    )


def _open_overlay(page, ticket_id: str):
    """Open detail overlay for the given ticket."""
    page.evaluate(f"window.openDetailOverlay('{ticket_id}')")
    page.wait_for_selector("#ticket-detail-overlay:not(.hidden)", timeout=5000)
    page.wait_for_timeout(400)  # let loadWorkflowRuns fire


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_feed_hidden_when_no_workflow_runs(live_page):
    """The workflow feed section is hidden when the ticket has no workflow runs."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    # Remove any pre-existing runs for this ticket to get a clean state
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "DELETE FROM workflow_runs WHERE ticket_id = ? AND project_id = 'ticket-takeaway'",
        (tid,),
    )
    conn.commit()
    conn.close()

    _open_overlay(p, tid)
    p.wait_for_timeout(600)  # wait for loadWorkflowRuns to complete

    feed_section = p.query_selector("#section-workflow-feed")
    assert feed_section is not None, "section-workflow-feed element not found"
    # Should be hidden (class 'hidden' present or not visible)
    is_hidden = "hidden" in (feed_section.get_attribute("class") or "")
    assert is_hidden, "Feed section should be hidden when no runs exist"


def test_feed_visible_after_run_seeded(live_page):
    """Feed section becomes visible when workflow_runs exist for the ticket."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    run_id = _seed_workflow_run(
        tid,
        conversation=[
            {"role": "system", "step": 0, "content": "Starting run.", "ts": None}
        ],
    )
    try:
        _open_overlay(p, tid)
        p.wait_for_timeout(800)

        feed_section = p.query_selector("#section-workflow-feed")
        assert feed_section is not None
        is_hidden = "hidden" in (feed_section.get_attribute("class") or "")
        assert not is_hidden, "Feed section should be visible when runs exist"
    finally:
        _delete_workflow_run(run_id)


def test_feed_renders_turns(live_page):
    """At least one turn element renders when runs have conversation entries."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    run_id = _seed_workflow_run(
        tid,
        conversation=[
            {"role": "system", "step": 0, "content": "Workflow started.", "ts": None},
            {
                "role": "agent",
                "step": 0,
                "agent": "TestAgent",
                "content": "Analysing ticket.",
                "ts": None,
            },
        ],
    )
    try:
        _open_overlay(p, tid)
        p.wait_for_timeout(800)

        turns = p.query_selector_all(".wf-feed-turn")
        assert len(turns) >= 1, f"Expected at least 1 turn, got {len(turns)}"
    finally:
        _delete_workflow_run(run_id)


def test_toggle_buttons_exist(live_page):
    """Compact and Full toggle buttons are present in the feed section."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    run_id = _seed_workflow_run(
        tid, conversation=[{"role": "agent", "content": "hello", "ts": None, "step": 0}]
    )
    try:
        _open_overlay(p, tid)
        p.wait_for_timeout(800)

        btn_compact = p.query_selector("#wfFeedToggleCompact")
        btn_full = p.query_selector("#wfFeedToggleFull")
        assert btn_compact is not None, "wfFeedToggleCompact button not found"
        assert btn_full is not None, "wfFeedToggleFull button not found"
    finally:
        _delete_workflow_run(run_id)


def test_full_toggle_activates(live_page):
    """Clicking Full activates it and deactivates Compact."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    run_id = _seed_workflow_run(
        tid, conversation=[{"role": "agent", "content": "hello", "ts": None, "step": 0}]
    )
    try:
        _open_overlay(p, tid)
        p.wait_for_timeout(800)

        # Reset to compact first by clicking compact
        p.click("#wfFeedToggleCompact")
        p.wait_for_timeout(200)

        # Now click Full
        p.click("#wfFeedToggleFull")
        p.wait_for_timeout(200)

        btn_full_cls = (
            p.query_selector("#wfFeedToggleFull").get_attribute("class") or ""
        )
        btn_compact_cls = (
            p.query_selector("#wfFeedToggleCompact").get_attribute("class") or ""
        )
        assert "active" in btn_full_cls, "Full button should have 'active' class"
        assert "active" not in btn_compact_cls, (
            "Compact button should NOT have 'active' class"
        )
    finally:
        _delete_workflow_run(run_id)


def test_compact_hides_tool_call_lines(live_page):
    """Compact mode: content with tool calls shows a chip with 'N tool-call lines hidden'."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    tool_content = "Here is my analysis.\n<function_calls>\n<invoke>some_tool</invoke>\n</function_calls>\nDone."
    run_id = _seed_workflow_run(
        tid,
        conversation=[
            {"role": "agent", "content": tool_content, "step": 0, "ts": None}
        ],
    )
    try:
        # Reset localStorage to compact before opening to ensure compact mode
        p.evaluate("localStorage.setItem('tt-workflow-feed-mode', 'compact')")
        _open_overlay(p, tid)
        p.wait_for_timeout(800)

        # Ensure compact mode is active by clicking compact button
        p.click("#wfFeedToggleCompact")
        p.wait_for_timeout(300)

        chips = p.query_selector_all(".wf-feed-tool-chip")
        assert len(chips) >= 1, "Expected at least one tool-call chip in compact mode"
        chip_text = chips[0].inner_text()
        assert "tool-call line" in chip_text, f"Chip text unexpected: {chip_text!r}"
    finally:
        _delete_workflow_run(run_id)


def test_role_badge_classes(live_page):
    """Turn role badges have the correct CSS class per role."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    run_id = _seed_workflow_run(
        tid,
        conversation=[
            {"role": "agent", "content": "Agent response.", "step": 0, "ts": None},
            {"role": "user", "content": "User reply.", "step": 0, "ts": None},
            {"role": "system", "content": "System note.", "step": 0, "ts": None},
        ],
    )
    try:
        _open_overlay(p, tid)
        p.wait_for_timeout(800)

        badges = p.query_selector_all(".wf-feed-turn-role")
        classes = [b.get_attribute("class") or "" for b in badges]
        role_classes = set()
        for cls in classes:
            for part in cls.split():
                if part.startswith("role-"):
                    role_classes.add(part)

        assert "role-agent" in role_classes, f"role-agent missing from: {role_classes}"
        assert "role-user" in role_classes, f"role-user missing from: {role_classes}"
        assert "role-system" in role_classes, (
            f"role-system missing from: {role_classes}"
        )
    finally:
        _delete_workflow_run(run_id)


def test_streaming_dot_shown(live_page):
    """A turn with streaming=true renders the wf-feed-streaming-dot."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    run_id = _seed_workflow_run(
        tid,
        status="running",
        conversation=[
            {
                "role": "agent",
                "content": "Still processing...",
                "step": 0,
                "streaming": True,
                "ts": None,
            }
        ],
    )
    try:
        _open_overlay(p, tid)
        p.wait_for_timeout(800)

        dots = p.query_selector_all(".wf-feed-streaming-dot")
        assert len(dots) >= 1, "Expected wf-feed-streaming-dot for streaming turn"
    finally:
        _delete_workflow_run(run_id)


def test_needs_input_panel_appears(live_page):
    """needs_input panel shows when latest run status is needs_input."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    run_id = _seed_workflow_run(
        tid,
        status="needs_input",
        conversation=[
            {
                "role": "agent",
                "content": "Please provide the branch name.",
                "step": 0,
                "ts": None,
            }
        ],
    )
    try:
        _open_overlay(p, tid)
        p.wait_for_timeout(800)

        ni_panel = p.query_selector("#wfFeedNeedsInput")
        assert ni_panel is not None, "wfFeedNeedsInput element not found"
        is_hidden = "hidden" in (ni_panel.get_attribute("class") or "")
        assert not is_hidden, "needs_input panel should be visible"
    finally:
        _delete_workflow_run(run_id)


def test_send_disabled_until_input(live_page):
    """Send button is disabled until the textarea has content."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    run_id = _seed_workflow_run(
        tid,
        status="needs_input",
        conversation=[
            {"role": "agent", "content": "What is the deadline?", "step": 0, "ts": None}
        ],
    )
    try:
        _open_overlay(p, tid)
        p.wait_for_timeout(800)

        send_btn = p.query_selector("#wfFeedNiSend")
        assert send_btn is not None
        assert send_btn.is_disabled(), "Send should be disabled when textarea is empty"

        # Type something into textarea
        p.fill("#wfFeedNiTextarea", "Tomorrow")
        p.wait_for_timeout(150)
        assert not send_btn.is_disabled(), "Send should be enabled after typing"
    finally:
        _delete_workflow_run(run_id)


def test_localstorage_persists_mode(live_page, dashboard_server):
    """Selecting Full mode, then reloading, preserves Full as active."""
    p = live_page
    tid = _get_first_ticket_id(p)
    if not tid:
        pytest.skip("No tickets on board")

    run_id = _seed_workflow_run(
        tid, conversation=[{"role": "agent", "content": "hello", "step": 0, "ts": None}]
    )
    try:
        _open_overlay(p, tid)
        p.wait_for_timeout(600)

        # Click Full
        p.click("#wfFeedToggleFull")
        p.wait_for_timeout(300)

        # Verify localStorage was set
        mode = p.evaluate("localStorage.getItem('tt-workflow-feed-mode')")
        assert mode == "full", f"Expected 'full' in localStorage, got {mode!r}"

        # Reload and check button state
        p.goto(dashboard_server)
        p.wait_for_selector(".card[data-item-id]", timeout=8000)
        _open_overlay(p, tid)
        p.wait_for_timeout(600)

        btn_full_cls = (
            p.query_selector("#wfFeedToggleFull").get_attribute("class") or ""
        )
        assert "active" in btn_full_cls, "Full button should be active after reload"

        # Cleanup: reset to compact
        p.evaluate("localStorage.setItem('tt-workflow-feed-mode', 'compact')")
    finally:
        _delete_workflow_run(run_id)
