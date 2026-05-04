"""Smoke tests for Phase 3A API endpoints.

Covers:
 A. Workflow CRUD with new fields + system workflow 403 guard
 B. Condition catalog (project-agnostic)
 C. Eligibility inspector
 D. Kitchen settings GET + PUT
 E. Run observability (active, recent, detail, evidence)

Uses the shared dashboard_server fixture from conftest.py.
"""

import json
import time
import urllib.error
import urllib.request

import pytest

from conftest import api_delete, api_get, api_post, api_put


def _raw_put(base_url, path, body):
    """PUT JSON, return (status_code, dict). Safe even when body is empty on error."""
    url = f"{base_url}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"},
                                  method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            return e.code, json.loads(body_bytes) if body_bytes else {}
        except Exception:
            return e.code, {}


def _raw_post(base_url, path, body):
    """POST JSON, return (status_code, dict). Safe even when body is empty on error."""
    url = f"{base_url}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            return e.code, json.loads(body_bytes) if body_bytes else {}
        except Exception:
            return e.code, {}


def _raw_delete(base_url, path):
    """DELETE, return (status_code, dict). Safe even when body is empty on error."""
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            return e.code, json.loads(body_bytes) if body_bytes else {}
        except Exception:
            return e.code, {}


# ---------------------------------------------------------------------------
# A. Workflow CRUD — new fields + system workflow guard
# ---------------------------------------------------------------------------

def test_list_workflows_returns_new_fields(dashboard_server):
    """GET /api/workflow/workflows returns workflows with trigger_json etc."""
    data = api_get(dashboard_server, "/api/workflow/workflows")
    assert "workflows" in data
    assert isinstance(data["workflows"], list)
    # Every workflow must carry the Phase 3A fields (may be null/0)
    for wf in data["workflows"]:
        assert "system" in wf
        assert "enabled" in wf
        assert "trigger_json" in wf    # parsed or null
        assert "on_success_json" in wf  # parsed or null
        assert "subject_type" in wf
        assert "steps" in wf           # parsed list


def test_create_workflow_with_trigger_json(dashboard_server):
    """POST /api/workflow/workflows accepts trigger_json as object."""
    wid = f"p3a-smoke-{int(time.time())}"
    trigger = {"all_of": [{"kind": "section_equals", "value": "Backlog"}]}
    status, data = api_post(dashboard_server, "/api/workflow/workflows", {
        "id": wid,
        "name": "Phase 3A smoke test workflow",
        "trigger_json": trigger,
        "enabled": True,
        "subject_type": "ticket",
        "steps": [],
    })
    assert status == 201, data
    assert data["id"] == wid
    # trigger_json should be returned as parsed object
    assert isinstance(data.get("trigger_json"), dict)
    assert data["system"] == 0  # user-created, never system

    # cleanup
    api_delete(dashboard_server, f"/api/workflow/workflows/{wid}")


def test_update_system_workflow_enabled_allowed(dashboard_server):
    """PUT /api/workflow/workflows/{id} toggling enabled on a system workflow returns 200."""
    # Find a system workflow (seeded by workflows_seed.py)
    data = api_get(dashboard_server, "/api/workflow/workflows")
    system_wfs = [w for w in data["workflows"] if w.get("system")]
    if not system_wfs:
        pytest.skip("No system workflows seeded — nothing to test")
    wid = system_wfs[0]["id"]
    current_enabled = system_wfs[0].get("enabled", 1)
    status, resp = _raw_put(dashboard_server, f"/api/workflow/workflows/{wid}", {
        "enabled": 0 if current_enabled else 1,
    })
    assert status == 200, resp
    # Restore
    _raw_put(dashboard_server, f"/api/workflow/workflows/{wid}", {"enabled": current_enabled})


def test_update_system_workflow_other_fields_forbidden(dashboard_server):
    """PUT /api/workflow/workflows/{id} with non-enabled fields on a system workflow returns 403."""
    data = api_get(dashboard_server, "/api/workflow/workflows")
    system_wfs = [w for w in data["workflows"] if w.get("system")]
    if not system_wfs:
        pytest.skip("No system workflows seeded")
    wid = system_wfs[0]["id"]
    status, resp = _raw_put(dashboard_server, f"/api/workflow/workflows/{wid}", {
        "name": "hacked name",
    })
    assert status == 403, resp
    assert resp.get("error") == "system_workflow"


def test_delete_system_workflow_forbidden(dashboard_server):
    """DELETE /api/workflow/workflows/{id} on a system workflow returns 403."""
    data = api_get(dashboard_server, "/api/workflow/workflows")
    system_wfs = [w for w in data["workflows"] if w.get("system")]
    if not system_wfs:
        pytest.skip("No system workflows seeded")
    wid = system_wfs[0]["id"]
    status, resp = _raw_delete(dashboard_server, f"/api/workflow/workflows/{wid}")
    assert status == 403, resp
    assert resp.get("error") == "system_workflow"


def test_duplicate_system_workflow_creates_user_copy(dashboard_server):
    """POST /api/workflow/workflows/{id}/duplicate clones a system row to a
    user-owned row. Even disabled system workflows can be duplicated.

    Phase A migration: user-facing duplicate path so users can customise
    behaviour by cloning a system workflow then editing the clone.
    """
    data = api_get(dashboard_server, "/api/workflow/workflows")
    system_wfs = [w for w in data["workflows"] if w.get("system")]
    if not system_wfs:
        pytest.skip("No system workflows seeded")

    # Prefer a disabled system workflow if available — verifies that
    # duplicate works regardless of enabled state.
    disabled = [w for w in system_wfs if not w.get("enabled")]
    src = disabled[0] if disabled else system_wfs[0]
    src_id = src["id"]

    status, resp = _raw_post(
        dashboard_server,
        f"/api/workflow/workflows/{src_id}/duplicate",
        {},
    )
    assert status == 201, resp
    assert resp.get("system") == 0, "duplicate must NOT carry the system flag"
    assert resp.get("id") != src_id, "duplicate must have a different id"
    assert "(copy)" in (resp.get("name") or "").lower() or resp.get("name")

    # Cleanup the user-owned duplicate.
    api_delete(dashboard_server, f"/api/workflow/workflows/{resp['id']}")


def test_duplicate_nonexistent_workflow_404(dashboard_server):
    status, resp = _raw_post(
        dashboard_server,
        "/api/workflow/workflows/does-not-exist-xyz/duplicate",
        {},
    )
    assert status == 404, resp


# ---------------------------------------------------------------------------
# B. Condition catalog
# ---------------------------------------------------------------------------

def test_condition_catalog_returns_catalog(dashboard_server):
    """GET /api/workflow-conditions/catalog returns conditions list (project-agnostic)."""
    # Strip the project prefix — this is a global endpoint
    # dashboard_server is e.g. http://localhost:PORT/ticket-takeaway
    base = dashboard_server.rsplit("/", 1)[0]  # http://localhost:PORT
    with urllib.request.urlopen(f"{base}/api/workflow-conditions/catalog", timeout=10) as resp:
        import json
        data = json.loads(resp.read())
    assert "conditions" in data
    assert isinstance(data["conditions"], list)
    assert len(data["conditions"]) > 0
    # Each entry must have kind, label, params
    for entry in data["conditions"]:
        assert "kind" in entry
        assert "label" in entry
        assert "params" in entry
        # Must NOT have evaluator (not JSON-serializable, should be stripped)
        assert "evaluator" not in entry

    # Phase A: the four new system-workflow conditions must be exposed.
    kinds = {c["kind"] for c in data["conditions"]}
    for required in (
        "children_have_open_bugs",
        "children_no_open_bugs",
        "children_all_status_in",
        "children_any_status_in",
    ):
        assert required in kinds, f"{required!r} missing from catalog"


# ---------------------------------------------------------------------------
# C. Eligibility inspector
# ---------------------------------------------------------------------------

def _get_first_ticket_id_raw(base_url):
    import json as _json
    with urllib.request.urlopen(f"{base_url}/api/tickets", timeout=10) as r:
        data = _json.loads(r.read())
    if isinstance(data, list) and data:
        return data[0]["id"]
    if isinstance(data, dict):
        tickets = data.get("tickets") or data.get("items") or []
        if tickets:
            return tickets[0]["id"]
    pytest.skip("No tickets in database")


def test_inspect_returns_per_condition_results(dashboard_server):
    """POST /api/workflows/inspect returns workflow eligibility per condition."""
    tid = _get_first_ticket_id_raw(dashboard_server)
    status, data = api_post(dashboard_server, "/api/workflows/inspect", {"ticket_id": tid})
    assert status == 200, data
    assert data.get("ticket_id") == tid
    assert "subject_context_summary" in data
    ctx = data["subject_context_summary"]
    assert "section" in ctx
    assert "automation_mode" in ctx
    assert "criteria_count" in ctx
    assert "workflows" in data
    for wf in data["workflows"]:
        assert "workflow_id" in wf
        assert "name" in wf
        assert "passed" in wf
        assert "conditions" in wf


def test_inspect_missing_ticket_returns_404(dashboard_server):
    """POST /api/workflows/inspect with non-existent ticket_id returns 404."""
    status, data = _raw_post(dashboard_server, "/api/workflows/inspect", {"ticket_id": "NONEXISTENT-9999"})
    assert status == 404, data


# ---------------------------------------------------------------------------
# D. Kitchen settings
# ---------------------------------------------------------------------------

def test_get_kitchen_settings(dashboard_server):
    """GET /api/settings/kitchen returns kitchen settings dict."""
    data = api_get(dashboard_server, "/api/settings/kitchen")
    assert "settings" in data
    s = data["settings"]
    assert isinstance(s, dict)
    # paused is always present (from live kitchen state)
    assert "paused" in s
    assert isinstance(s["paused"], bool)


def test_put_kitchen_settings_valid(dashboard_server):
    """PUT /api/settings/kitchen with valid booleans returns updated settings."""
    # Read current state first
    orig = api_get(dashboard_server, "/api/settings/kitchen")["settings"]
    orig_flag = orig.get("use_db_workflows", False)

    status, data = _raw_put(dashboard_server, "/api/settings/kitchen", {
        "use_db_workflows": not orig_flag,
    })
    assert status == 200, data
    assert "settings" in data
    assert data["settings"]["use_db_workflows"] is not orig_flag

    # Restore
    _raw_put(dashboard_server, "/api/settings/kitchen", {"use_db_workflows": orig_flag})


def test_put_kitchen_settings_invalid_type(dashboard_server):
    """PUT /api/settings/kitchen with wrong type for boolean returns 400."""
    status, data = _raw_put(dashboard_server, "/api/settings/kitchen", {
        "use_db_workflows": "not-a-bool",
    })
    assert status == 400, data
    assert "error" in data


# ---------------------------------------------------------------------------
# E. Run observability
# ---------------------------------------------------------------------------

def test_active_runs_returns_list(dashboard_server):
    """GET /api/runs/active returns runs list (may be empty)."""
    data = api_get(dashboard_server, "/api/runs/active")
    assert "runs" in data
    assert isinstance(data["runs"], list)
    for run in data["runs"]:
        assert "id" in run
        assert "status" in run


def test_recent_runs_returns_list(dashboard_server):
    """GET /api/runs/recent returns runs list with correct shape."""
    data = api_get(dashboard_server, "/api/runs/recent?limit=5")
    assert "runs" in data
    assert isinstance(data["runs"], list)
    for run in data["runs"]:
        assert "id" in run
        assert "status" in run
        assert "workflow_meta" in run  # may be None


def test_run_detail_not_found(dashboard_server):
    """GET /api/runs/99999999 returns 404 for non-existent run."""
    try:
        urllib.request.urlopen(f"{dashboard_server}/api/runs/99999999", timeout=10)
        pytest.fail("Expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_run_evidence_not_found(dashboard_server):
    """GET /api/runs/99999999/evidence returns 404 for non-existent run."""
    try:
        urllib.request.urlopen(f"{dashboard_server}/api/runs/99999999/evidence", timeout=10)
        pytest.fail("Expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404
