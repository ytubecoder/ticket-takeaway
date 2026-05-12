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
