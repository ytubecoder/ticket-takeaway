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


import sqlite3

import pytest

import db as _db


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _db.init_db(c)
    # Seed a minimal project + ticket so foreign-keyish constraints (if any) pass
    c.execute(
        "INSERT INTO tickets (id, project_id, title, section, status) "
        "VALUES ('B-1', 'p', 'demo', 'backlog', 'proposed')"
    )
    c.commit()
    return c


def test_link_pane_inserts_row(conn):
    row_id = pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()
    assert row_id > 0
    row = conn.execute("SELECT * FROM pane_links WHERE id = ?", (row_id,)).fetchone()
    assert row["ticket_id"] == "B-1"
    assert row["pane_address"] == "%23"
    assert row["host"] == "llm-node"
    assert row["pane_descriptor"] == "vibe:0.1"
    assert row["status"] == "active"
    assert row["attention_state"] == "none"


def test_link_pane_replaces_existing(conn):
    pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()
    # Add a second ticket and re-link
    conn.execute(
        "INSERT INTO tickets (id, project_id, title, section, status) "
        "VALUES ('B-2', 'p', 'other', 'backlog', 'proposed')"
    )
    conn.commit()
    pane_links.link_pane(conn, "B-2", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()
    rows = conn.execute(
        "SELECT ticket_id FROM pane_links WHERE pane_address = '%23'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == "B-2"


def test_get_ticket_for_pane(conn):
    pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()
    row = pane_links.get_ticket_for_pane(conn, "%23")
    assert row["ticket_id"] == "B-1"
    assert row["project_id"] == "p"


def test_get_ticket_for_pane_missing(conn):
    assert pane_links.get_ticket_for_pane(conn, "%99") is None


def test_unlink_pane(conn):
    pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()
    deleted = pane_links.unlink_pane(conn, "%23")
    conn.commit()
    assert deleted == 1
    assert pane_links.get_ticket_for_pane(conn, "%23") is None


def test_list_pane_links_for_ticket(conn):
    pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    pane_links.link_pane(conn, "B-1", "p", "%24", "llm-node", "vibe:0.2")
    conn.commit()
    rows = pane_links.list_pane_links_for_ticket(conn, "p", "B-1")
    assert len(rows) == 2
    addrs = sorted(r["pane_address"] for r in rows)
    assert addrs == ["%23", "%24"]


def test_update_pane_capture_stores_tail_and_classifies(conn):
    pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()
    pane_links.update_pane_capture(
        conn, "%23", tail_text="hello\nworld", attention_state="none"
    )
    conn.commit()
    row = pane_links.get_ticket_for_pane(conn, "%23")
    assert row["tail_text"] == "hello\nworld"
    assert row["attention_state"] == "none"
    assert row["last_captured_at"] is not None


def test_update_pane_capture_records_attention_time_on_alert(conn):
    pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()
    pane_links.update_pane_capture(
        conn, "%23", tail_text="Error: bad", attention_state="exception"
    )
    conn.commit()
    row = pane_links.get_ticket_for_pane(conn, "%23")
    assert row["attention_state"] == "exception"
    assert row["attention_detected_at"] is not None


def test_mark_pane_stale(conn):
    pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()
    pane_links.mark_pane_stale(conn, "%23")
    conn.commit()
    row = pane_links.get_ticket_for_pane(conn, "%23")
    assert row["status"] == "stale"


def test_trim_tail_bounds():
    long = "\n".join(f"line {i}" for i in range(500))
    out = pane_links.trim_tail(long)
    # Should be bounded to PANE_TAIL_MAX_LINES
    assert len(out.splitlines()) <= 200


# Regression tests for fix #2: last_captured_at must not refresh when tail is unchanged.
# Without this fix the idle/question classifier sees ~2s elapsed every cycle and never
# reaches the 30s PANE_IDLE_THRESHOLD_S, so the 'quiet' branch never fires.


def test_last_captured_at_stable_when_tail_unchanged(conn):
    """Calling update_pane_capture twice with identical tail must not advance last_captured_at."""
    pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()

    tail = "all done\n$ "
    pane_links.update_pane_capture(conn, "%23", tail_text=tail, attention_state="none")
    conn.commit()
    first_ts = conn.execute(
        "SELECT last_captured_at FROM pane_links WHERE pane_address = '%23'"
    ).fetchone()["last_captured_at"]

    import time as _time

    _time.sleep(1)  # ensure wall-clock advances

    pane_links.update_pane_capture(conn, "%23", tail_text=tail, attention_state="none")
    conn.commit()
    second_ts = conn.execute(
        "SELECT last_captured_at FROM pane_links WHERE pane_address = '%23'"
    ).fetchone()["last_captured_at"]

    assert first_ts == second_ts, (
        "last_captured_at must not advance when tail is unchanged "
        f"(first={first_ts}, second={second_ts})"
    )


def test_last_captured_at_advances_when_tail_changes(conn):
    """Calling update_pane_capture with a different tail MUST advance last_captured_at."""
    pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()

    pane_links.update_pane_capture(
        conn, "%23", tail_text="step 1\n$ ", attention_state="none"
    )
    conn.commit()
    first_ts = conn.execute(
        "SELECT last_captured_at FROM pane_links WHERE pane_address = '%23'"
    ).fetchone()["last_captured_at"]

    import time as _time

    _time.sleep(1)

    pane_links.update_pane_capture(
        conn, "%23", tail_text="step 2\n$ ", attention_state="none"
    )
    conn.commit()
    second_ts = conn.execute(
        "SELECT last_captured_at FROM pane_links WHERE pane_address = '%23'"
    ).fetchone()["last_captured_at"]

    assert second_ts > first_ts, (
        "last_captured_at must advance when tail changes "
        f"(first={first_ts}, second={second_ts})"
    )


def test_idle_classifier_fires_after_stable_tail(conn):
    """Simulate the capture loop: write same tail twice, verify classify_attention sees idle."""
    import time as _time

    from constants import PANE_IDLE_THRESHOLD_S

    pane_links.link_pane(conn, "B-1", "p", "%23", "llm-node", "vibe:0.1")
    conn.commit()

    tail = "all done\n$ "

    # First capture — seeds last_captured_at
    pane_links.update_pane_capture(conn, "%23", tail_text=tail, attention_state="none")
    conn.commit()

    # Manually backdate last_captured_at to simulate PANE_IDLE_THRESHOLD_S elapsed
    stale_ts = int(_time.time()) - PANE_IDLE_THRESHOLD_S - 10
    conn.execute(
        "UPDATE pane_links SET last_captured_at = ? WHERE pane_address = '%23'",
        (stale_ts,),
    )
    conn.commit()

    row = conn.execute(
        "SELECT tail_text, last_captured_at FROM pane_links WHERE pane_address = '%23'"
    ).fetchone()

    # Second capture with same tail — last_captured_at stays at stale_ts
    pane_links.update_pane_capture(conn, "%23", tail_text=tail, attention_state="none")
    conn.commit()
    row2 = conn.execute(
        "SELECT last_captured_at FROM pane_links WHERE pane_address = '%23'"
    ).fetchone()
    assert row2["last_captured_at"] == stale_ts, (
        "last_captured_at should not have advanced"
    )

    # Now the classifier should see the elapsed time and return idle
    state = pane_links.classify_attention(tail, prev_tail=tail, prev_time=stale_ts)
    assert state == "idle", f"Expected 'idle', got {state!r}"
