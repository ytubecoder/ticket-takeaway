"""Smoke tests for Journey API endpoints.

Requires serve.py to be running (via dashboard_server fixture).
"""

import json
import urllib.error
import urllib.request

import pytest
from conftest import api_delete, api_get, api_put


def safe_api_get(base_url, path):
    """GET that returns (status, data) like the other helpers."""
    url = f"{base_url}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, json.loads(body) if body else {}


def safe_api_post(base_url, path, body_data):
    """POST that safely reads error body once."""
    url = f"{base_url}{path}"
    data = json.dumps(body_data).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, json.loads(body) if body else {}


# Use safe_api_post as the default
api_post = safe_api_post


# ===========================================================================
# Journey CRUD
# ===========================================================================


class TestJourneyCreate:
    def test_create_journey(self, dashboard_server):
        status, data = api_post(
            dashboard_server,
            "/api/journeys",
            {
                "title": "Smoke Test Journey",
                "description": "Test description",
                "persona": "Tester",
            },
        )
        assert status == 201
        assert data["title"] == "Smoke Test Journey"
        assert data["id"] == "smoke-test-journey"
        assert data["status"] == "draft"
        api_delete(dashboard_server, f"/api/journeys/{data['id']}")

    def test_create_journey_missing_title(self, dashboard_server):
        status, data = api_post(dashboard_server, "/api/journeys", {})
        assert status == 400
        assert "title" in data.get("error", "").lower()


class TestJourneyList:
    def test_list_journeys(self, dashboard_server):
        data = api_get(dashboard_server, "/api/journeys")
        assert "journeys" in data

    def test_list_includes_created(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "List Test"})
        data = api_get(dashboard_server, "/api/journeys")
        ids = [jj["id"] for jj in data["journeys"]]
        assert j["id"] in ids
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")


class TestJourneyGet:
    def test_get_journey(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Get Test"})
        data = api_get(dashboard_server, f"/api/journeys/{j['id']}")
        assert data["title"] == "Get Test"
        assert "steps" in data
        assert "runs" in data
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")

    def test_get_nonexistent(self, dashboard_server):
        status, _ = safe_api_get(dashboard_server, "/api/journeys/nonexistent-999")
        assert status == 404


class TestJourneyUpdate:
    def test_update_title(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Before"})
        status, data = api_put(
            dashboard_server,
            f"/api/journeys/{j['id']}",
            {
                "title": "After",
            },
        )
        assert status == 200
        assert data["title"] == "After"
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")

    def test_update_status(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Status Test"})
        status, data = api_put(
            dashboard_server,
            f"/api/journeys/{j['id']}",
            {
                "status": "active",
            },
        )
        assert status == 200
        assert data["status"] == "active"
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")


class TestJourneyDelete:
    def test_delete_journey(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "To Delete"})
        status, _ = api_delete(dashboard_server, f"/api/journeys/{j['id']}")
        assert status == 200
        # Verify gone
        get_status, _ = safe_api_get(dashboard_server, f"/api/journeys/{j['id']}")
        assert get_status == 404


# ===========================================================================
# Step CRUD
# ===========================================================================


class TestStepCreate:
    def test_add_step(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Step Test"})
        status, step = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps",
            {
                "action": "open",
                "label": "Open board",
            },
        )
        assert status == 201
        assert step["action"] == "open"
        assert step["label"] == "Open board"
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")

    def test_add_step_with_target(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Target Test"})
        status, step = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps",
            {
                "action": "click",
                "label": "Click button",
                "target": {"testid": "submit-btn"},
            },
        )
        assert status == 201
        target = json.loads(step["target_json"])
        assert target == {"testid": "submit-btn"}
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")

    def test_add_step_invalid_action(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Invalid Step"})
        status, _data = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps",
            {
                "action": "bogus",
            },
        )
        assert status == 400
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")


class TestStepUpdate:
    def test_update_step_label(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Step Update"})
        _, step = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps",
            {
                "action": "open",
                "label": "Old",
            },
        )
        status, updated = api_put(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps/{step['id']}",
            {"label": "New"},
        )
        assert status == 200
        assert updated["label"] == "New"
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")


class TestStepDelete:
    def test_delete_step(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Step Delete"})
        _, step = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps",
            {
                "action": "open",
                "label": "To remove",
            },
        )
        status, _ = api_delete(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps/{step['id']}",
        )
        assert status == 200
        # Verify step is gone
        data = api_get(dashboard_server, f"/api/journeys/{j['id']}/steps")
        assert len(data["steps"]) == 0
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")


class TestStepReorder:
    def test_reorder_steps(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Reorder Test"})
        _, s1 = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps",
            {
                "action": "open",
                "label": "A",
            },
        )
        _, s2 = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps",
            {
                "action": "click",
                "label": "B",
                "target": {"testid": "x"},
            },
        )
        # Reverse
        status, _ = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps/reorder",
            {"step_ids": [s2["id"], s1["id"]]},
        )
        assert status == 200
        data = api_get(dashboard_server, f"/api/journeys/{j['id']}/steps")
        labels = [s["label"] for s in data["steps"]]
        assert labels == ["B", "A"]
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")


# ===========================================================================
# Validation
# ===========================================================================


class TestJourneyValidate:
    def test_validate_valid_journey(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Valid Journey"})
        api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/steps",
            {
                "action": "open",
                "label": "Open",
            },
        )
        status, data = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/validate",
            {},
        )
        assert status == 200
        assert data["ok"] is True
        assert "manifest" in data
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")

    def test_validate_empty_journey_fails(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Empty Journey"})
        status, data = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/validate",
            {},
        )
        assert status == 400
        assert "no steps" in data.get("error", "").lower()
        api_delete(dashboard_server, f"/api/journeys/{j['id']}")


# ===========================================================================
# Ticket Linking
# ===========================================================================


class TestTicketLinking:
    def _create_ticket(self, dashboard_server):
        """Create a test ticket and return its ID."""
        import time

        ts = int(time.time())
        _status, data = api_post(
            dashboard_server,
            "/api/tickets",
            {
                "title": f"Link test ticket {ts}",
                "section": "Backlog",
            },
        )
        return data.get("id", data.get("ticket", {}).get("id"))

    def test_link_and_unlink(self, dashboard_server):
        _, j = api_post(dashboard_server, "/api/journeys", {"title": "Link Test"})
        ticket_id = self._create_ticket(dashboard_server)
        if not ticket_id:
            pytest.skip("Could not create test ticket")

        # Link
        status, _ = api_post(
            dashboard_server,
            f"/api/journeys/{j['id']}/link",
            {"ticket_id": ticket_id},
        )
        assert status == 200

        # Verify in get
        data = api_get(dashboard_server, f"/api/journeys/{j['id']}")
        linked = [l["ticket_id"] for l in data.get("linked_tickets", [])]
        assert ticket_id in linked

        # Unlink
        status, _ = api_delete(
            dashboard_server,
            f"/api/journeys/{j['id']}/link/{ticket_id}",
        )
        assert status == 200

        api_delete(dashboard_server, f"/api/journeys/{j['id']}")
        api_delete(dashboard_server, f"/api/tickets/{ticket_id}")
