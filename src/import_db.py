#!/usr/bin/env python3
"""
Import a ticket-takeaway export JSON into a target SQLite DB.

Usage:
    python3 src/import_db.py <export.json> [--db PATH] [--project ID]
                                            [--mode skip|merge|replace]
                                            [--dry-run]

Modes:
    skip     (default) Refuse if any project to import already has tickets;
             abort cleanly without writing.
    merge    Insert tickets that don't exist in the target; leave existing
             tickets untouched (matched by (project_id, id)).
    replace  Delete all rows for each imported project's tickets/relations,
             then insert fresh. Destructive — confirms unless --yes given.

Design:
    - Direct INSERTs preserve original ticket IDs (B-01 etc.). actions.add_ticket
      can't do this — it auto-generates IDs.
    - Schema-aware: queries PRAGMA table_info on the target DB and only writes
      columns that exist there. Tolerates importing newer-schema exports into
      older DBs (extra fields silently dropped, with a warning).
    - Post-import: calls actions.sync_to_markdown() per touched project so the
      generated PRODUCT_BACKLOG.md stays consistent with the DB. Other hooks
      (parent auto-promote, etc.) re-trigger naturally on first edit.
    - Single transaction per project, rollback on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any

# Allow running from anywhere — locate src/ relative to this file.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEFAULT_DB = os.path.expanduser("~/.claude/ticket-takeaway/tickets.db")
SUPPORTED_FORMAT_VERSIONS = {1}

# Tables we touch, in dependency order for INSERT and reverse for DELETE.
TICKET_FANOUT_TABLES = [
    "acceptance_criteria",
    "depends",
    "readiness_flags",
    "ticket_tags",
    "ticket_branches",
    "ticket_attachments",
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _filter_to_schema(row: dict, allowed: set[str]) -> dict:
    return {k: v for k, v in row.items() if k in allowed}


def _insert_row(conn: sqlite3.Connection, table: str, row: dict, mode: str) -> int:
    """INSERT a dict as a row. mode controls conflict handling. Returns rowcount."""
    if not row:
        return 0
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    col_list = ",".join(cols)
    if mode == "merge":
        verb = "INSERT OR IGNORE"
    elif mode == "replace":
        verb = "INSERT OR REPLACE"
    else:
        verb = "INSERT"
    cur = conn.execute(
        f"{verb} INTO {table} ({col_list}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )
    return cur.rowcount or 0


def _delete_project_data(conn: sqlite3.Connection, project_id: str) -> None:
    """Delete all rows for a project across ticket-related tables."""
    for tbl in TICKET_FANOUT_TABLES:
        if _table_exists(conn, tbl):
            conn.execute(f"DELETE FROM {tbl} WHERE project_id = ?", (project_id,))
    if _table_exists(conn, "journey_tickets"):
        conn.execute("DELETE FROM journey_tickets WHERE project_id = ?", (project_id,))
    if _table_exists(conn, "journey_steps"):
        conn.execute("DELETE FROM journey_steps WHERE project_id = ?", (project_id,))
    if _table_exists(conn, "journeys"):
        conn.execute("DELETE FROM journeys WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM tickets WHERE project_id = ?", (project_id,))


def _project_has_tickets(conn: sqlite3.Connection, project_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tickets WHERE project_id = ? LIMIT 1", (project_id,)
    ).fetchone()
    return row is not None


def import_bundle(
    bundle: dict,
    db_path: str,
    *,
    project_filter: str | None = None,
    mode: str = "skip",
    dry_run: bool = False,
    confirmed: bool = False,
    sync_markdown: bool = False,
) -> dict:
    fmt = bundle.get("format_version")
    if fmt not in SUPPORTED_FORMAT_VERSIONS:
        raise ValueError(
            f"Unsupported format_version={fmt}. "
            f"This importer handles {sorted(SUPPORTED_FORMAT_VERSIONS)}."
        )

    # Open + initialise schema (canonical pattern from tickets-cli.py).
    import db as _db_module

    target = _db_module.get_db(db_path)
    _db_module.init_db(target)
    target.commit()
    # Use autocommit so we control transactions explicitly with BEGIN/COMMIT.
    target.isolation_level = None

    if not _table_columns(target, "tickets"):
        raise RuntimeError(
            f"Target DB has no `tickets` schema after init_db at {db_path}. "
            f"Aborting to avoid silent data drops."
        )

    summary: dict[str, Any] = {
        "mode": mode,
        "dry_run": dry_run,
        "projects": {},
        "user_agents_inserted": 0,
        "warnings": [],
    }

    projects = bundle.get("projects", {})
    if project_filter:
        if project_filter not in projects:
            raise ValueError(f"--project {project_filter} not in export")
        projects = {project_filter: projects[project_filter]}

    # Pre-flight: detect collisions for skip mode.
    if mode == "skip":
        collisions = [
            pid
            for pid in projects
            if pid != "_global"
            and _project_has_tickets(target, pid)
            and projects[pid].get("tickets")
        ]
        if collisions:
            raise RuntimeError(
                f"Target DB already has tickets for: {', '.join(collisions)}. "
                f"Use --mode merge to skip-existing, or --mode replace to overwrite."
            )

    if mode == "replace" and not dry_run and not confirmed:
        raise RuntimeError(
            "Refusing replace mode without --yes (would delete existing project data)."
        )

    # Schema cache
    ticket_cols = _table_columns(target, "tickets")
    fanout_cols = {
        t: _table_columns(target, t) if _table_exists(target, t) else set()
        for t in TICKET_FANOUT_TABLES
    }
    workflow_cols = (
        _table_columns(target, "workflows")
        if _table_exists(target, "workflows")
        else set()
    )
    workflow_agent_cols = (
        _table_columns(target, "workflow_agents")
        if _table_exists(target, "workflow_agents")
        else set()
    )
    journey_cols = (
        _table_columns(target, "journeys")
        if _table_exists(target, "journeys")
        else set()
    )
    journey_step_cols = (
        _table_columns(target, "journey_steps")
        if _table_exists(target, "journey_steps")
        else set()
    )

    # User agents (global). Wrap in a transaction so dry-run can probe
    # accurately (uses real INSERT OR IGNORE, then rolls back).
    if bundle.get("user_agents") and workflow_agent_cols:
        agent_mode = "replace" if mode == "replace" else "merge"
        target.execute("BEGIN")
        try:
            for agent in bundle["user_agents"]:
                row = _filter_to_schema(agent, workflow_agent_cols)
                summary["user_agents_inserted"] += _insert_row(
                    target, "workflow_agents", row, agent_mode
                )
        except Exception:
            target.execute("ROLLBACK")
            raise
        target.execute("ROLLBACK" if dry_run else "COMMIT")

    # Per-project import
    for pid, p in projects.items():
        if pid == "_global":
            continue

        ticket_count = len(p.get("tickets", []))
        if ticket_count == 0 and not p.get("user_workflows") and not p.get("journeys"):
            continue

        try:
            target.execute("BEGIN")

            if mode == "replace":
                _delete_project_data(target, pid)

            counts = {
                k: 0
                for k in (
                    "tickets",
                    "criteria",
                    "depends",
                    "tags",
                    "branches",
                    "readiness_flags",
                    "attachments",
                )
            }
            ticket_insert_mode = (
                "merge"
                if mode == "merge"
                else ("replace" if mode == "replace" else "skip")
            )
            warned_dropped: set[str] = set()  # warn once per dropped fieldset

            for t in p.get("tickets", []):
                ticket_row = {
                    k: v
                    for k, v in t.items()
                    if k
                    not in {
                        "acceptance_criteria",
                        "depends_on",
                        "tags",
                        "branches",
                        "readiness_flags",
                        "attachments",
                    }
                }
                ticket_row["project_id"] = pid
                row_for_insert = _filter_to_schema(ticket_row, ticket_cols)
                dropped = set(ticket_row) - set(row_for_insert)
                if dropped:
                    sig = ",".join(sorted(dropped))
                    if sig not in warned_dropped:
                        warned_dropped.add(sig)
                        summary["warnings"].append(
                            f"[{pid}] dropped fields not in target schema: {sorted(dropped)} "
                            f"(applies to all tickets in this project)"
                        )

                # Always perform the real INSERT (rolled back at end if dry_run).
                # That way dry-run counts reflect what the actual run would write.
                try:
                    wrote = _insert_row(
                        target,
                        "tickets",
                        row_for_insert,
                        ticket_insert_mode,
                    )
                    counts["tickets"] += wrote
                    ticket_was_inserted = wrote > 0
                except sqlite3.IntegrityError:
                    if mode == "merge":
                        continue
                    raise

                # In merge mode, if the ticket already existed in the target,
                # skip its fanout — the target's existing criteria/tags/deps/etc.
                # are authoritative, and re-inserting the backup's would create
                # duplicates (no UNIQUE constraint guards most fanout tables).
                if not ticket_was_inserted:
                    continue

                tid = t["id"]
                pairs = [
                    (
                        "acceptance_criteria",
                        "criteria",
                        [
                            {
                                **c,
                                "ticket_id": tid,
                                "project_id": pid,
                                "checked": int(bool(c.get("checked", 0))),
                            }
                            for c in (t.get("acceptance_criteria") or [])
                        ],
                    ),
                    (
                        "depends",
                        "depends",
                        [
                            {"ticket_id": tid, "project_id": pid, "depends_on_id": d}
                            for d in (t.get("depends_on") or [])
                        ],
                    ),
                    (
                        "ticket_tags",
                        "tags",
                        [
                            {"ticket_id": tid, "project_id": pid, "tag": tg}
                            for tg in (t.get("tags") or [])
                        ],
                    ),
                    (
                        "ticket_branches",
                        "branches",
                        [
                            {**b, "ticket_id": tid, "project_id": pid}
                            for b in (t.get("branches") or [])
                        ],
                    ),
                    (
                        "readiness_flags",
                        "readiness_flags",
                        [
                            {**f, "ticket_id": tid, "project_id": pid}
                            for f in (t.get("readiness_flags") or [])
                        ],
                    ),
                    (
                        "ticket_attachments",
                        "attachments",
                        [
                            {**a, "ticket_id": tid, "project_id": pid}
                            for a in (t.get("attachments") or [])
                        ],
                    ),
                ]
                for table, key, items in pairs:
                    if not fanout_cols[table] or not items:
                        continue
                    sub_mode = "merge" if mode == "merge" else "skip"
                    for item in items:
                        row = _filter_to_schema(item, fanout_cols[table])
                        counts[key] += _insert_row(target, table, row, sub_mode)

            inserted_tickets = counts["tickets"]
            inserted_criteria = counts["criteria"]
            inserted_deps = counts["depends"]
            inserted_tags = counts["tags"]
            inserted_branches = counts["branches"]
            inserted_flags = counts["readiness_flags"]
            inserted_attachments = counts["attachments"]

            child_mode = (
                "merge"
                if mode == "merge"
                else ("replace" if mode == "replace" else "skip")
            )

            inserted_workflows = 0
            for w in p.get("user_workflows", []) or []:
                if not workflow_cols:
                    break
                row = _filter_to_schema({**w, "project_id": pid}, workflow_cols)
                inserted_workflows += _insert_row(target, "workflows", row, child_mode)

            inserted_journeys = 0
            for j in p.get("journeys", []) or []:
                if not journey_cols:
                    break
                steps = j.get("steps", []) or []
                linked = j.get("linked_ticket_ids", []) or []
                jrow = _filter_to_schema(
                    {
                        k: v
                        for k, v in j.items()
                        if k not in {"steps", "linked_ticket_ids"}
                    },
                    journey_cols,
                )
                wrote_journey = _insert_row(target, "journeys", jrow, child_mode)
                inserted_journeys += wrote_journey
                if not wrote_journey:
                    # Journey already existed in target; skip its steps and links
                    # to avoid step duplication (same fanout hazard as tickets).
                    continue
                for s in steps:
                    srow = _filter_to_schema(
                        {**s, "project_id": pid}, journey_step_cols
                    )
                    _insert_row(target, "journey_steps", srow, child_mode)
                if _table_exists(target, "journey_tickets"):
                    for tid in linked:
                        _insert_row(
                            target,
                            "journey_tickets",
                            {
                                "journey_id": j["id"],
                                "project_id": pid,
                                "ticket_id": tid,
                            },
                            "merge",
                        )

            if dry_run:
                target.execute("ROLLBACK")
            else:
                target.execute("COMMIT")

            summary["projects"][pid] = {
                "tickets": inserted_tickets,
                "criteria": inserted_criteria,
                "depends": inserted_deps,
                "tags": inserted_tags,
                "branches": inserted_branches,
                "readiness_flags": inserted_flags,
                "attachments": inserted_attachments,
                "user_workflows": inserted_workflows,
                "journeys": inserted_journeys,
            }
        except Exception:
            target.execute("ROLLBACK")
            raise

    # Post-import: optionally regenerate markdown for touched projects.
    # Disabled by default — the registry/PRODUCT_BACKLOG.md paths may differ on
    # the importing machine, so we don't write outside the DB unless asked.
    if sync_markdown and not dry_run:
        try:
            import importlib.util

            cli_path = os.path.join(HERE, "tickets-cli.py")
            spec = importlib.util.spec_from_file_location("tickets_cli", cli_path)
            cli_mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(cli_mod)  # type: ignore[union-attr]
            registry = cli_mod.load_registry()
            synced = []
            for pid in summary["projects"]:
                proj = cli_mod.find_project(registry, pid)
                if not proj:
                    summary["warnings"].append(
                        f"markdown sync skipped for {pid}: not in registry"
                    )
                    continue
                try:
                    cli_mod.sync_to_markdown(target, proj)
                    synced.append(pid)
                except Exception as e:
                    summary["warnings"].append(f"markdown sync skipped for {pid}: {e}")
            summary["markdown_synced"] = synced
        except Exception as e:
            summary["warnings"].append(f"post-import markdown sync unavailable: {e}")

    target.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("export_json", help="Path to a tickets-export-*.json file")
    parser.add_argument(
        "--db", default=DEFAULT_DB, help=f"Target DB (default: {DEFAULT_DB})"
    )
    parser.add_argument(
        "--project", default=None, help="Limit import to a single project_id"
    )
    parser.add_argument(
        "--mode",
        choices=["skip", "merge", "replace"],
        default="skip",
        help="skip = abort if any target project has tickets (safe default); "
        "merge = INSERT OR IGNORE; replace = delete project rows then insert",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Confirm destructive --mode replace"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Compute counts without writing"
    )
    parser.add_argument(
        "--sync-markdown",
        action="store_true",
        help="After import, run sync_to_markdown for any imported project that's "
        "registered locally. Off by default to avoid surprise filesystem writes.",
    )
    args = parser.parse_args(argv)

    with open(args.export_json, encoding="utf-8") as f:
        bundle = json.load(f)

    summary = import_bundle(
        bundle,
        os.path.abspath(args.db),
        project_filter=args.project,
        mode=args.mode,
        dry_run=args.dry_run,
        confirmed=args.yes,
        sync_markdown=args.sync_markdown,
    )

    label = "WOULD INSERT (dry-run)" if args.dry_run else "Inserted"
    print(f"{label}  mode={summary['mode']}  db={os.path.abspath(args.db)}")
    print(f"User agents: {summary['user_agents_inserted']}")
    print("Per-project counts:")
    for pid, c in sorted(summary["projects"].items()):
        bits = [
            f"{c['tickets']} tickets",
            f"{c['criteria']} criteria",
            f"{c['depends']} deps",
        ]
        for k in (
            "tags",
            "branches",
            "readiness_flags",
            "attachments",
            "user_workflows",
            "journeys",
        ):
            if c.get(k):
                bits.append(f"{c[k]} {k}")
        print(f"  {pid:<20} {', '.join(bits)}")
    if summary.get("markdown_synced"):
        print(f"Markdown synced: {', '.join(summary['markdown_synced'])}")
    if summary["warnings"]:
        print("Warnings:")
        for w in summary["warnings"][:20]:
            print(f"  {w}")
        if len(summary["warnings"]) > 20:
            print(f"  ... and {len(summary['warnings']) - 20} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
