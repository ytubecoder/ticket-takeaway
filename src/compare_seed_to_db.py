#!/usr/bin/env python3
"""Compare seeded automation definitions against the live DB.

Prints a side-by-side audit of system agents + system workflows defined in
``workflows_seed.py`` versus what's actually in the live ``tickets.db``.
Surfaces drift (system rows missing in DB, user rows hanging around in DB,
settings that aren't seed-defined) so you know what needs cleaning before
declaring a "ship" version.

Usage:
    python3 src/compare_seed_to_db.py               # default DB path
    python3 src/compare_seed_to_db.py --db /path    # custom path

Run after editing seeds to confirm the changes round-tripped to DB. Run
before shipping to spot dev cruft to delete.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Make sibling modules importable when run as a script
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from workflows_seed import (
    DEFAULT_AGENTS,
    DEFAULT_ENDPOINTS,
    DEFAULT_WORKFLOWS,
)

_DEFAULT_DB = Path.home() / ".claude" / "ticket-takeaway" / "tickets.db"


def _ok(s: str) -> str:
    return f"  OK   {s}"


def _drift(s: str) -> str:
    return f"  DRIFT {s}"


def _cruft(s: str) -> str:
    return f"  CRUFT {s}"


def _audit_agents(conn: sqlite3.Connection) -> int:
    """Return number of issues found (drift + cruft)."""
    print("== Agents ==")
    seed_by_id = {a["id"]: a for a in DEFAULT_AGENTS}
    db_rows = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT id, name, command, system FROM workflow_agents"
        ).fetchall()
    }

    issues = 0
    # Seed → DB: every seeded agent must exist with the correct system flag
    for aid, seed in seed_by_id.items():
        db = db_rows.get(aid)
        if db is None:
            print(_drift(f"{aid:30s} in seed, MISSING in DB"))
            issues += 1
            continue
        seed_sys = int(seed.get("system", 0))
        db_sys = int(db.get("system") or 0)
        if seed_sys != db_sys:
            print(_drift(f"{aid:30s} seed.system={seed_sys} DB.system={db_sys}"))
            issues += 1
        else:
            print(_ok(f"{aid:30s} system={db_sys}"))

    # DB → Seed: every system=1 row should be in the seed
    for aid, db in db_rows.items():
        if aid in seed_by_id:
            continue
        if int(db.get("system") or 0) == 1:
            print(_drift(f"{aid:30s} in DB as system=1, NOT in seed"))
            issues += 1
        else:
            print(_cruft(f"{aid:30s} user agent (delete pre-ship if dev cruft)"))
    return issues


def _audit_workflows(conn: sqlite3.Connection) -> int:
    print("\n== Workflows ==")
    seed_by_name = {w["name"]: w for w in DEFAULT_WORKFLOWS}
    db_rows = {
        r["name"]: dict(r)
        for r in conn.execute(
            "SELECT id, name, system, enabled FROM workflows"
        ).fetchall()
    }

    issues = 0
    for name, seed in seed_by_name.items():
        db = db_rows.get(name)
        if db is None:
            print(_drift(f"{name:40s} in seed, MISSING in DB"))
            issues += 1
            continue
        seed_enabled = int(seed.get("enabled", 0))
        db_enabled = int(db.get("enabled") or 0)
        if seed_enabled != db_enabled:
            # Note: enabled is per-project for system workflows now
            # (workflow_projects); the workflows.enabled field is the seed default.
            print(
                _drift(
                    f"{name:40s} seed.enabled={seed_enabled} DB.enabled={db_enabled}"
                )
            )
            issues += 1
        else:
            print(_ok(f"{name:40s} enabled={db_enabled}"))

    for name, db in db_rows.items():
        if name in seed_by_name:
            continue
        if int(db.get("system") or 0) == 1:
            print(_drift(f"{name:40s} in DB as system=1, NOT in seed"))
            issues += 1
        else:
            print(_cruft(f"{name:40s} user workflow (delete pre-ship if dev cruft)"))
    return issues


def _audit_endpoints(conn: sqlite3.Connection) -> int:
    """Return number of issues found (drift + cruft)."""
    print("\n== Endpoints ==")
    seed_by_id = {ep.id: ep for ep in DEFAULT_ENDPOINTS}
    db_rows = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT id, name, command, args, system FROM endpoints"
        ).fetchall()
    }

    issues = 0
    # Seed → DB: every seeded endpoint must exist with matching system flag + command + args
    for eid, ep in seed_by_id.items():
        db = db_rows.get(eid)
        if db is None:
            print(_drift(f"{eid:40s} in seed, MISSING in DB"))
            issues += 1
            continue
        db_sys = int(db.get("system") or 0)
        if db_sys != 1:
            print(_drift(f"{eid:40s} expected system=1, DB.system={db_sys}"))
            issues += 1
            continue
        db_cmd = db.get("command") or ""
        if db_cmd != (ep.command or ""):
            print(
                _drift(f"{eid:40s} command drift (seed={ep.command!r}, db={db_cmd!r})")
            )
            issues += 1
            continue
        db_args = json.loads(db.get("args") or "[]")
        if db_args != ep.args:
            print(_drift(f"{eid:40s} args drift (seed={ep.args!r}, db={db_args!r})"))
            issues += 1
            continue
        print(_ok(f"{eid:40s} system={db_sys}"))

    # DB → Seed: every system=1 endpoint row should be in the seed
    for eid, db in db_rows.items():
        if eid in seed_by_id:
            continue
        if int(db.get("system") or 0) == 1:
            print(_drift(f"{eid:40s} in DB as system=1, NOT in seed"))
            issues += 1
        else:
            print(_cruft(f"{eid:40s} user endpoint (delete pre-ship if dev cruft)"))
    return issues


def _audit_settings(conn: sqlite3.Connection) -> None:
    print("\n== Settings (informational — not seeded yet) ==")
    rows = list(conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall())
    if not rows:
        print("  (no settings rows)")
        return
    for r in rows:
        val = (r["value"] or "")[:60]
        print(f"  {r['key']:50s} = {val}")
    print(
        "\n  Seed equivalent: ``seed_default_agents`` writes "
        "``agent.default = agent_orchestrator``; everything else above is "
        "either runtime state (kitchen.paused) or per-project user choice. "
        "Decide pre-ship which keys to seed."
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(_DEFAULT_DB), help="Path to tickets.db")
    args = p.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(2)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        a = _audit_agents(conn)
        w = _audit_workflows(conn)
        e = _audit_endpoints(conn)
        _audit_settings(conn)
    finally:
        conn.close()

    total = a + w + e
    print(f"\nIssues found: {total} (agents={a}, workflows={w}, endpoints={e})")
    sys.exit(0 if total == 0 else 1)


if __name__ == "__main__":
    main()
