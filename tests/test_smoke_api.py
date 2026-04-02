"""Smoke tests: every API endpoint returns expected response.

Uses the live dashboard_server fixture. Each test verifies status code
and basic response shape — no complex assertions on content.
"""

import json
import time
import urllib.error
import urllib.request

import pytest

from conftest import api_delete, api_get, api_post, api_put


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_first_ticket_id(base_url):
    """Return the first ticket ID from the API."""
    data = api_get(base_url, "/api/tickets")
    if isinstance(data, list) and len(data) > 0:
        return data[0]["id"]
    elif isinstance(data, dict) and "tickets" in data:
        return data["tickets"][0]["id"]
    pytest.skip("No tickets in database")


def _raw_get(url):
    """Raw GET returning (status, content_type, body)."""
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


# ---------------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------------


def test_get_root_returns_html(dashboard_server):
    """GET / returns 200 with HTML content."""
    status, ct, body = _raw_get(dashboard_server)
    assert status == 200
    assert "text/html" in ct
    assert b"<html" in body.lower()


def test_get_tickets_returns_json(dashboard_server):
    """GET /api/tickets returns 200 with a JSON array or object."""
    data = api_get(dashboard_server, "/api/tickets")
    assert isinstance(data, (list, dict))


def test_get_single_ticket_returns_json(dashboard_server):
    """GET /api/tickets/{id} returns 200 with ticket data."""
    tid = _get_first_ticket_id(dashboard_server)
    data = api_get(dashboard_server, f"/api/tickets/{tid}")
    assert data["id"] == tid


def test_get_nonexistent_ticket_returns_404(dashboard_server):
    """GET /api/tickets/NONEXISTENT returns 404."""
    try:
        urllib.request.urlopen(
            f"{dashboard_server}/api/tickets/NONEXISTENT-999", timeout=10
        )
        pytest.fail("Expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404


# ---------------------------------------------------------------------------
# POST endpoints
# ---------------------------------------------------------------------------


def test_post_create_ticket(dashboard_server):
    """POST /api/tickets creates a ticket and returns 201."""
    title = f"smoke-test-{int(time.time())}"
    status_code, data = api_post(
        dashboard_server, "/api/tickets", {"title": title, "section": "Ideas"}
    )
    assert status_code == 201
    assert "id" in data

    # Cleanup
    api_delete(dashboard_server, f"/api/tickets/{data['id']}")


def test_post_move_ticket(dashboard_server):
    """POST /api/tickets/{id}/move returns 200."""
    tid = _get_first_ticket_id(dashboard_server)
    # Get current section to restore later
    original = api_get(dashboard_server, f"/api/tickets/{tid}")
    original_section = original.get("section", original.get("column", "Backlog"))

    status_code, data = api_post(
        dashboard_server, f"/api/tickets/{tid}/move", {"section": "WIP"}
    )
    assert status_code == 200

    # Restore
    api_post(
        dashboard_server, f"/api/tickets/{tid}/move", {"section": original_section}
    )


def test_post_toggle_readiness_flag(dashboard_server):
    """POST /api/tickets/{id}/readiness/tests returns 200."""
    tid = _get_first_ticket_id(dashboard_server)
    status_code, data = api_post(
        dashboard_server, f"/api/tickets/{tid}/readiness/tests", {}
    )
    assert status_code == 200

    # Toggle back
    api_post(dashboard_server, f"/api/tickets/{tid}/readiness/tests", {})


# ---------------------------------------------------------------------------
# PUT endpoints
# ---------------------------------------------------------------------------


def test_put_update_ticket(dashboard_server):
    """PUT /api/tickets/{id} returns 200 and updates field."""
    tid = _get_first_ticket_id(dashboard_server)
    original = api_get(dashboard_server, f"/api/tickets/{tid}")
    original_desc = original.get("description", "")

    status_code, data = api_put(
        dashboard_server,
        f"/api/tickets/{tid}",
        {"description": "smoke test description"},
    )
    assert status_code == 200

    # Restore
    api_put(dashboard_server, f"/api/tickets/{tid}", {"description": original_desc})


def test_put_readiness_content(dashboard_server):
    """PUT /api/tickets/{id}/readiness/tests returns 200."""
    tid = _get_first_ticket_id(dashboard_server)
    status_code, data = api_put(
        dashboard_server,
        f"/api/tickets/{tid}/readiness/tests",
        {"content": "smoke test"},
    )
    assert status_code == 200

    # Restore
    api_put(
        dashboard_server, f"/api/tickets/{tid}/readiness/tests", {"content": ""}
    )


# ---------------------------------------------------------------------------
# DELETE endpoint
# ---------------------------------------------------------------------------


def test_delete_ticket(dashboard_server):
    """DELETE /api/tickets/{id} returns 200."""
    # Create a ticket to delete
    title = f"smoke-delete-{int(time.time())}"
    _, created = api_post(
        dashboard_server, "/api/tickets", {"title": title, "section": "Ideas"}
    )
    tid = created["id"]

    status_code, data = api_delete(dashboard_server, f"/api/tickets/{tid}")
    assert status_code == 200
