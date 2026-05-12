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


def link_pane(
    conn: sqlite3.Connection,
    ticket_id: str,
    project_id: str,
    pane_address: str,
    host: str,
    pane_descriptor: str,
) -> int:
    """Create or replace a pane→ticket link. Returns row id.

    Caller commits and emits activity event in the same transaction.
    """
    now = int(time.time())
    conn.execute(
        "DELETE FROM pane_links WHERE pane_address = ?", (pane_address,)
    )
    cur = conn.execute(
        """
        INSERT INTO pane_links
            (ticket_id, project_id, pane_address, host, pane_descriptor,
             created_at, status, attention_state)
        VALUES (?, ?, ?, ?, ?, ?, 'active', 'none')
        """,
        (ticket_id, project_id, pane_address, host, pane_descriptor, now),
    )
    return cur.lastrowid


def unlink_pane(conn: sqlite3.Connection, pane_address: str) -> int:
    """Remove the pane→ticket link. Returns row count deleted."""
    cur = conn.execute(
        "DELETE FROM pane_links WHERE pane_address = ?", (pane_address,)
    )
    return cur.rowcount


def get_ticket_for_pane(
    conn: sqlite3.Connection, pane_address: str
) -> Optional[sqlite3.Row]:
    """Return the pane_links row for *pane_address*, or None."""
    return conn.execute(
        "SELECT * FROM pane_links WHERE pane_address = ?", (pane_address,)
    ).fetchone()


def list_pane_links_for_ticket(
    conn: sqlite3.Connection, project_id: str, ticket_id: str
) -> list[sqlite3.Row]:
    """Return all pane_links rows for the given ticket."""
    return conn.execute(
        "SELECT * FROM pane_links WHERE project_id = ? AND ticket_id = ? "
        "ORDER BY created_at ASC",
        (project_id, ticket_id),
    ).fetchall()


def list_pane_links_for_host(
    conn: sqlite3.Connection, host: str
) -> list[sqlite3.Row]:
    """Return active pane_links rows where host matches (for the capture worker)."""
    return conn.execute(
        "SELECT * FROM pane_links WHERE host = ? AND status = 'active'",
        (host,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Attention classifier
# ---------------------------------------------------------------------------

# Heuristic patterns. Order matters: exception > question > idle > none.

_EXCEPTION_PATTERNS = (
    re.compile(r"^Traceback \(most recent call", re.MULTILINE),
    re.compile(r"^[A-Za-z_]*Error: ", re.MULTILINE),
    re.compile(r"^Exception: ", re.MULTILINE),
    re.compile(r"^panic: ", re.MULTILINE),
    re.compile(r"failed with status \d", re.MULTILINE),
)

_QUESTION_TRAILING = re.compile(r"\?\s*$")
_QUESTION_PROMPTS = re.compile(
    r"\((y/n|Y/n|y/N)\)\s*$|"
    r"Please specify|Which option|"
    r"^\s*>\s*$",
    re.MULTILINE | re.IGNORECASE,
)

_SHELL_PROMPT_TAIL = re.compile(r"(?:^|\n)[^\n]*[\$%>#]\s*$")


def classify_attention(
    tail: str, prev_tail: str, prev_time: int
) -> str:
    """Return one of ATTENTION_* constants for *tail*.

    *prev_tail* and *prev_time* describe the previous capture: if the tail
    is unchanged and time has elapsed past PANE_IDLE_THRESHOLD_S, we treat
    the pane as quiet — needed to disambiguate "model just asked a question
    and is waiting" from "model is mid-stream and a `?` is in passing".
    """
    if not tail:
        return ATTENTION_NONE

    # Look at last 30 non-empty lines
    last_window = "\n".join(tail.strip().splitlines()[-30:])

    # Exception always wins
    for pat in _EXCEPTION_PATTERNS:
        if pat.search(last_window):
            return ATTENTION_EXCEPTION

    quiet = (
        tail == prev_tail
        and (int(time.time()) - prev_time) >= PANE_IDLE_THRESHOLD_S
    )

    # Question only when pane has settled
    if quiet:
        last_line = next(
            (ln for ln in reversed(tail.splitlines()) if ln.strip()), ""
        )
        if _QUESTION_TRAILING.search(last_line) or _QUESTION_PROMPTS.search(last_window):
            return ATTENTION_QUESTION
        if _SHELL_PROMPT_TAIL.search(tail):
            return ATTENTION_IDLE

    return ATTENTION_NONE


# ---------------------------------------------------------------------------
# Capture helpers
# ---------------------------------------------------------------------------

def trim_tail(text: str) -> str:
    """Bound tail text to PANE_TAIL_MAX_LINES and PANE_TAIL_MAX_BYTES.

    Keeps the LAST lines (the relevant tail), not the first.
    """
    if not text:
        return text
    lines = text.splitlines()
    if len(lines) > PANE_TAIL_MAX_LINES:
        lines = lines[-PANE_TAIL_MAX_LINES:]
    out = "\n".join(lines)
    if len(out.encode("utf-8", errors="replace")) > PANE_TAIL_MAX_BYTES:
        # Drop oldest lines until we fit
        while len(out.encode("utf-8", errors="replace")) > PANE_TAIL_MAX_BYTES and len(lines) > 1:
            lines = lines[1:]
            out = "\n".join(lines)
    return out


def update_pane_capture(
    conn: sqlite3.Connection,
    pane_address: str,
    tail_text: str,
    attention_state: str,
) -> None:
    """Write a fresh capture; record attention transition timestamp."""
    now = int(time.time())
    bounded = trim_tail(tail_text)
    # Only stamp attention_detected_at when entering a non-none state
    if attention_state == ATTENTION_NONE:
        conn.execute(
            "UPDATE pane_links SET tail_text = ?, attention_state = ?, "
            "attention_detected_at = NULL, last_captured_at = ?, status = 'active' "
            "WHERE pane_address = ?",
            (bounded, attention_state, now, pane_address),
        )
    else:
        conn.execute(
            "UPDATE pane_links SET tail_text = ?, attention_state = ?, "
            "attention_detected_at = COALESCE(attention_detected_at, ?), "
            "last_captured_at = ?, status = 'active' "
            "WHERE pane_address = ?",
            (bounded, attention_state, now, now, pane_address),
        )


def mark_pane_stale(conn: sqlite3.Connection, pane_address: str) -> None:
    """Flip status to 'stale' (capture failed / pane gone)."""
    conn.execute(
        "UPDATE pane_links SET status = 'stale' WHERE pane_address = ?",
        (pane_address,),
    )
