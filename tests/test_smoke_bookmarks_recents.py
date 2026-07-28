"""Smoke tests for bookmarks + recents API endpoints (I-43).

Hits the live dashboard_server with the unprefixed /api routes (multi-project
routing rewrites these to the legacy project under the hood). Verifies the
endpoints accept the documented requests and return the documented shapes.
"""

import pytest
from conftest import api_get, api_post


def _get_first_ticket_id(base_url):
    data = api_get(base_url, "/api/tickets")
    if isinstance(data, list) and len(data) > 0:
        return data[0]["id"]
    if isinstance(data, dict) and "tickets" in data and data["tickets"]:
        return data["tickets"][0]["id"]
    pytest.skip("No tickets in database")


def test_get_bookmarks_returns_wrapped_list(dashboard_server):
    data = api_get(dashboard_server, "/api/bookmarks")
    assert "bookmarks" in data
    assert isinstance(data["bookmarks"], list)


def test_get_recents_returns_wrapped_list(dashboard_server):
    data = api_get(dashboard_server, "/api/recents")
    assert "recents" in data
    assert isinstance(data["recents"], list)


def test_toggle_bookmark_flips_state(dashboard_server):
    tid = _get_first_ticket_id(dashboard_server)
    status, body = api_post(dashboard_server, f"/api/bookmarks/{tid}", {})
    assert status == 200
    first_state = body["bookmarked"]
    assert isinstance(first_state, bool)
    # Toggle again — state should flip back
    status, body = api_post(dashboard_server, f"/api/bookmarks/{tid}", {})
    assert status == 200
    assert body["bookmarked"] is not first_state


def test_bookmark_appears_in_list(dashboard_server):
    tid = _get_first_ticket_id(dashboard_server)
    # Make sure the ticket is bookmarked
    _, body = api_post(dashboard_server, f"/api/bookmarks/{tid}", {})
    try:
        if body["bookmarked"] is False:
            api_post(dashboard_server, f"/api/bookmarks/{tid}", {})
        data = api_get(dashboard_server, "/api/bookmarks")
        ids = [b["id"] for b in data["bookmarks"]]
        assert tid in ids
    finally:
        # Best-effort cleanup so the test is idempotent
        cur = api_get(dashboard_server, "/api/bookmarks")
        if any(b["id"] == tid for b in cur["bookmarks"]):
            api_post(dashboard_server, f"/api/bookmarks/{tid}", {})


def test_touch_recent_adds_to_list(dashboard_server):
    tid = _get_first_ticket_id(dashboard_server)
    status, body = api_post(dashboard_server, f"/api/recents/{tid}", {})
    assert status == 200
    assert body.get("ok") is True
    data = api_get(dashboard_server, "/api/recents")
    ids = [r["id"] for r in data["recents"]]
    assert tid in ids


def test_unknown_ticket_returns_404(dashboard_server):
    status, body = api_post(dashboard_server, "/api/bookmarks/NOPE-9999", {})
    assert status == 404
    status, body = api_post(dashboard_server, "/api/recents/NOPE-9999", {})
    assert status == 404
