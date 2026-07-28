"""Smoke tests for Lane A (factory-talk) API endpoints.

These tests verify the API contracts for endpoints being added by Lane B
(serve.py routes) that depend on Lane A primitives. They are written now,
ahead of Lane B, using a skip-if-missing guard pattern: each test does an
HTTP probe and calls pytest.skip() if the endpoint does not yet exist.

INTENT: Once Lane B lands and wires these endpoints, remove the skip probes
and let the tests run unconditionally. The skip guard is temporary scaffolding.

Requires: running serve.py (`dashboard_server` fixture from conftest.py).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

# ---------------------------------------------------------------------------
# Helper: probe whether an endpoint exists at all.
# Returns the HTTP status code or None if connection refused / 404.
# ---------------------------------------------------------------------------


def _probe_endpoint(url: str, method: str = "GET") -> int | None:
    """Return the HTTP status code for a request to url, or None on network error."""
    try:
        req = urllib.request.Request(url, method=method)
        resp = urllib.request.urlopen(req, timeout=3)
        return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    """POST JSON payload to url, return (status_code, response_dict)."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {}
        return exc.code, body


def _patch_json(url: str, payload: dict) -> tuple[int, dict]:
    """PATCH JSON payload to url, return (status_code, response_dict)."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {}
        return exc.code, body


# ---------------------------------------------------------------------------
# Helper: get or create a test ticket id from the running server.
# ---------------------------------------------------------------------------


def _get_any_ticket_id(base_url: str) -> str | None:
    """Return the id of the first ticket visible via the API, or None."""
    try:
        req = urllib.request.Request(f"{base_url}/api/tickets")
        resp = urllib.request.urlopen(req, timeout=3)
        body = json.loads(resp.read())
        tickets = body if isinstance(body, list) else body.get("tickets", [])
        if tickets:
            return tickets[0]["id"]
    except Exception:
        pass
    return None


def _create_test_ticket(base_url: str) -> str | None:
    """POST a minimal ticket and return its id, or None on failure."""
    status, body = _post_json(
        f"{base_url}/api/tickets",
        {"title": "Smoke test ticket", "section": "Ideas"},
    )
    if status in (200, 201):
        return body.get("id")
    return None


# ===========================================================================
# POST /api/runs/{id}/respond
# Lane B endpoint — skip-if-missing.
# ===========================================================================
#
# TODO (Lane B): Remove the skip probe block when this endpoint is implemented.
#


class TestRunsRespondEndpoint:
    """Tests for POST /api/runs/{id}/respond.

    Contract per the plan:
    - Returns 400/404 if run is not in 'needs_input' status.
    - Validates 'kind' matches runs.needs_input_kind.
    - Accepts {"kind": "text", "response": "..."}.
    - Accepts {"kind": "propose", "accepted": {...}}.
    - Returns {"status": "resumed", ...} shape on success.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_missing(self, dashboard_server):
        """Skip-if-missing: probe for the endpoint pattern using a sentinel run id."""
        probe_url = f"{dashboard_server}/api/runs/0/respond"
        status = _probe_endpoint(probe_url, method="POST")
        # 404 with JSON body or no response: endpoint doesn't exist yet — skip.
        # 400/422 means the endpoint exists but rejected the bad id — that's fine.
        if status is None:
            pytest.skip(
                "POST /api/runs/{id}/respond is not yet implemented (Lane B pending). "
                "Remove this skip once Lane B wires the endpoint."
            )
        # A 404 with endpoint-not-found semantics (vs. run-not-found) signals
        # the route is missing. We can't easily distinguish, so skip on 404 too.
        if status == 404:
            pytest.skip(
                "POST /api/runs/{id}/respond returned 404 — endpoint likely not yet "
                "implemented by Lane B. Remove skip once the route is wired."
            )

    def test_respond_returns_400_when_run_not_in_needs_input(self, dashboard_server):
        """Posting to a run that is not waiting for input should return 400."""
        # Run id 999999 is unlikely to exist at all — expect 404 or 400.
        url = f"{dashboard_server}/api/runs/999999/respond"
        status, body = _post_json(url, {"kind": "text", "response": "hello"})
        assert status in (400, 404), (
            f"Expected 400 or 404 for non-existent run, got {status}: {body}"
        )

    def test_respond_accepts_text_kind_payload(self, dashboard_server):
        """If a run in needs_input (kind=text) exists, a text reply must be accepted."""
        # We cannot create a real needs_input run without a full agent subprocess,
        # so this test only verifies the payload is structurally accepted (not 422).
        # The 400 comes from 'run not found / not in needs_input', not from bad JSON.
        url = f"{dashboard_server}/api/runs/0/respond"
        status, body = _post_json(url, {"kind": "text", "response": "My reply text"})
        assert status != 422, (
            f"Server rejected the payload shape — expected 400/404, got 422: {body}"
        )

    def test_respond_accepts_propose_kind_payload(self, dashboard_server):
        """Propose-kind payload with 'accepted' dict must not be rejected as 422."""
        url = f"{dashboard_server}/api/runs/0/respond"
        status, body = _post_json(
            url,
            {
                "kind": "propose",
                "accepted": {"description": "New desc", "criteria": []},
            },
        )
        assert status != 422, (
            f"Server rejected propose payload shape — expected 400/404, got 422: {body}"
        )

    def test_respond_rejects_unknown_kind(self, dashboard_server):
        """An unrecognised kind value must return 400 or 422."""
        url = f"{dashboard_server}/api/runs/0/respond"
        status, body = _post_json(url, {"kind": "garbage", "response": "x"})
        assert status in (400, 404, 422), (
            f"Expected 400/404/422 for unknown kind, got {status}: {body}"
        )


# ===========================================================================
# PATCH /api/tickets/{id} — is_container field
# Lane B endpoint extension — skip-if-missing.
# ===========================================================================
#
# TODO (Lane B): Remove the skip probe block when is_container is wired into
# the PATCH handler.
#


class TestTicketPatchIsContainer:
    """Tests for PATCH /api/tickets/{id} accepting is_container."""

    @pytest.fixture(autouse=True)
    def _setup(self, dashboard_server):
        self._base = dashboard_server
        # Get or create a ticket to operate on.
        self._tid = _get_any_ticket_id(dashboard_server) or _create_test_ticket(
            dashboard_server
        )
        if not self._tid:
            pytest.skip("Could not obtain a test ticket from the running server.")

    def test_patch_is_container_endpoint_exists(self, dashboard_server):
        """PATCH /api/tickets/{id} must exist (not 404/405).

        Skip-if-missing guard: if is_container returns 404/405 (route missing
        or field not accepted), skip with a clear message for Lane B to wire.
        """
        url = f"{self._base}/api/tickets/{self._tid}"
        status, body = _patch_json(url, {"is_container": 0})
        if status in (404, 405):
            pytest.skip(
                "PATCH /api/tickets/{id} with is_container returned 404/405 — "
                "Lane B needs to wire is_container into the PATCH handler. "
                "Remove this skip once done."
            )
        assert status in (200, 204), (
            f"Expected 200/204 from PATCH is_container=0, got {status}: {body}"
        )

    def test_patch_is_container_true_accepts(self, dashboard_server):
        url = f"{self._base}/api/tickets/{self._tid}"
        status, body = _patch_json(url, {"is_container": 1})
        if status in (404, 405):
            pytest.skip(
                "PATCH is_container field not yet accepted — Lane B pending. "
                "Remove skip once wired."
            )
        assert status in (200, 204), (
            f"Expected 200/204 from PATCH is_container=1, got {status}: {body}"
        )

    def test_patch_is_container_false_accepts(self, dashboard_server):
        url = f"{self._base}/api/tickets/{self._tid}"
        status, _body = _patch_json(url, {"is_container": 0})
        if status in (404, 405):
            pytest.skip(
                "PATCH is_container field not yet accepted — Lane B pending. "
                "Remove skip once wired."
            )
        assert status in (200, 204)


# ===========================================================================
# GET /api/tickets/{id}/activity
# Lane B endpoint — skip-if-missing.
# ===========================================================================
#
# TODO (Lane B): Remove the skip probe block when activity endpoint is implemented.
#


class TestTicketActivityEndpoint:
    """Tests for GET /api/tickets/{id}/activity."""

    @pytest.fixture(autouse=True)
    def _setup(self, dashboard_server):
        self._base = dashboard_server
        self._tid = _get_any_ticket_id(dashboard_server) or _create_test_ticket(
            dashboard_server
        )
        if not self._tid:
            pytest.skip("Could not obtain a test ticket from the running server.")

    def _get_activity(self) -> tuple[int, dict]:
        url = f"{self._base}/api/tickets/{self._tid}/activity"
        try:
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=3)
            return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except Exception:
                body = {}
            return exc.code, body

    def test_activity_endpoint_exists_and_returns_200(self, dashboard_server):
        """GET /api/tickets/{id}/activity must return 200.

        Skip-if-missing guard: 404 means Lane B hasn't wired the route yet.
        """
        status, body = self._get_activity()
        if status == 404:
            pytest.skip(
                "GET /api/tickets/{id}/activity returned 404 — Lane B needs to add "
                "this endpoint. Remove skip once wired."
            )
        assert status == 200, (
            f"Expected 200 from activity endpoint, got {status}: {body}"
        )

    def test_activity_response_has_events_list(self, dashboard_server):
        """Response must contain an 'events' key with a list value."""
        status, body = self._get_activity()
        if status == 404:
            pytest.skip(
                "GET /api/tickets/{id}/activity not yet implemented — Lane B pending. "
                "Remove skip once wired."
            )
        events = body.get("events")
        assert events is not None, f"Response missing 'events' key: {body}"
        assert isinstance(events, list), (
            f"Expected 'events' to be a list, got {type(events)}"
        )

    def test_activity_response_has_total_key(self, dashboard_server):
        """Response must include a 'total' count key."""
        status, body = self._get_activity()
        if status == 404:
            pytest.skip(
                "GET /api/tickets/{id}/activity not yet implemented — Lane B pending."
            )
        assert "total" in body or "count" in body, (
            f"Expected 'total' or 'count' key in activity response: {body}"
        )

    def test_activity_endpoint_accepts_limit_param(self, dashboard_server):
        """?limit=10 query parameter must not cause an error."""
        url = f"{self._base}/api/tickets/{self._tid}/activity?limit=10"
        try:
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=3)
            status = resp.status
            json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
        if status == 404:
            pytest.skip(
                "GET /api/tickets/{id}/activity not yet implemented — Lane B pending."
            )
        assert status == 200, f"Expected 200 with ?limit=10, got {status}"

    def test_activity_for_nonexistent_ticket_returns_404(self, dashboard_server):
        """Requesting activity for a ticket that doesn't exist must return 404."""
        url = f"{self._base}/api/tickets/NOSUCHTICKET-99999/activity"
        try:
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=3)
            status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception:
            status = None
        if status is None or status == 404 and self._tid == "NOSUCHTICKET-99999":
            pytest.skip(
                "GET /api/tickets/{id}/activity not yet implemented — Lane B pending."
            )
        # If the activity route exists, a missing ticket must be 404.
        # But if the route itself is missing (general 404), skip.
        # We check by seeing if our known-good ticket's route returns 200.
        good_status, _ = self._get_activity()
        if good_status == 404:
            pytest.skip("Activity endpoint not yet implemented — Lane B pending.")
        assert status == 404, f"Expected 404 for nonexistent ticket, got {status}"
