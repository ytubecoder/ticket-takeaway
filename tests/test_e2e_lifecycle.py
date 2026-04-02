"""E2E test: ticket lifecycle journey.

Creates a ticket and moves it through the full lifecycle:
  create → Backlog → WIP → For Review → accept → Done

All operations go through the live HTTP API (no mocks).
"""

import time

import pytest

from conftest import api_delete, api_get, api_post


@pytest.fixture()
def lifecycle_ticket(dashboard_server):
    """Create a test ticket, yield its ID, delete on teardown."""
    title = f"e2e-lifecycle-{int(time.time())}"
    status_code, data = api_post(
        dashboard_server, "/api/tickets", {"title": title, "section": "Ideas"}
    )
    assert status_code == 201, f"Failed to create ticket: {data}"
    tid = data["id"]
    yield tid
    # Cleanup — ignore errors if already deleted
    api_delete(dashboard_server, f"/api/tickets/{tid}")


def test_full_ticket_lifecycle(dashboard_server, lifecycle_ticket):
    """Ticket moves through Ideas → Backlog → WIP → For Review → Done."""
    tid = lifecycle_ticket

    # 1. Verify created in Ideas
    ticket = api_get(dashboard_server, f"/api/tickets/{tid}")
    assert ticket["section"] == "Ideas"
    assert ticket["status"] == "proposed"

    # 2. Move to Backlog
    status_code, _ = api_post(
        dashboard_server, f"/api/tickets/{tid}/move", {"section": "Backlog"}
    )
    assert status_code == 200
    ticket = api_get(dashboard_server, f"/api/tickets/{tid}")
    assert ticket["section"] == "Backlog"
    assert ticket["status"] == "proposed"

    # 3. Move to WIP
    status_code, _ = api_post(
        dashboard_server, f"/api/tickets/{tid}/move", {"section": "WIP"}
    )
    assert status_code == 200
    ticket = api_get(dashboard_server, f"/api/tickets/{tid}")
    assert ticket["section"] == "WIP"
    assert ticket["status"] == "in-progress"

    # 4. Move to For Review
    status_code, _ = api_post(
        dashboard_server, f"/api/tickets/{tid}/move", {"section": "For Review"}
    )
    assert status_code == 200
    ticket = api_get(dashboard_server, f"/api/tickets/{tid}")
    assert ticket["section"] == "For Review"
    assert ticket["status"] == "for-review"

    # 5. Accept → Done
    status_code, _ = api_post(
        dashboard_server, f"/api/tickets/{tid}/accept", {}
    )
    assert status_code == 200
    ticket = api_get(dashboard_server, f"/api/tickets/{tid}")
    assert ticket["section"] == "Done"
    assert ticket["status"] == "done"
