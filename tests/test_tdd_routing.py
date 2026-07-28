"""TDD tests for multi-project support."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_generate_html_single_project():
    """generate_html() should accept a single Project (not a list)."""
    from generate import Project, generate_html

    proj = Project(id="test-proj", name="Test Project", path="", active=True)
    proj.tickets = []
    html = generate_html(proj)
    assert "Test Project" in html
    assert "<html" in html


def test_resolve_project_known_prefix():
    """Known project ID in URL prefix returns (project, remainder)."""
    from serve import _PROJECTS_CACHE, _PROJECTS_CACHE_LOCK, _resolve_project_from_path

    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
        _PROJECTS_CACHE["goodform"] = {
            "id": "goodform",
            "name": "GoodForm",
            "path": "/tmp",
        }
    proj, remainder = _resolve_project_from_path("/goodform/api/tickets")
    assert proj["id"] == "goodform"
    assert remainder == "/api/tickets"


def test_resolve_project_root_path():
    """Root path returns (None, '/')."""
    from serve import _resolve_project_from_path

    proj, remainder = _resolve_project_from_path("/")
    assert proj is None
    assert remainder == "/"


def test_resolve_project_unknown_prefix():
    """Unknown prefix returns (None, original_path)."""
    from serve import _PROJECTS_CACHE, _PROJECTS_CACHE_LOCK, _resolve_project_from_path

    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
    proj, remainder = _resolve_project_from_path("/unknown/api/tickets")
    assert proj is None
    assert remainder == "/unknown/api/tickets"


def test_resolve_project_global_routes_not_captured():
    """Global routes like /api/projects should not match a project named 'api'."""
    from serve import _PROJECTS_CACHE, _PROJECTS_CACHE_LOCK, _resolve_project_from_path

    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
        _PROJECTS_CACHE["api"] = {"id": "api", "name": "bad", "path": "/tmp"}
    proj, _remainder = _resolve_project_from_path("/api/projects")
    assert proj is None  # global routes take precedence


def test_resolve_project_bare_id():
    """/goodform (no trailing slash) should resolve correctly."""
    from serve import _PROJECTS_CACHE, _PROJECTS_CACHE_LOCK, _resolve_project_from_path

    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
        _PROJECTS_CACHE["goodform"] = {
            "id": "goodform",
            "name": "GoodForm",
            "path": "/tmp",
        }
    proj, remainder = _resolve_project_from_path("/goodform")
    assert proj["id"] == "goodform"
    assert remainder == "/"


import shutil
import tempfile


def test_validate_project_id_reserved():
    """Reserved IDs like 'api' and 'settings' should be rejected."""
    from serve import _RESERVED_IDS

    assert "api" in _RESERVED_IDS
    assert "settings" in _RESERVED_IDS


def test_validate_project_registration_bad_path():
    """Registration with non-existent path should fail."""
    from serve import _validate_project_registration

    error = _validate_project_registration(
        {"id": "test-proj", "path": "/nonexistent/path"}
    )
    assert error is not None
    assert "does not exist" in error


def test_validate_project_registration_good():
    """Registration with valid ID and path should succeed."""
    from serve import (
        _PROJECTS_CACHE,
        _PROJECTS_CACHE_LOCK,
        _validate_project_registration,
    )

    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
    tmp = tempfile.mkdtemp(dir=str(Path.home()))
    try:
        error = _validate_project_registration({"id": "test-proj", "path": tmp})
        assert error is None
    finally:
        shutil.rmtree(tmp)
