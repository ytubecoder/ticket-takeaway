#!/usr/bin/env python3
"""Compare two tickets.db files and report row-level differences.

Useful when claude-sync between WSL and llm-node is paused: lets you see
what diverged before deciding which side wins (or merging row-by-row).

Usage:
    python3 src/diff_dbs.py <db_a> <db_b>           # plain text diff
    python3 src/diff_dbs.py <db_a> <db_b> --json    # JSON for piping

The two DBs are inspected read-only — no writes, no merging. Compares
these tables (the ones that hold meaningful state):

  - tickets               by (project_id, id)
  - acceptance_criteria   by (project_id, ticket_id, text)
  - ticket_tags           by (project_id, ticket_id, tag)
  - ticket_branches       by (project_id, ticket_id, branch_name)
  - workflow_agents       by id
  - workflows             by id
  - automation_subjects   by (project_id, subject_type, subject_id)
  - settings              by key

For each table, prints rows that exist on only one side ("only in A" /
"only in B") and rows that exist on both but with different content
("changed"). Append-only tables (runs, activity_events) are summarised
as counts only — full row diff is too noisy and they're meant to union
on merge.

This is a diagnosis tool, not a merge tool. Once you see the diff:

  1. If only one side has new content (the common case after a paused
     sync), copy the winning DB over the other and resume sync.
  2. If both sides have unique content, decide row-by-row what to keep
     and write the merge by hand (or build a merger if it's a recurring
     need).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# ---- Comparable tables ----
#   key_cols: tuple — composite key uniquely identifying a row
#   compare_cols: tuple — columns whose values must match for "same content"
_TABLES = {
    "tickets": {
        "key_cols": ("project_id", "id"),
        "compare_cols": (
            "title",
            "priority",
            "status",
            "section",
            "description",
            "parent",
            "draft",
            "is_container",
            "summary_oneliner",
            "release_tag",
            "commit_hash",
        ),
    },
    "acceptance_criteria": {
        "key_cols": ("project_id", "ticket_id", "text"),
        "compare_cols": ("checked", "sort_order"),
    },
    "ticket_tags": {
        "key_cols": ("project_id", "ticket_id", "tag"),
        "compare_cols": (),  # presence only
    },
    "ticket_branches": {
        "key_cols": ("project_id", "ticket_id", "branch_name"),
        "compare_cols": ("pr_number", "pr_status", "pr_url"),
    },
    "workflow_agents": {
        "key_cols": ("id",),
        "compare_cols": (
            "name",
            "command",
            "args",
            "system_prompt",
            "persist_session",
            "system",
        ),
    },
    "workflows": {
        "key_cols": ("id",),
        "compare_cols": (
            "name",
            "description",
            "steps",
            "system",
            "enabled",
            "trigger_json",
            "on_success_json",
            "subject_type",
        ),
    },
    "automation_subjects": {
        "key_cols": ("project_id", "subject_type", "subject_id"),
        "compare_cols": ("automation_mode", "pause_reason"),
    },
    "settings": {
        "key_cols": ("key",),
        "compare_cols": ("value",),
    },
}

# Tables we count but don't row-diff (append-only audit logs).
_COUNT_ONLY = ("runs", "activity_events")


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open a sqlite3 connection in read-only mode."""
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return col in cols


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _filter_existing_cols(
    conn: sqlite3.Connection, table: str, cols: tuple[str, ...]
) -> tuple[str, ...]:
    """Drop columns that don't exist on this side (handles schema drift)."""
    return tuple(c for c in cols if _col_exists(conn, table, c))


def _load_rows(
    conn: sqlite3.Connection,
    table: str,
    key_cols: tuple[str, ...],
    compare_cols: tuple[str, ...],
) -> dict[tuple, dict]:
    """Load all rows from a table, keyed by composite key. Returns {} if missing."""
    if not _table_exists(conn, table):
        return {}
    safe_keys = _filter_existing_cols(conn, table, key_cols)
    safe_compare = _filter_existing_cols(conn, table, compare_cols)
    if not safe_keys:
        return {}
    cols_select = ", ".join(safe_keys + safe_compare)
    rows = conn.execute(f"SELECT {cols_select} FROM {table}").fetchall()
    out = {}
    for r in rows:
        key = tuple(r[c] for c in safe_keys)
        out[key] = {c: r[c] for c in safe_compare}
    return out


def _diff_table(
    conn_a: sqlite3.Connection, conn_b: sqlite3.Connection, table: str, spec: dict
) -> dict:
    """Return {only_in_a, only_in_b, changed} for one table."""
    a = _load_rows(conn_a, table, spec["key_cols"], spec["compare_cols"])
    b = _load_rows(conn_b, table, spec["key_cols"], spec["compare_cols"])

    only_in_a = sorted(set(a) - set(b))
    only_in_b = sorted(set(b) - set(a))
    changed: list[tuple[tuple, dict, dict]] = []
    for key in sorted(set(a) & set(b)):
        if a[key] != b[key]:
            changed.append((key, a[key], b[key]))

    return {
        "table": table,
        "total_a": len(a),
        "total_b": len(b),
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
        "changed": changed,
    }


def _count_only(
    conn_a: sqlite3.Connection, conn_b: sqlite3.Connection, table: str
) -> dict:
    """For append-only tables, just report counts."""
    ca = (
        conn_a.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if _table_exists(conn_a, table)
        else 0
    )
    cb = (
        conn_b.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if _table_exists(conn_b, table)
        else 0
    )
    return {"table": table, "count_a": ca, "count_b": cb, "delta": cb - ca}


def _print_text(
    diffs: list[dict], counts: list[dict], a_name: str, b_name: str
) -> None:
    total_changes = 0
    for d in diffs:
        a_only = len(d["only_in_a"])
        b_only = len(d["only_in_b"])
        chg = len(d["changed"])
        if a_only == 0 and b_only == 0 and chg == 0:
            print(f"  OK    {d['table']:25s} ({d['total_a']} rows, identical)")
            continue
        total_changes += a_only + b_only + chg
        print(
            f"  DIFF  {d['table']:25s} A={d['total_a']} B={d['total_b']}  "
            f"only_in_A={a_only} only_in_B={b_only} changed={chg}"
        )
        for key in d["only_in_a"][:5]:
            print(f"          - only in {a_name}: {key}")
        if a_only > 5:
            print(f"          ... + {a_only - 5} more only in {a_name}")
        for key in d["only_in_b"][:5]:
            print(f"          - only in {b_name}: {key}")
        if b_only > 5:
            print(f"          ... + {b_only - 5} more only in {b_name}")
        for key, va, vb in d["changed"][:5]:
            print(f"          ~ changed: {key}")
            for col in sorted(set(va) | set(vb)):
                if va.get(col) != vb.get(col):
                    print(
                        f"              {col}: {a_name}={va.get(col)!r}  {b_name}={vb.get(col)!r}"
                    )
        if chg > 5:
            print(f"          ... + {chg - 5} more changed")
    print()
    print("Append-only tables (counts only — union on merge):")
    for c in counts:
        marker = "  OK   " if c["delta"] == 0 else "  DIFF "
        delta_str = f"  delta={c['delta']:+d}" if c["delta"] != 0 else ""
        print(f"{marker}{c['table']:25s} A={c['count_a']} B={c['count_b']}{delta_str}")
    print()
    if total_changes == 0:
        print("DBs are identical across all row-compared tables.")
    else:
        print(f"Total row-level differences: {total_changes}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("db_a", help="Path to first tickets.db (e.g. WSL)")
    p.add_argument("db_b", help="Path to second tickets.db (e.g. llm-node)")
    p.add_argument(
        "--json", action="store_true", help="Emit JSON instead of human-readable text"
    )
    args = p.parse_args()

    pa, pb = Path(args.db_a), Path(args.db_b)
    if not pa.exists():
        print(f"DB A not found: {pa}", file=sys.stderr)
        sys.exit(2)
    if not pb.exists():
        print(f"DB B not found: {pb}", file=sys.stderr)
        sys.exit(2)

    a_name = pa.parent.name or pa.name
    b_name = pb.parent.name or pb.name

    conn_a = _open_ro(pa)
    conn_b = _open_ro(pb)
    try:
        diffs = [_diff_table(conn_a, conn_b, t, spec) for t, spec in _TABLES.items()]
        counts = [_count_only(conn_a, conn_b, t) for t in _COUNT_ONLY]
    finally:
        conn_a.close()
        conn_b.close()

    if args.json:
        # Make tuples JSON-friendly
        def _serialise(d):
            d = {**d}
            d["only_in_a"] = [list(k) for k in d["only_in_a"]]
            d["only_in_b"] = [list(k) for k in d["only_in_b"]]
            d["changed"] = [
                {"key": list(k), "a": va, "b": vb} for k, va, vb in d["changed"]
            ]
            return d

        out = {
            "db_a": str(pa),
            "db_b": str(pb),
            "tables": [_serialise(d) for d in diffs],
            "append_only": counts,
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"A = {pa}")
        print(f"B = {pb}\n")
        _print_text(diffs, counts, "A", "B")


if __name__ == "__main__":
    main()
