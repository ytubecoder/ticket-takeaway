"""API smoke tests for /api/endpoints.

Uses the same dashboard_server fixture as other smoke tests. NOTE:
this fixture requires serve.py to actually start, which is reliable on
WSL but hangs on macOS (~/.claude/ticket-takeaway/CLAUDE.md gotcha:
socket.getfqdn). Run these tests on WSL.
"""
import json
import uuid
import pytest
import urllib.request
import urllib.error


def _unique_id(prefix: str) -> str:
    """Return a unique endpoint ID for a test run to avoid cross-run collisions."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


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


def test_post_creates_user_endpoint(api_url):
    ep_id = _unique_id("test-user-ep")
    status, body = _post(api_url, {
        "id": ep_id,
        "name": "Test User Endpoint",
        "endpoint_type": "cli",
        "command": "echo",
        "args": ["{prompt}"],
    })
    assert status == 201
    assert body["id"] == ep_id
    assert body["system"] == 0
    # cleanup (best-effort; DELETE may not exist yet)
    _delete(f"{api_url}/{ep_id}")


def test_post_rejects_invalid_id(api_url):
    status, body = _post(api_url, {
        "id": "bad id with spaces",
        "name": "x",
        "endpoint_type": "cli",
        "command": "echo",
    })
    assert status == 400
    assert "error" in body


def test_post_rejects_duplicate_id(api_url):
    ep_id = _unique_id("dup-test")
    status, _ = _post(api_url, {
        "id": ep_id,
        "name": "x",
        "endpoint_type": "cli",
        "command": "echo",
    })
    assert status == 201
    status, _ = _post(api_url, {
        "id": ep_id,
        "name": "x",
        "endpoint_type": "cli",
        "command": "echo",
    })
    assert status == 409
    _delete(f"{api_url}/{ep_id}")  # best-effort cleanup


def test_post_rejects_api_type_without_api_key_env(api_url):
    status, body = _post(api_url, {
        "id": _unique_id("api-no-key"),
        "name": "x",
        "endpoint_type": "openai_api",
        "provider": "openai",
    })
    assert status == 400
    assert "api_key_env" in str(body)


def test_post_rejects_args_not_array_of_strings(api_url):
    status, body = _post(api_url, {
        "id": _unique_id("bad-args"),
        "name": "x",
        "endpoint_type": "cli",
        "command": "echo",
        "args": ["ok", 42, "also-ok"],
    })
    assert status == 400
    assert "[1]" in str(body) or "index 1" in str(body)


def test_put_updates_user_endpoint(api_url):
    eid = _unique_id("put-test")
    _post(api_url, {"id": eid, "name": "x",
                    "endpoint_type": "cli", "command": "echo"})
    status, body = _put(f"{api_url}/{eid}", {"name": "renamed"})
    assert status == 200
    assert body["name"] == "renamed"
    _delete(f"{api_url}/{eid}")


def test_put_system_endpoint_returns_403(api_url):
    status, body = _put(f"{api_url}/claude-cli", {"name": "evil"})
    assert status == 403
    assert body.get("error") == "system_endpoint"


def test_delete_system_endpoint_returns_403(api_url):
    status, body = _delete(f"{api_url}/claude-cli")
    assert status == 403


def test_delete_user_endpoint_returns_unlinked_count(api_url):
    eid = _unique_id("del-test")
    _post(api_url, {"id": eid, "name": "x",
                    "endpoint_type": "cli", "command": "echo"})
    status, body = _delete(f"{api_url}/{eid}")
    # 204 has empty body; 200 has agents_unlinked
    assert status in (200, 204)
    if status == 200:
        assert "agents_unlinked" in body
