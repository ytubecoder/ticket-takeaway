#!/usr/bin/env python3
"""
Export ticket-takeaway data to a portable JSON file.

Usage:
    python3 src/export_db.py [--db PATH] [--out PATH] [--project ID]

Output structure (format_version 1):
    {
      "format_version": 1,
      "source": "ticket-takeaway",
      "exported_at": "<UTC ISO 8601>",
      "exported_from_db": "<absolute db path>",
      "schema_migrations": [<int>, ...],
      "user_agents": [
        {"id","name","command","args","system_prompt","created_at","persist_session"}
      ],
      "projects": {
        "<project_id>": {
          "tickets": [
            {<all tickets columns>,
             "acceptance_criteria":[{"text","checked","sort_order"}],
             "depends_on":["<ticket_id>", ...],
             "tags":["<tag>", ...],
             "branches":[{branch_name, remote, pr_number, pr_status, pr_url,
                          ahead, behind, auto_linked, last_synced, created_at}],
             "readiness_flags":[{flag, set_at, set_by, content}],
             "attachments":[{attachment_type, name, path, summary, metadata,
                             triage_status, triage_result, created_at}]
            }
          ],
          "user_workflows": [<workflows row, system=0>],
          "journeys": [
            {<journeys row>,
             "steps":[<journey_steps row>],
             "linked_ticket_ids":["<ticket_id>", ...]}
          ],
          "settings": {"<key>": "<value>"}
        }
      }
    }

Tables intentionally excluded (operational state, code-as-data):
    runs, journey_runs, journey_step_results, workflow_runs,
    activity_events, scheduled_events, _sync_state, _migrations,
    sqlite_sequence, workflows where system=1.

Re-import path: see import_db.py (companion), or use tickets-cli.py
add/update for a CLI-driven re-import.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys
from collections import defaultdict

FORMAT_VERSION = 1
DEFAULT_DB = os.path.expanduser("~/.claude/ticket-takeaway/tickets.db")


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _bucket_by_ticket(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        out[(r["project_id"], r["ticket_id"])].append(r)
    return out


def export_db(
    db_path: str,
    out_path: str,
    project_filter: str | None = None,
) -> dict:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    schema_versions = sorted(
        r[0] for r in conn.execute("SELECT version FROM _migrations").fetchall()
    )

    # ---- ticket fanout tables, batched once then bucketed ----
    where = "WHERE project_id = ?" if project_filter else ""
    params = (project_filter,) if project_filter else ()

    criteria = _rows(
        conn,
        f"SELECT ticket_id, project_id, text, checked, sort_order "
        f"FROM acceptance_criteria {where} ORDER BY project_id, ticket_id, sort_order, id",
        params,
    )
    depends = _rows(
        conn,
        f"SELECT ticket_id, project_id, depends_on_id "
        f"FROM depends {where} ORDER BY project_id, ticket_id",
        params,
    )
    flags = _rows(
        conn,
        f"SELECT ticket_id, project_id, flag, set_at, set_by, content "
        f"FROM readiness_flags {where} ORDER BY project_id, ticket_id, flag",
        params,
    )

    tags: list[dict] = []
    if _table_exists(conn, "ticket_tags"):
        tags = _rows(
            conn,
            f"SELECT ticket_id, project_id, tag "
            f"FROM ticket_tags {where} ORDER BY project_id, ticket_id, tag",
            params,
        )

    branches: list[dict] = []
    if _table_exists(conn, "ticket_branches"):
        branches = _rows(
            conn,
            f"SELECT ticket_id, project_id, branch_name, remote, pr_number, "
            f"pr_status, pr_url, ahead, behind, auto_linked, last_synced, created_at "
            f"FROM ticket_branches {where} ORDER BY project_id, ticket_id, branch_name",
            params,
        )

    attachments: list[dict] = []
    if _table_exists(conn, "ticket_attachments"):
        attachments = _rows(
            conn,
            f"SELECT ticket_id, project_id, attachment_type, name, path, "
            f"summary, metadata, triage_status, triage_result, created_at "
            f"FROM ticket_attachments {where} ORDER BY project_id, ticket_id, id",
            params,
        )

    crit_by = _bucket_by_ticket(criteria)
    dep_by = _bucket_by_ticket(depends)
    flag_by = _bucket_by_ticket(flags)
    tag_by = _bucket_by_ticket(tags)
    branch_by = _bucket_by_ticket(branches)
    att_by = _bucket_by_ticket(attachments)

    # ---- tickets ----
    ticket_cols = [r[1] for r in conn.execute("PRAGMA table_info(tickets)").fetchall()]
    tw = "WHERE project_id = ?" if project_filter else ""
    ticket_rows = _rows(
        conn, f"SELECT * FROM tickets {tw} ORDER BY project_id, id", params
    )

    projects: dict[str, dict] = defaultdict(
        lambda: {"tickets": [], "user_workflows": [], "journeys": [], "settings": {}}
    )

    for t in ticket_rows:
        pid = t["project_id"]
        tid = t["id"]
        # Strip values that are None to keep file compact, but preserve schema-known columns
        # (an importer can default missing keys).
        ticket = {k: v for k, v in t.items() if v is not None}
        ticket["acceptance_criteria"] = [
            {
                "text": c["text"],
                "checked": bool(c["checked"]),
                "sort_order": c["sort_order"],
            }
            for c in crit_by.get((pid, tid), [])
        ]
        ticket["depends_on"] = [d["depends_on_id"] for d in dep_by.get((pid, tid), [])]
        ticket["tags"] = [tg["tag"] for tg in tag_by.get((pid, tid), [])]
        ticket["branches"] = [
            {
                k: v
                for k, v in b.items()
                if k not in ("ticket_id", "project_id") and v is not None
            }
            for b in branch_by.get((pid, tid), [])
        ]
        ticket["readiness_flags"] = [
            {
                k: v
                for k, v in f.items()
                if k not in ("ticket_id", "project_id") and v is not None
            }
            for f in flag_by.get((pid, tid), [])
        ]
        ticket["attachments"] = [
            {
                k: v
                for k, v in a.items()
                if k not in ("ticket_id", "project_id") and v is not None
            }
            for a in att_by.get((pid, tid), [])
        ]
        projects[pid]["tickets"].append(ticket)

    # ---- user-created workflows (system=0) per project ----
    if _table_exists(conn, "workflows"):
        ww = "WHERE COALESCE(system,0) = 0"
        if project_filter:
            ww += " AND project_id = ?"
        wf_rows = _rows(
            conn,
            f"SELECT * FROM workflows {ww} ORDER BY project_id, id",
            (project_filter,) if project_filter else (),
        )
        for w in wf_rows:
            pid = w.get("project_id") or "_global"
            projects[pid]["user_workflows"].append(
                {k: v for k, v in w.items() if v is not None}
            )

    # ---- journeys per project ----
    if _table_exists(conn, "journeys"):
        jw = "WHERE project_id = ?" if project_filter else ""
        j_rows = _rows(
            conn,
            f"SELECT * FROM journeys {jw} ORDER BY project_id, id",
            params,
        )
        if j_rows:
            jstep_rows = _rows(
                conn,
                f"SELECT * FROM journey_steps {jw} ORDER BY project_id, journey_id, sort_order, id",
                params,
            )
            jt_rows = _rows(
                conn,
                f"SELECT journey_id, project_id, ticket_id, step_id "
                f"FROM journey_tickets {jw} ORDER BY project_id, journey_id",
                params,
            )
            steps_by_journey: dict[str, list[dict]] = defaultdict(list)
            for s in jstep_rows:
                steps_by_journey[s["journey_id"]].append(
                    {k: v for k, v in s.items() if v is not None}
                )
            tids_by_journey: dict[str, list[str]] = defaultdict(list)
            for jt in jt_rows:
                tids_by_journey[jt["journey_id"]].append(jt["ticket_id"])

            for j in j_rows:
                jid = j["id"]
                pid = j["project_id"]
                entry = {k: v for k, v in j.items() if v is not None}
                entry["steps"] = steps_by_journey.get(jid, [])
                entry["linked_ticket_ids"] = tids_by_journey.get(jid, [])
                projects[pid]["journeys"].append(entry)

    # ---- per-project settings (settings is global; group by project_id-prefix) ----
    if _table_exists(conn, "settings"):
        for k, v in conn.execute("SELECT key, value FROM settings").fetchall():
            # Settings keys aren't project-scoped in this schema; emit them at the top
            # under "_global" rather than fabricating a project mapping.
            projects.setdefault(
                "_global",
                {"tickets": [], "user_workflows": [], "journeys": [], "settings": {}},
            )
            projects["_global"]["settings"][k] = v

    # ---- user agents (global) ----
    user_agents: list[dict] = []
    if _table_exists(conn, "workflow_agents"):
        agent_cols = [
            r[1] for r in conn.execute("PRAGMA table_info(workflow_agents)").fetchall()
        ]
        # If the schema has a `system` column (added in later migrations), filter to user-created.
        sql = "SELECT * FROM workflow_agents"
        if "system" in agent_cols:
            sql += " WHERE COALESCE(system,0) = 0"
        sql += " ORDER BY id"
        user_agents = _rows(conn, sql)

    bundle = {
        "format_version": FORMAT_VERSION,
        "source": "ticket-takeaway",
        "exported_at": _utc_now_iso(),
        "exported_from_db": os.path.abspath(db_path),
        "schema_migrations": schema_versions,
        "user_agents": user_agents,
        "projects": dict(projects),
    }

    if project_filter:
        bundle["project_filter"] = project_filter

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, ensure_ascii=False, sort_keys=False)

    return _summarize(bundle)


def _summarize(bundle: dict) -> dict:
    out = {"projects": {}, "user_agents": len(bundle.get("user_agents", []))}
    for pid, p in bundle["projects"].items():
        out["projects"][pid] = {
            "tickets": len(p.get("tickets", [])),
            "user_workflows": len(p.get("user_workflows", [])),
            "journeys": len(p.get("journeys", [])),
            "settings": len(p.get("settings", {})),
        }
    return out


def _default_out_path() -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"tickets-export-{ts}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export ticket-takeaway data to a portable JSON file."
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})"
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output JSON path (default: ./tickets-export-{ts}.json)",
    )
    parser.add_argument(
        "--project", default=None, help="Limit export to a single project_id"
    )
    args = parser.parse_args(argv)

    out_path = args.out or _default_out_path()
    out_path = os.path.abspath(out_path)

    summary = export_db(args.db, out_path, project_filter=args.project)
    size_kb = os.path.getsize(out_path) / 1024.0

    print(f"Wrote {out_path}  ({size_kb:.1f} KB)")
    print(f"User agents: {summary['user_agents']}")
    print("Projects:")
    for pid, counts in sorted(summary["projects"].items()):
        bits = [f"{counts['tickets']} tickets"]
        if counts["user_workflows"]:
            bits.append(f"{counts['user_workflows']} user workflows")
        if counts["journeys"]:
            bits.append(f"{counts['journeys']} journeys")
        if counts["settings"]:
            bits.append(f"{counts['settings']} settings")
        print(f"  {pid:<20} {', '.join(bits)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
