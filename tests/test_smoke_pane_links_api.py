"""Smoke tests for pane_links API endpoints — exercises serve.py via fixture."""
from __future__ import annotations
import pytest, requests, urllib.request, json


def _get_first_ticket_id(base_url):
    """Return the first ticket ID from the API."""
    with urllib.request.urlopen(f"{base_url}/api/tickets", timeout=10) as resp:
        data = json.loads(resp.read())
    if isinstance(data, list) and len(data) > 0:
        return data[0]["id"]
    elif isinstance(data, dict) and "tickets" in data:
        return data["tickets"][0]["id"]
    pytest.skip("No tickets in database")


def test_create_pane_link(dashboard_server):
    base = dashboard_server  # http://localhost:port/ticket-takeaway
    tid = _get_first_ticket_id(base)
    r = requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": "%test1", "host": "test-host", "pane_descriptor": "t:0.0"},
        timeout=5,
    )
    assert r.status_code in (200, 201), r.text


def test_list_pane_links_wraps_payload(dashboard_server):
    base = dashboard_server
    tid = _get_first_ticket_id(base)
    requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": "%test2", "host": "test-host", "pane_descriptor": "t:0.0"},
        timeout=5,
    )
    r = requests.get(f"{base}/api/tickets/{tid}/pane-links", timeout=5)
    data = r.json()
    assert "pane_links" in data, "API must wrap list in {pane_links: [...]}"
    assert any(p["pane_address"] == "%test2" for p in data["pane_links"])


def test_delete_pane_link(dashboard_server):
    base = dashboard_server
    tid = _get_first_ticket_id(base)
    requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": "%test3", "host": "test-host", "pane_descriptor": "t:0.0"},
        timeout=5,
    )
    r = requests.delete(f"{base}/api/pane-links/%test3", timeout=5)
    assert r.status_code == 200
    r = requests.get(f"{base}/api/tickets/{tid}/pane-links", timeout=5)
    assert not any(p["pane_address"] == "%test3" for p in r.json().get("pane_links", []))


def test_send_keys_validates_size(dashboard_server, monkeypatch):
    base = dashboard_server
    tid = _get_first_ticket_id(base)
    requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": "%sktest", "host": "test-host", "pane_descriptor": "t:0.0"},
        timeout=5,
    )
    big = "x" * (5 * 1024)  # >4KB
    r = requests.post(
        f"{base}/api/pane-links/%sktest/send-keys",
        json={"text": big, "press_enter": False},
        timeout=5,
    )
    assert r.status_code == 413


def test_send_keys_rejects_null_bytes(dashboard_server):
    base = dashboard_server
    tid = _get_first_ticket_id(base)
    requests.post(
        f"{base}/api/tickets/{tid}/pane-links",
        json={"pane_address": "%sktest2", "host": "test-host", "pane_descriptor": "t:0.0"},
        timeout=5,
    )
    r = requests.post(
        f"{base}/api/pane-links/%sktest2/send-keys",
        json={"text": "hello\x00world", "press_enter": False},
        timeout=5,
    )
    assert r.status_code == 400
