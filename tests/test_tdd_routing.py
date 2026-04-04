"""TDD tests for multi-project support."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_generate_html_single_project():
    """generate_html() should accept a single Project (not a list)."""
    from generate import generate_html, Project

    proj = Project(id="test-proj", name="Test Project", path="", description="", active=True)
    proj.tickets = []
    html = generate_html(proj)
    assert "Test Project" in html
    assert "<html" in html
