"""Pane-link domain logic — pure DB + classification, no I/O side effects.

Follows the same pattern as actions.py / journeys.py: every function takes
an open sqlite3.Connection and returns a value. Callers commit and emit
events. See docs/superpowers/specs/2026-05-12-pane-link-design.md.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Optional

from constants import (
    ATTENTION_NONE, ATTENTION_QUESTION, ATTENTION_EXCEPTION, ATTENTION_IDLE,
    PANE_TAIL_MAX_LINES, PANE_TAIL_MAX_BYTES, PANE_IDLE_THRESHOLD_S,
)

# Matches CSI escapes (ESC [ ... letter), OSC escapes (ESC ] ... BEL),
# and lone two-byte ESC sequences. Conservative — strips known terminal
# control sequences, leaves printable text and newlines untouched.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"   # CSI
    r"|\x1b\][^\x07]*\x07"          # OSC ... BEL
    r"|\x1b[@-Z\\-_]"               # Two-byte ESC
)


def strip_ansi(text: str) -> str:
    """Remove ANSI terminal control sequences from *text*."""
    if not text:
        return text
    return _ANSI_RE.sub("", text)
