"""Smoke tests for pane_links API endpoints — exercises serve.py via fixture."""

from __future__ import annotations

import json
import urllib.request

import pytest
import requests


def _get_first_ticket_id(base_url):
    """Return the first ticket ID from the API."""
    with urllib.request.urlopen(f"{base_url}/api/tickets", timeout=10) as resp:
        data = json.loads(resp.read())
    if isinstance(data, list) and len(data) > 0:
        return data[0]["id"]
    elif isinstance(data, dict) and "tickets" in data:
        return data["tickets"][0]["id"]
    pytest.skip("No tickets in database")


@pytest.fixture(autouse=True)
def cleanup_test_panes(dashboard_server):
    """Delete any test pane rows after each test so they don't pollute subsequent runs."""
    yield
    # Delete all panes whose address contains 'test' (covers %test*, %sktest*)
    base = dashboard_server
    # List all pane addresses that look like test rows by pattern
    test_addresses = [
        "%9001",
        "%9002",
        "%9003",
        "%9011",
        "%9012",
        "%23",  # created by the double-unquote regression test
    ]
    for addr in test_addresses:
        try:
            requests.delete(f"{base}/api/pane-links/{addr}", timeout=3)
        except Exception:
            pass


def test_create_pane_link(dashboard_server):
    base = dashboard_server  # http://localhost:port/ticket-takeaway
    tid = _get_first_ticket_id(base)
    r = requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": "%9001", "host": "test-host", "pane_descriptor": "t:0.0"},
        timeout=5,
    )
    assert r.status_code in (200, 201), r.text


def test_list_pane_links_wraps_payload(dashboard_server):
    base = dashboard_server
    tid = _get_first_ticket_id(base)
    requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": "%9002", "host": "test-host", "pane_descriptor": "t:0.0"},
        timeout=5,
    )
    r = requests.get(f"{base}/api/tickets/{tid}/pane-links", timeout=5)
    data = r.json()
    assert "pane_links" in data, "API must wrap list in {pane_links: [...]}"
    assert any(p["pane_address"] == "%9002" for p in data["pane_links"])


def test_delete_pane_link(dashboard_server):
    base = dashboard_server
    tid = _get_first_ticket_id(base)
    requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": "%9003", "host": "test-host", "pane_descriptor": "t:0.0"},
        timeout=5,
    )
    r = requests.delete(f"{base}/api/pane-links/%9003", timeout=5)
    assert r.status_code == 200
    r = requests.get(f"{base}/api/tickets/{tid}/pane-links", timeout=5)
    assert not any(p["pane_address"] == "%9003" for p in r.json().get("pane_links", []))


def test_send_keys_validates_size(dashboard_server, monkeypatch):
    base = dashboard_server
    tid = _get_first_ticket_id(base)
    requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": "%9011", "host": "test-host", "pane_descriptor": "t:0.0"},
        timeout=5,
    )
    big = "x" * (5 * 1024)  # >4KB
    r = requests.post(
        f"{base}/api/pane-links/%9011/send-keys",
        json={"text": big, "press_enter": False},
        timeout=5,
    )
    assert r.status_code == 413


def test_send_keys_rejects_null_bytes(dashboard_server):
    base = dashboard_server
    tid = _get_first_ticket_id(base)
    requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": "%9012", "host": "test-host", "pane_descriptor": "t:0.0"},
        timeout=5,
    )
    r = requests.post(
        f"{base}/api/pane-links/%9012/send-keys",
        json={"text": "hello\x00world", "press_enter": False},
        timeout=5,
    )
    assert r.status_code == 400


# Regression test for fix #1: percent-encoded pane IDs must not be double-decoded.
# A pane_address of "%23" (literal) is stored in the DB.
# It must be accessible via /api/pane-links/%2523 (URL-encoded form of "%23").
# With the old double-unquote bug, %2523 would decode to %23 on path decode, then
# unquote() would decode %23 → '#', causing a 404 on DB lookup.
def test_pane_address_with_percent_not_double_decoded(dashboard_server):
    base = dashboard_server
    tid = _get_first_ticket_id(base)
    # Create a link with pane_address = "%23" (the literal string used by tmux for pane 23)
    r = requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={
            "pane_address": "%23",
            "host": "test-host",
            "pane_descriptor": "vibe:0.0",
        },
        timeout=5,
    )
    assert r.status_code in (200, 201), f"create failed: {r.text}"

    # Verify list shows the row
    r = requests.get(f"{base}/api/tickets/{tid}/pane-links", timeout=5)
    links = r.json().get("pane_links", [])
    assert any(p["pane_address"] == "%23" for p in links), "row not found after create"

    # send-keys: requests encodes the URL, so the path becomes /api/pane-links/%2523/send-keys
    # The server decodes %2523 → %23. With the old bug, a second unquote would decode %23 → '#'.
    # We expect 409 (cross-host) or 404 (no tmux), NOT 404-from-db-miss.
    # The critical assertion: status must not be 404 (which would indicate DB lookup failed).
    r = requests.post(
        f"{base}/api/pane-links/%23/send-keys",
        json={"text": "echo hi", "press_enter": False},
        timeout=5,
    )
    # 409 = cross-host (expected in CI, no real tmux on test-host)
    # 502 = tmux subprocess failed (also fine — means DB lookup succeeded)
    # 404 = DB lookup failed (the bug we fixed)
    assert r.status_code != 404, (
        f"send-keys returned 404 — pane_address lookup failed (double-unquote bug?): {r.text}"
    )

    # DELETE: same encoding — should succeed (200) not 404
    r = requests.delete(f"{base}/api/pane-links/%23", timeout=5)
    assert r.status_code == 200, f"delete returned {r.status_code}: {r.text}"

    # Confirm gone
    r = requests.get(f"{base}/api/tickets/{tid}/pane-links", timeout=5)
    links = r.json().get("pane_links", [])
    assert not any(p["pane_address"] == "%23" for p in links), (
        "row still present after delete"
    )
