"""Shared fixtures for Ticket Takeaway dashboard browser tests."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time

import pytest
from playwright.sync_api import sync_playwright

SERVE_PY = os.path.join(os.path.dirname(__file__), "..", "src", "serve.py")
GENERATE_PY = os.path.join(os.path.dirname(__file__), "..", "src", "generate.py")
CLI_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "tickets-cli.py")
GEN_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "generate.py")

# ---------------------------------------------------------------------------
# Import tickets-cli.py and generate.py (hyphenated filename requires importlib)
# Ensure src/ is on sys.path so `from constants import ...` works inside them.
# ---------------------------------------------------------------------------

_src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

_cli_spec = importlib.util.spec_from_file_location("tickets_cli", CLI_PATH)
cli_mod = importlib.util.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(cli_mod)

_gen_spec = importlib.util.spec_from_file_location("generate", GEN_PATH)
gen_mod = importlib.util.module_from_spec(_gen_spec)
_gen_spec.loader.exec_module(gen_mod)

MOCK_GATE_RESPONSE = {
    "verdict": "ready",
    "summary": "All checks passed",
    "categories": {
        "D": {"status": "ok", "current_summary": "Description is complete"},
        "C": {"status": "ok", "current_summary": "Criteria defined"},
        "T": {"status": "ok", "current_summary": "Tests written"},
        "R": {"status": "ok", "current_summary": "Review done"},
        "S": {"status": "ok", "current_summary": "Smoke tested"},
    },
}

MOCK_TICKET_RESPONSE = {
    "id": "B-01",
    "title": "Mock ticket",
    "priority": "medium",
    "complexity": "M",
    "status": "in-progress",
    "description": "A test ticket",
    "acceptance_criteria": [],
    "readiness_flags": {},
}


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pytest_addoption(parser):
    """Register custom CLI options for the scenario runner."""
    parser.addoption(
        "--scenario-id",
        action="store",
        default=None,
        metavar="ID",
        help="Run only the scenario with this exact ID (skips all others).",
    )
    parser.addoption(
        "--publish",
        action="store_true",
        default=False,
        help=(
            "After each scenario run, write a run-summary.json file "
            "alongside the captured screenshots (for gallery publishing)."
        ),
    )


@pytest.fixture(scope="session")
def dashboard_server():
    """Start serve.py on a free port, yield the base URL, kill on teardown.

    If the environment variable TT_SCENARIO_BASE_URL is set, that URL is
    yielded directly without starting a local server — useful for running
    scenario tests against an already-running instance (CI, staging, etc.).
    """
    external_url = os.environ.get("TT_SCENARIO_BASE_URL", "").strip()
    if external_url:
        # Use the externally provided URL as-is; no server to manage.
        yield external_url.rstrip("/")
        return

    # Regenerate dashboard HTML from current generate.py source
    project_dir = os.path.join(os.path.dirname(__file__), "..")
    subprocess.run(
        [sys.executable, GENERATE_PY],
        cwd=project_dir,
        timeout=30,
    )
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, SERVE_PY, "--port", str(port), "--project", "ticket-takeaway"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for server to be ready
    base_url = f"http://localhost:{port}"
    for _ in range(40):
        try:
            import urllib.request
            urllib.request.urlopen(base_url, timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    else:
        proc.kill()
        raise RuntimeError(f"Dashboard server failed to start on port {port}")
    # Multi-project routing prefixes all API paths with /{project-id}
    base_url = f"{base_url}/ticket-takeaway"
    yield base_url
    proc.kill()
    proc.wait()


@pytest.fixture(scope="session")
def browser():
    """Session-scoped Playwright Chromium browser."""
    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()
    pw.stop()


@pytest.fixture()
def page(browser, dashboard_server):
    """Per-test browser page with gate-check API mocked."""
    ctx = browser.new_context()
    p = ctx.new_page()

    def mock_gate_check(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(MOCK_GATE_RESPONSE),
        )

    def mock_move(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True}),
        )

    def mock_accept(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True}),
        )

    p.route("**/api/tickets/*/gate-check", mock_gate_check)
    p.route("**/api/tickets/*/move", mock_move)
    p.route("**/api/tickets/*/accept", mock_accept)

    p.goto(dashboard_server)
    # Wait for at least one card to render
    p.wait_for_selector(".card[data-item-id]", timeout=10000)
    yield p
    ctx.close()


@pytest.fixture()
def ticket_id(page):
    """Return the first ticket ID found in the dashboard DOM."""
    return page.evaluate(
        "document.querySelector('.card[data-item-id]').dataset.itemId"
    )


@pytest.fixture()
def two_ticket_ids(page):
    """Return the first two ticket IDs found in the dashboard DOM."""
    ids = page.evaluate("""
        Array.from(document.querySelectorAll('.card[data-item-id]'))
            .slice(0, 2)
            .map(c => c.dataset.itemId)
    """)
    if len(ids) < 2:
        pytest.skip("Need at least 2 tickets in dashboard")
    return ids


def trigger_gate_check(page, ticket_id, target_section):
    """Programmatically trigger a gate check via the JS API."""
    page.evaluate(
        f"window.startGateCheck('{ticket_id}', '{target_section}')"
    )


def wait_for_hash(page, expected_hash, timeout=5000):
    """Wait until location.hash matches expected value."""
    page.wait_for_function(
        f"window.location.hash === '{expected_hash}'",
        timeout=timeout,
    )


def wait_for_empty_hash(page, timeout=5000):
    """Wait until location.hash is empty."""
    page.wait_for_function(
        "window.location.hash === '' || window.location.hash === '#'",
        timeout=timeout,
    )


def wait_for_overlay_visible(page, timeout=5000):
    """Wait for the detail overlay to become visible."""
    page.wait_for_selector(
        "#ticket-detail-overlay:not(.hidden)", timeout=timeout
    )


def wait_for_overlay_hidden(page, timeout=5000):
    """Wait for the detail overlay to be hidden."""
    page.wait_for_function(
        "document.getElementById('ticket-detail-overlay')?.classList.contains('hidden') === true",
        timeout=timeout,
    )


def wait_for_gate_banner_visible(page, timeout=5000):
    """Wait for the gate banner to become visible."""
    page.wait_for_selector(
        "#detail-gate-banner:not(.hidden)", timeout=timeout
    )


# ---------------------------------------------------------------------------
# Live page fixture (no mocked routes — real API calls)
# ---------------------------------------------------------------------------


@pytest.fixture()
def live_page(browser, dashboard_server):
    """Per-test browser page with NO mocked routes — hits real server APIs."""
    ctx = browser.new_context()
    p = ctx.new_page()
    p.goto(dashboard_server)
    p.wait_for_selector(".card[data-item-id]", timeout=10000)
    yield p
    ctx.close()


# ---------------------------------------------------------------------------
# Shared API helpers
# ---------------------------------------------------------------------------


def api_get(base_url: str, path: str) -> dict:
    """GET an API path, return parsed JSON."""
    import urllib.request
    url = f"{base_url}{path}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def api_post(base_url: str, path: str, body: dict) -> tuple[int, dict]:
    """POST JSON to an API path, return (status_code, parsed_json)."""
    import urllib.request
    import urllib.error
    url = f"{base_url}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()) if e.read() else {}


def api_put(base_url: str, path: str, body: dict) -> tuple[int, dict]:
    """PUT JSON to an API path, return (status_code, parsed_json)."""
    import urllib.request
    import urllib.error
    url = f"{base_url}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()) if e.read() else {}


def api_delete(base_url: str, path: str) -> tuple[int, dict]:
    """DELETE an API path, return (status_code, parsed_json)."""
    import urllib.request
    import urllib.error
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()) if e.read() else {}
