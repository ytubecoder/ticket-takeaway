"""E2E test: add a new project via the browser folder picker.

Tests the full onboarding flow:
  1. Navigate to project picker page
  2. Click "Add Project"
  3. Use folder picker to select a directory
  4. Verify name/ID auto-fill from directory name
  5. Submit the form
  6. Verify redirect to the new project's dashboard (no error)
  7. Cleanup: remove the project from registry
"""

import json
import os
import tempfile
import time

import pytest

from conftest import api_delete, api_get, api_post


@pytest.fixture()
def temp_project_dir():
    """Create a temporary project directory under ~/projects/ for the test."""
    base = os.path.expanduser("~/projects")
    # Use a unique name to avoid collisions
    dir_name = f"e2e-test-proj-{int(time.time())}"
    project_path = os.path.join(base, dir_name)
    os.makedirs(project_path, exist_ok=True)
    yield project_path, dir_name
    # Cleanup: remove the temp directory
    import shutil
    shutil.rmtree(project_path, ignore_errors=True)


def _root_url(dashboard_server: str) -> str:
    """Extract root URL from project-scoped dashboard_server URL."""
    # dashboard_server is like http://localhost:PORT/ticket-takeaway
    # We need http://localhost:PORT
    parts = dashboard_server.rsplit("/", 1)
    return parts[0] if len(parts) > 1 else dashboard_server


def test_add_project_via_api(dashboard_server, temp_project_dir):
    """Register a new project via API and verify dashboard loads."""
    project_path, dir_name = temp_project_dir
    root = _root_url(dashboard_server)
    slug = dir_name.lower().replace("_", "-")

    # 1. Register via API
    status_code, data = api_post(
        root, "/api/projects",
        {"id": slug, "name": dir_name, "path": project_path, "description": ""}
    )
    assert status_code == 201, f"Registration failed: {data}"
    assert data["id"] == slug
    # Should have scaffolded (no existing backlog)
    assert data.get("scaffolded") is True

    # 2. Verify PRODUCT_BACKLOG.md was created
    backlog = os.path.join(project_path, "PRODUCT_BACKLOG.md")
    assert os.path.exists(backlog), "PRODUCT_BACKLOG.md not created"

    # 3. Verify PRODUCT_SPECIFICATION.md was created
    spec = os.path.join(project_path, "PRODUCT_SPECIFICATION.md")
    assert os.path.exists(spec), "PRODUCT_SPECIFICATION.md not created"

    # 4. Verify dashboard HTML was generated
    dashboard = os.path.join(project_path, "docs", "sdlc-dashboard.html")
    assert os.path.exists(dashboard), "Dashboard HTML not generated"

    # 5. Verify the dashboard is accessible via HTTP (no error)
    import urllib.request
    import urllib.error
    try:
        url = f"{root}/{slug}/"
        with urllib.request.urlopen(url, timeout=10) as resp:
            html = resp.read().decode()
            assert "Dashboard not generated" not in html
            assert "<html" in html
    except urllib.error.HTTPError as e:
        pytest.fail(f"Dashboard returned HTTP {e.code}: {e.read().decode()}")

    # 6. Verify managed files API works
    import urllib.request
    url = f"{root}/{slug}/api/managed-files"
    with urllib.request.urlopen(url, timeout=10) as resp:
        files = json.loads(resp.read())
    paths = [f["path"] for f in files]
    assert "PRODUCT_BACKLOG.md" in paths
    assert "PRODUCT_SPECIFICATION.md" in paths
    # Both should exist since we just scaffolded
    backlog_entry = next(f for f in files if f["path"] == "PRODUCT_BACKLOG.md")
    assert backlog_entry["exists"] is True

    # 7. Cleanup: remove project from registry
    status_code, _ = api_delete(root, f"/api/projects/{slug}")
    assert status_code == 200


def test_add_project_with_existing_backlog(dashboard_server, temp_project_dir):
    """Register a project that already has a PRODUCT_BACKLOG.md — should auto-seed."""
    project_path, dir_name = temp_project_dir
    root = _root_url(dashboard_server)
    slug = dir_name.lower().replace("_", "-")

    # Write a backlog with one ticket
    backlog = os.path.join(project_path, "PRODUCT_BACKLOG.md")
    with open(backlog, "w") as f:
        f.write(
            "# Product Backlog\n\n"
            "## Backlog\n\n"
            "### B-01: Test ticket from existing backlog\n"
            "Priority: high | Complexity: M | Status: proposed\n"
            "- [ ] First criterion\n\n"
        )

    # Register — should auto-seed
    status_code, data = api_post(
        root, "/api/projects",
        {"id": slug, "name": dir_name, "path": project_path, "description": ""}
    )
    assert status_code == 201, f"Registration failed: {data}"
    assert data.get("seeded") == 1, f"Expected 1 seeded ticket, got: {data}"

    # Verify ticket is accessible via API
    import urllib.request
    url = f"{root}/{slug}/api/tickets"
    with urllib.request.urlopen(url, timeout=10) as resp:
        result = json.loads(resp.read())
    tickets = result.get("tickets", result) if isinstance(result, dict) else result
    assert len(tickets) == 1
    assert tickets[0]["title"] == "Test ticket from existing backlog"

    # Cleanup
    api_delete(root, f"/api/projects/{slug}")


def test_add_project_browser_flow(dashboard_server, browser, temp_project_dir):
    """Full browser flow: click Add, browse, select, submit, verify dashboard loads."""
    project_path, dir_name = temp_project_dir
    root = _root_url(dashboard_server)

    # Verify browse API works first
    import urllib.request
    browse_url = f"{root}/api/browse?path=~/projects"
    with urllib.request.urlopen(browse_url, timeout=10) as resp:
        browse_data = json.loads(resp.read())
    assert dir_name in browse_data["dirs"], f"{dir_name} not in browse results: {browse_data['dirs'][:10]}"

    page = browser.new_page()
    try:
        # 1. Go to project picker
        page.goto(root + "/")
        page.wait_for_selector("[data-testid='add-project-card']", timeout=5000)

        # 2. Click Add Project to show form
        page.click("[data-testid='add-project-card']")
        page.wait_for_selector("[data-testid='add-project-browse']", state="visible", timeout=3000)

        # 3. Click Browse to open picker
        page.click("[data-testid='add-project-browse']")
        # Wait for the picker overlay to become visible and items to load
        page.wait_for_selector("#folder-picker.visible", timeout=3000)
        page.wait_for_selector(".picker-item", timeout=10000)

        # 4. Our dir is in ~/projects — find and click it
        dir_item = page.wait_for_selector(f"[data-testid='picker-dir-{dir_name}']", timeout=5000)
        dir_item.click()

        # 5. Click "Select This Folder"
        page.click("#picker-select-current")

        # 6. Verify path display shows our directory
        path_text = page.text_content("[data-testid='add-project-path-display']")
        assert dir_name in path_text

        # 7. Verify name was auto-filled
        name_val = page.input_value("[data-testid='add-project-name']")
        assert name_val, "Name should be auto-filled"

        # 8. Verify ID was auto-filled
        id_val = page.input_value("[data-testid='add-project-id']")
        assert id_val, "ID should be auto-filled"

        # 9. Submit the form and wait for navigation
        with page.expect_navigation(timeout=15000):
            page.click("[data-testid='add-project-form'] .btn")

        # 10. Verify we landed on the new project's page
        assert id_val in page.url, f"Expected {id_val} in URL, got {page.url}"

        # 11. Verify no error message
        content = page.content()
        assert "Dashboard not generated" not in content

    finally:
        # Cleanup: remove from registry (ignore errors)
        slug = dir_name.lower().replace("_", "-")
        try:
            api_delete(root, f"/api/projects/{slug}")
        except Exception:
            pass
        page.close()
