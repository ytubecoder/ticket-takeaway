"""TDD for attention classifier — pattern matching only, no I/O."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pane_links


def now():
    return int(time.time())


def test_question_trailing_qmark():
    tail = "model > Should we cache offline assets too?"
    assert (
        pane_links.classify_attention(tail, prev_tail=tail, prev_time=now() - 60)
        == "question"
    )


def test_question_y_n_prompt():
    tail = "Proceed with migration? (y/n)"
    assert (
        pane_links.classify_attention(tail, prev_tail=tail, prev_time=now() - 60)
        == "question"
    )


def test_question_only_fires_when_quiet():
    # Same tail but recent activity — classifier should not fire question yet
    tail = "anything ending in?"
    assert (
        pane_links.classify_attention(tail, prev_tail="different", prev_time=now())
        == "none"
    )


def test_exception_traceback():
    tail = "Traceback (most recent call last):\n  File 'x.py'\nTypeError: bad"
    assert (
        pane_links.classify_attention(tail, prev_tail="", prev_time=now())
        == "exception"
    )


def test_exception_error_prefix():
    tail = "doing stuff\nError: connection refused\n$ "
    assert (
        pane_links.classify_attention(tail, prev_tail="", prev_time=now())
        == "exception"
    )


def test_exception_panic_prefix():
    tail = "panic: runtime error: invalid memory address"
    assert (
        pane_links.classify_attention(tail, prev_tail="", prev_time=now())
        == "exception"
    )


def test_idle_quiet_prompt():
    tail = "all done\n$ "
    # No activity for >30s
    assert (
        pane_links.classify_attention(tail, prev_tail=tail, prev_time=now() - 60)
        == "idle"
    )


def test_idle_not_when_recent():
    tail = "all done\n$ "
    assert (
        pane_links.classify_attention(tail, prev_tail=tail, prev_time=now()) == "none"
    )


def test_none_when_busy():
    tail = "installing package X\nresolving dependencies\nbuilding"
    assert (
        pane_links.classify_attention(
            tail, prev_tail="installing package X", prev_time=now()
        )
        == "none"
    )


def test_exception_beats_idle():
    # Even with a stale prompt-looking tail, an exception should win
    tail = "Error: blew up\n$ "
    assert (
        pane_links.classify_attention(tail, prev_tail=tail, prev_time=now() - 60)
        == "exception"
    )
