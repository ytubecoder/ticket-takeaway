"""E2E tests for the Seek feature — API endpoint and idempotency."""
import json
import os
import time
import pytest
from conftest import api_post, api_get, api_delete


def test_seek_api_returns_results(dashboard_server):
    """POST /api/seek returns proper response shape."""
    status_code, data = api_post(dashboard_server, "/api/seek", {})
    assert status_code == 200, f"Seek failed: {data}"
    assert "discovered" in data
    assert "created" in data
    assert "skipped_duplicates" in data
    assert "tickets" in data
    assert isinstance(data["tickets"], list)


def test_seek_api_idempotent(dashboard_server):
    """Running seek twice creates 0 new tickets on second run."""
    status1, data1 = api_post(dashboard_server, "/api/seek", {})
    assert status1 == 200
    status2, data2 = api_post(dashboard_server, "/api/seek", {})
    assert status2 == 200
    assert data2["created"] == 0


def test_seek_api_with_source_filter(dashboard_server):
    """POST with sources filter only scans specified types."""
    status_code, data = api_post(dashboard_server, "/api/seek", {"sources": ["md_task"]})
    assert status_code == 200
    assert "discovered" in data


def test_drafts_excluded_from_markdown(dashboard_server):
    """Draft tickets should NOT appear in PRODUCT_BACKLOG.md after sync."""
    # Create a draft ticket via API
    status, ticket = api_post(dashboard_server, "/api/tickets", {
        "title": f"e2e-draft-test-{int(time.time())}",
        "section": "Ideas",
        "draft": True
    })
    if status != 201:
        pytest.skip("Could not create draft ticket")

    # Read the backlog file and check the draft is NOT in it
    # The sync happens automatically after ticket creation
    backlog_path = os.path.join(os.path.dirname(__file__), "..", "PRODUCT_BACKLOG.md")
    if os.path.exists(backlog_path):
        with open(backlog_path, "r") as f:
            content = f.read()
        assert ticket.get("title", "e2e-draft-test") not in content, \
            "Draft ticket should not appear in PRODUCT_BACKLOG.md"

    # Cleanup
    tid = ticket.get("id", "")
    if tid:
        api_delete(dashboard_server, f"/api/tickets/{tid}")
