"""TDD tests for src/pane_links.py — no server, no tmux."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pane_links


def test_strip_ansi_removes_color_codes():
    s = "\x1b[31mRED\x1b[0m \x1b[1mBOLD\x1b[0m"
    assert pane_links.strip_ansi(s) == "RED BOLD"


def test_strip_ansi_removes_cursor_codes():
    # Cursor up + clear line
    s = "before\x1b[2A\x1b[2K\nafter"
    assert pane_links.strip_ansi(s) == "before\nafter"


def test_strip_ansi_passes_through_clean_text():
    s = "plain text with newlines\nand symbols !@#"
    assert pane_links.strip_ansi(s) == s


def test_strip_ansi_handles_empty():
    assert pane_links.strip_ansi("") == ""
