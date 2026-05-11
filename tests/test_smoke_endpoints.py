"""API smoke tests for /api/endpoints.

Uses the same dashboard_server fixture as other smoke tests. NOTE:
this fixture requires serve.py to actually start, which is reliable on
WSL but hangs on macOS (~/.claude/ticket-takeaway/CLAUDE.md gotcha:
socket.getfqdn). Run these tests on WSL.
"""
import json
import pytest
import urllib.request
import urllib.error


@pytest.fixture
def api_url(dashboard_server):
    """dashboard_server is project-scoped; endpoints are global."""
    # Extract base URL (strip project path)
    base = dashboard_server.rsplit("/", 1)[0]
    return f"{base}/api/endpoints"


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, json.loads(r.read().decode())


def _post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _put(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, json.loads(body) if body else {}


def _delete(url):
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, json.loads(body) if body else {}


def test_get_endpoints_returns_seed(api_url):
    status, body = _get(api_url)
    assert status == 200
    assert "endpoints" in body
    ids = {e["id"] for e in body["endpoints"]}
    assert "claude-cli" in ids
    assert "codex-cli" in ids
    assert "codex-exec-readonly" in ids
    assert "hermes-cli" in ids
