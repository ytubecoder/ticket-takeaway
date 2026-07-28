"""E2E test: quick edit journey.

Opens a ticket detail overlay, edits a field, verifies persistence.
"""

import time

import pytest
from conftest import api_delete, api_get, api_post, api_put


@pytest.fixture()
def editable_ticket(dashboard_server):
    """Create a ticket for editing tests, yield ID, delete on teardown."""
    title = f"e2e-edit-{int(time.time())}"
    _, data = api_post(
        dashboard_server,
        "/api/tickets",
        {"title": title, "section": "Backlog", "description": "original description"},
    )
    tid = data["id"]
    yield tid
    api_delete(dashboard_server, f"/api/tickets/{tid}")


def test_edit_description_persists(dashboard_server, editable_ticket):
    """Editing description via API persists and is retrievable."""
    tid = editable_ticket
    new_desc = f"edited-{int(time.time())}"

    # Edit via PUT
    status_code, _ = api_put(
        dashboard_server,
        f"/api/tickets/{tid}",
        {"description": new_desc},
    )
    assert status_code == 200

    # Verify persistence
    ticket = api_get(dashboard_server, f"/api/tickets/{tid}")
    assert ticket["description"] == new_desc


def test_edit_title_persists(dashboard_server, editable_ticket):
    """Editing title via API persists and is retrievable."""
    tid = editable_ticket
    new_title = f"edited-title-{int(time.time())}"

    status_code, _ = api_put(
        dashboard_server,
        f"/api/tickets/{tid}",
        {"title": new_title},
    )
    assert status_code == 200

    ticket = api_get(dashboard_server, f"/api/tickets/{tid}")
    assert ticket["title"] == new_title


def test_add_acceptance_criterion_persists(dashboard_server, editable_ticket):
    """Adding an acceptance criterion via API persists."""
    tid = editable_ticket
    criterion_text = f"criterion-{int(time.time())}"

    status_code, _ = api_put(
        dashboard_server,
        f"/api/tickets/{tid}",
        {"add_criteria": criterion_text},
    )
    assert status_code == 200

    ticket = api_get(dashboard_server, f"/api/tickets/{tid}")
    criteria_texts = [
        c["text"] if isinstance(c, dict) else c[1]
        for c in ticket.get("acceptance_criteria", [])
    ]
    assert criterion_text in criteria_texts


def test_edit_reflects_in_dashboard(live_page, dashboard_server):
    """Edit a ticket via API, then verify the dashboard reflects the change."""
    page = live_page

    # Create a test ticket
    ts = int(time.time())
    _, data = api_post(
        dashboard_server,
        "/api/tickets",
        {"title": f"browser-edit-{ts}", "section": "Backlog", "description": "old"},
    )
    tid = data["id"]

    try:
        # Edit via API
        new_desc = f"browser-edited-{ts}"
        api_put(dashboard_server, f"/api/tickets/{tid}", {"description": new_desc})

        # Reload dashboard and verify the ticket shows updated content
        page.reload()
        page.wait_for_selector(".card[data-item-id]", timeout=10000)

        # Open the ticket detail overlay via JS API
        page.evaluate(f"window.openDetailOverlay('{tid}')")

        # Wait for the overlay to load ticket data (it fetches async)
        page.wait_for_function(
            """() => {
                var ta = document.querySelector('#ticket-detail-overlay textarea[data-field="description"]');
                return ta && ta.value !== '';
            }""",
            timeout=5000,
        )

        # Verify the description textarea has the new value
        desc_value = page.evaluate("""() => {
            var ta = document.querySelector('#ticket-detail-overlay textarea[data-field="description"]');
            return ta ? ta.value : null;
        }""")
        assert desc_value == new_desc
    finally:
        api_delete(dashboard_server, f"/api/tickets/{tid}")


@pytest.mark.skip(reason="undo/redo not yet implemented in detail overlay")
def test_undo_redo():
    """Placeholder for undo/redo test when implemented."""
