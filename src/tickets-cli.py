#!/usr/bin/env python3
"""Ticket Takeaway CLI — SQLite-backed ticket management with markdown sync.

Usage:
    tickets-cli.py seed [--project ID]
    tickets-cli.py list [--project ID] [--section S] [--status S]
    tickets-cli.py add <project> "title" [--section S] [--priority P] [--complexity C] [--parent ID] [--description D] [--rationale R]
    tickets-cli.py update <project> <id> [--title T] [--priority P] [--complexity C] [--status S] [--description D] [--rationale R] [--parent P] [--summary SUM] [--add-criteria "text"] [--check-criteria N] [--uncheck-criteria N] [--remove-criteria N] [--add-depends ID] [--remove-depends ID]
    tickets-cli.py move <project> <id> <section>
    tickets-cli.py accept <project> <id>
    tickets-cli.py sync [--project ID]
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants (shared with generate.py)
# ---------------------------------------------------------------------------

DASHBOARD_DIR = Path.home() / ".claude" / "ticket-takeaway"
DB_PATH = DASHBOARD_DIR / "tickets.db"
REGISTRY_PATH = DASHBOARD_DIR / "registry.json"

SECTION_ORDER = ["WIP", "For Review", "Backlog", "Ideas", "Bugs", "Icebox", "Done", "Won't Do"]

SECTION_TO_COLUMN = {
    "Ideas": "ideas",
    "Backlog": "backlog",
    "WIP": "wip",
    "For Review": "review",
    "Done": "done",
    "Won't Do": "wontdo",
    "Icebox": "icebox",
    "Bugs": "bugs",
}

DEFAULT_STATUS_BY_SECTION = {
    "Ideas": "proposed",
    "Backlog": "proposed",
    "WIP": "in-progress",
    "For Review": "for-review",
    "Done": "done",
    "Won't Do": "wontdo",
    "Icebox": "icebox",
    "Bugs": "bug",
}

# Lowercase aliases for section names (used in CLI args)
COLUMN_TO_SECTION = {v: k for k, v in SECTION_TO_COLUMN.items()}

# ID prefix by section for auto-generation
SECTION_PREFIX = {
    "Ideas": "I",
    "Backlog": "B",
    "WIP": "B",
    "For Review": "B",
    "Done": "R",
    "Won't Do": "W",
    "Icebox": "Z",
    "Bugs": "BUG",
}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db(db_path: str = None) -> sqlite3.Connection:
    """Open (or create) the SQLite database with WAL mode and FK support."""
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection):
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tickets (
            id          TEXT NOT NULL,
            project_id  TEXT NOT NULL,
            title       TEXT NOT NULL,
            priority    TEXT NOT NULL DEFAULT 'medium',
            complexity  TEXT NOT NULL DEFAULT 'M',
            status      TEXT NOT NULL DEFAULT 'proposed',
            section     TEXT NOT NULL DEFAULT 'Ideas',
            column      TEXT NOT NULL DEFAULT 'ideas',
            description TEXT NOT NULL DEFAULT '',
            parent      TEXT,
            rationale   TEXT NOT NULL DEFAULT '',
            summary     TEXT NOT NULL DEFAULT '',
            archived    INTEGER NOT NULL DEFAULT 0,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (id, project_id)
        );

        CREATE TABLE IF NOT EXISTS acceptance_criteria (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   TEXT NOT NULL,
            project_id  TEXT NOT NULL,
            text        TEXT NOT NULL,
            checked     INTEGER NOT NULL DEFAULT 0,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (ticket_id, project_id) REFERENCES tickets(id, project_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS depends (
            ticket_id       TEXT NOT NULL,
            project_id      TEXT NOT NULL,
            depends_on_id   TEXT NOT NULL,
            PRIMARY KEY (ticket_id, project_id, depends_on_id),
            FOREIGN KEY (ticket_id, project_id) REFERENCES tickets(id, project_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tickets_project_section ON tickets(project_id, section);
        CREATE INDEX IF NOT EXISTS idx_criteria_ticket ON acceptance_criteria(ticket_id, project_id);
        CREATE INDEX IF NOT EXISTS idx_depends_ticket ON depends(ticket_id, project_id);
    """)


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def load_registry() -> list[dict]:
    """Load project list from registry.json."""
    if not REGISTRY_PATH.exists():
        print(f"Registry not found at {REGISTRY_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [p for p in data.get("projects", []) if p.get("active", True)]


def find_project(projects: list[dict], project_id: str) -> dict:
    """Find a project by ID (case-insensitive)."""
    for p in projects:
        if p["id"].lower() == project_id.lower():
            return p
    print(f"Project '{project_id}' not found in registry.", file=sys.stderr)
    sys.exit(1)


def resolve_project_id(projects: list[dict], project_id: str = None) -> list[dict]:
    """Resolve to a list of projects — specific one or all if auto-detect fails."""
    if project_id:
        return [find_project(projects, project_id)]
    # Auto-detect from cwd
    cwd = os.path.realpath(os.getcwd())
    for p in projects:
        proj_path = os.path.realpath(os.path.expanduser(p.get("path", "")))
        if cwd == proj_path or cwd.startswith(proj_path + os.sep):
            return [p]
    return projects  # all


# ---------------------------------------------------------------------------
# Markdown parsing (adapted from generate.py)
# ---------------------------------------------------------------------------

def _parse_ticket_header(header: str) -> tuple[str, str]:
    """Parse 'ID: Title' into (id, title)."""
    match = re.match(r"^([A-Za-z][\w-]*(?:-\d+)?)\s*:\s*(.+)$", header)
    if match:
        return match.group(1), match.group(2).strip()
    return "", header.strip()


def _parse_metadata_line(line: str) -> dict:
    """Parse 'Priority: high | Complexity: M | Status: in-progress'"""
    result = {}
    for part in line.split("|"):
        part = part.strip()
        if ":" in part:
            key, value = part.split(":", 1)
            key = key.strip().lower()
            value = value.strip().lower()
            if key == "priority" and value in ("high", "medium", "low"):
                result["priority"] = value
            elif key == "complexity" and value.upper() in ("S", "M", "L", "XL"):
                result["complexity"] = value.upper()
            elif key == "status":
                result["status"] = value
    return result


def parse_backlog(filepath: str) -> list[dict]:
    """Parse PRODUCT_BACKLOG.md into a list of ticket dicts."""
    path = Path(filepath)
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    tickets = []
    current_section = None
    current_ticket = None
    sort_order = 0

    for line in text.splitlines():
        line_stripped = line.strip()

        # Section headers
        if line_stripped.startswith("## ") and not line_stripped.startswith("### "):
            section_name = line_stripped[3:].strip()
            if section_name in SECTION_ORDER:
                current_section = section_name
                sort_order = 0
            if current_ticket:
                tickets.append(current_ticket)
                current_ticket = None
            continue

        # Ticket headers
        if line_stripped.startswith("### ") and current_section:
            if current_ticket:
                tickets.append(current_ticket)

            header = line_stripped[4:].strip()
            ticket_id, title = _parse_ticket_header(header)
            column = SECTION_TO_COLUMN.get(current_section, "backlog")
            default_status = DEFAULT_STATUS_BY_SECTION.get(current_section, "proposed")

            current_ticket = {
                "id": ticket_id,
                "title": title,
                "priority": "medium",
                "complexity": "M",
                "status": default_status,
                "section": current_section,
                "column": column,
                "description": "",
                "parent": None,
                "rationale": "",
                "depends": [],
                "acceptance_criteria": [],
                "sort_order": sort_order,
            }
            sort_order += 1
            continue

        # Metadata line
        if current_ticket and line_stripped.startswith("Priority:"):
            meta = _parse_metadata_line(line_stripped)
            if "priority" in meta:
                current_ticket["priority"] = meta["priority"]
            if "complexity" in meta:
                current_ticket["complexity"] = meta["complexity"]
            if "status" in meta:
                current_ticket["status"] = meta["status"]
            continue

        # Parent
        if current_ticket and line_stripped.startswith("Parent:"):
            val = line_stripped.split(":", 1)[1].strip()
            if val:
                current_ticket["parent"] = val
            continue

        # Rationale
        if current_ticket and line_stripped.startswith("Rationale:"):
            val = line_stripped.split(":", 1)[1].strip()
            if val:
                current_ticket["rationale"] = val
            continue

        # Depends
        if current_ticket and line_stripped.startswith("Depends:"):
            val = line_stripped.split(":", 1)[1].strip()
            if val:
                current_ticket["depends"] = [d.strip() for d in val.split(",") if d.strip()]
            continue

        # Acceptance criteria
        if current_ticket and re.match(r"^- \[[ xX]\]", line_stripped):
            checked = line_stripped[3] in ("x", "X")
            text_content = line_stripped[5:].strip()
            current_ticket["acceptance_criteria"].append((checked, text_content))
            continue

        # Description
        if current_ticket and line_stripped and not line_stripped.startswith("#"):
            if current_ticket["description"]:
                current_ticket["description"] += " " + line_stripped
            else:
                current_ticket["description"] = line_stripped

    if current_ticket:
        tickets.append(current_ticket)

    return tickets


# ---------------------------------------------------------------------------
# Resolve section name from CLI arg (accepts both "wip" and "WIP")
# ---------------------------------------------------------------------------

def resolve_section(name: str) -> str:
    """Resolve a section name from a CLI arg. Accepts column aliases like 'wip'."""
    # Exact match
    for s in SECTION_ORDER:
        if s.lower() == name.lower():
            return s
    # Column alias
    if name.lower() in COLUMN_TO_SECTION:
        return COLUMN_TO_SECTION[name.lower()]
    # Common aliases
    aliases = {
        "for-review": "For Review",
        "review": "For Review",
        "wontdo": "Won't Do",
        "wont-do": "Won't Do",
        "won't-do": "Won't Do",
    }
    if name.lower() in aliases:
        return aliases[name.lower()]
    print(f"Unknown section: '{name}'. Valid: {', '.join(SECTION_ORDER)}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Auto-generate ticket ID
# ---------------------------------------------------------------------------

def auto_generate_id(conn: sqlite3.Connection, project_id: str, section: str) -> str:
    """Generate the next ticket ID for a section (e.g., B-14, BUG-10, I-03)."""
    prefix = SECTION_PREFIX.get(section, "B")
    sep = "-"
    pattern = f"{prefix}{sep}%"

    rows = conn.execute(
        "SELECT id FROM tickets WHERE project_id = ? AND id LIKE ?",
        (project_id, pattern)
    ).fetchall()

    max_num = 0
    for row in rows:
        tid = row["id"]
        suffix = tid[len(prefix) + len(sep):]
        try:
            num = int(suffix)
            if num > max_num:
                max_num = num
        except ValueError:
            pass

    return f"{prefix}{sep}{max_num + 1:02d}"


# ---------------------------------------------------------------------------
# Sync: DB → PRODUCT_BACKLOG.md
# ---------------------------------------------------------------------------

def _extract_preserved_content(filepath: Path) -> tuple[list[str], list[str]]:
    """Extract content from existing markdown that should be preserved across syncs.

    Returns (preamble_lines, custom_section_lines):
    - preamble: everything before the first known ## section (title, notes, etc.)
    - custom_sections: any ## sections not in SECTION_ORDER, kept as-is
    """
    if not filepath.exists():
        return [], []

    text = filepath.read_text(encoding="utf-8")
    known_sections = set(SECTION_ORDER)
    preamble = []
    custom_sections = []
    current_custom_block = []
    in_known_section = False
    hit_first_known = False

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("## ") and not stripped.startswith("### "):
            section_name = stripped[3:].strip()

            # Flush any custom block we were accumulating
            if current_custom_block:
                custom_sections.extend(current_custom_block)
                current_custom_block = []

            if section_name in known_sections:
                hit_first_known = True
                in_known_section = True
                continue
            else:
                # Unknown section — preserve it
                in_known_section = False
                current_custom_block.append(line)
                continue

        if not hit_first_known:
            preamble.append(line)
        elif not in_known_section:
            current_custom_block.append(line)

    # Flush final custom block
    if current_custom_block:
        custom_sections.extend(current_custom_block)

    return preamble, custom_sections


def _ingest_markdown_changes(conn: sqlite3.Connection, project_id: str, filepath: Path):
    """Read current PRODUCT_BACKLOG.md and merge any direct edits into the DB.

    If an agent (or human) edited the markdown directly, this picks up those
    changes before the DB overwrites the file. Markdown wins for conflicts.
    """
    if not filepath.exists():
        return

    md_tickets = parse_backlog(str(filepath))
    if not md_tickets:
        return

    # Index DB tickets by ID for comparison
    db_rows = conn.execute(
        "SELECT id FROM tickets WHERE project_id = ?", (project_id,)
    ).fetchall()
    db_ids = {r["id"] for r in db_rows}

    for t in md_tickets:
        tid = t["id"]
        if not tid:
            continue

        if tid in db_ids:
            # Existing ticket — update fields from markdown (markdown wins)
            conn.execute("""
                UPDATE tickets SET title=?, priority=?, complexity=?, status=?,
                    section=?, column=?, description=?, parent=?, rationale=?,
                    sort_order=?, updated_at=?
                WHERE id=? AND project_id=?
            """, (
                t["title"], t["priority"], t["complexity"], t["status"],
                t["section"], t["column"], t["description"], t["parent"],
                t["rationale"], t["sort_order"], datetime.now().isoformat(),
                tid, project_id,
            ))

            # Replace acceptance criteria
            conn.execute(
                "DELETE FROM acceptance_criteria WHERE ticket_id=? AND project_id=?",
                (tid, project_id)
            )
            for i, (checked, text) in enumerate(t["acceptance_criteria"]):
                conn.execute(
                    "INSERT INTO acceptance_criteria (ticket_id, project_id, text, checked, sort_order) VALUES (?,?,?,?,?)",
                    (tid, project_id, text, int(checked), i)
                )

            # Replace depends
            conn.execute(
                "DELETE FROM depends WHERE ticket_id=? AND project_id=?",
                (tid, project_id)
            )
            for dep_id in t["depends"]:
                conn.execute(
                    "INSERT OR IGNORE INTO depends (ticket_id, project_id, depends_on_id) VALUES (?,?,?)",
                    (tid, project_id, dep_id)
                )
        else:
            # New ticket added directly to markdown — insert into DB
            conn.execute("""
                INSERT INTO tickets (id, project_id, title, priority, complexity, status,
                                     section, column, description, parent, rationale, sort_order)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                tid, project_id, t["title"], t["priority"], t["complexity"],
                t["status"], t["section"], t["column"], t["description"],
                t["parent"], t["rationale"], t["sort_order"],
            ))
            for i, (checked, text) in enumerate(t["acceptance_criteria"]):
                conn.execute(
                    "INSERT INTO acceptance_criteria (ticket_id, project_id, text, checked, sort_order) VALUES (?,?,?,?,?)",
                    (tid, project_id, text, int(checked), i)
                )
            for dep_id in t["depends"]:
                conn.execute(
                    "INSERT OR IGNORE INTO depends (ticket_id, project_id, depends_on_id) VALUES (?,?,?)",
                    (tid, project_id, dep_id)
                )

    # Detect tickets removed from markdown (deleted by agent)
    md_ids = {t["id"] for t in md_tickets if t["id"]}
    removed = db_ids - md_ids
    for rid in removed:
        conn.execute("DELETE FROM tickets WHERE id=? AND project_id=?", (rid, project_id))

    conn.commit()


def ingest_markdown(conn: sqlite3.Connection, project: dict):
    """Absorb any direct markdown edits into the DB. Call before making DB writes."""
    project_id = project["id"]
    project_path = os.path.expanduser(project.get("path", ""))
    if not project_path:
        return
    out_path = Path(project_path) / "PRODUCT_BACKLOG.md"
    _ingest_markdown_changes(conn, project_id, out_path)


def sync_to_markdown(conn: sqlite3.Connection, project: dict):
    """Regenerate PRODUCT_BACKLOG.md from database, preserving non-ticket content."""
    project_id = project["id"]
    project_path = os.path.expanduser(project.get("path", ""))
    project_name = project.get("name", project_id)

    if not project_path:
        return

    out_path = Path(project_path) / "PRODUCT_BACKLOG.md"

    # Preserve preamble and custom sections from existing file
    preamble, custom_sections = _extract_preserved_content(out_path)

    lines = []

    # Preamble (or default title if no existing file)
    if preamble:
        lines.extend(preamble)
    else:
        lines.append(f"# Product Backlog \u2014 {project_name}")
        lines.append("")

    # Known sections with tickets from DB
    for section in SECTION_ORDER:
        lines.append(f"## {section}")
        lines.append("")

        tickets = conn.execute(
            "SELECT * FROM tickets WHERE project_id = ? AND section = ? ORDER BY sort_order ASC",
            (project_id, section)
        ).fetchall()

        for t in tickets:
            # Header
            lines.append(f"### {t['id']}: {t['title']}")

            # Metadata
            lines.append(f"Priority: {t['priority']} | Complexity: {t['complexity']} | Status: {t['status']}")

            # Parent
            if t["parent"]:
                lines.append(f"Parent: {t['parent']}")

            # Rationale
            if t["rationale"]:
                lines.append(f"Rationale: {t['rationale']}")

            # Depends
            deps = conn.execute(
                "SELECT depends_on_id FROM depends WHERE ticket_id = ? AND project_id = ?",
                (t["id"], project_id)
            ).fetchall()
            if deps:
                dep_ids = ", ".join(d["depends_on_id"] for d in deps)
                lines.append(f"Depends: {dep_ids}")

            # Description
            if t["description"]:
                lines.append(t["description"])

            # Acceptance criteria
            criteria = conn.execute(
                "SELECT * FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
                (t["id"], project_id)
            ).fetchall()
            for c in criteria:
                check = "x" if c["checked"] else " "
                lines.append(f"- [{check}] {c['text']}")

            lines.append("")

    # Append any custom sections that aren't managed by the DB
    if custom_sections:
        lines.append("")
        lines.extend(custom_sections)

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sync_all(conn: sqlite3.Connection, projects: list[dict]):
    """Ingest markdown edits, then sync DB to markdown for a list of projects."""
    for proj in projects:
        ingest_markdown(conn, proj)
        sync_to_markdown(conn, proj)
        print(f"Synced {proj['name']}: {proj['path']}/PRODUCT_BACKLOG.md")


# ---------------------------------------------------------------------------
# Subcommand: seed
# ---------------------------------------------------------------------------

def cmd_seed(args):
    """Parse PRODUCT_BACKLOG.md files and insert into DB."""
    projects = load_registry()
    target = resolve_project_id(projects, args.project)

    conn = get_db()
    init_db(conn)

    for proj in target:
        project_id = proj["id"]
        project_path = os.path.expanduser(proj.get("path", ""))
        backlog_path = os.path.join(project_path, "PRODUCT_BACKLOG.md")

        tickets = parse_backlog(backlog_path)
        if not tickets:
            print(f"No tickets found in {backlog_path}")
            continue

        # Clear existing data for this project (idempotent)
        conn.execute("DELETE FROM tickets WHERE project_id = ?", (project_id,))

        for t in tickets:
            conn.execute("""
                INSERT INTO tickets (id, project_id, title, priority, complexity, status,
                                     section, column, description, parent, rationale, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                t["id"], project_id, t["title"], t["priority"], t["complexity"],
                t["status"], t["section"], t["column"], t["description"],
                t["parent"], t["rationale"], t["sort_order"],
            ))

            # Acceptance criteria
            for i, (checked, text) in enumerate(t["acceptance_criteria"]):
                conn.execute("""
                    INSERT INTO acceptance_criteria (ticket_id, project_id, text, checked, sort_order)
                    VALUES (?, ?, ?, ?, ?)
                """, (t["id"], project_id, text, int(checked), i))

            # Dependencies
            for dep_id in t["depends"]:
                conn.execute("""
                    INSERT OR IGNORE INTO depends (ticket_id, project_id, depends_on_id)
                    VALUES (?, ?, ?)
                """, (t["id"], project_id, dep_id))

        conn.commit()
        print(f"Seeded {len(tickets)} tickets for {proj['name']}")

    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------

def cmd_list(args):
    """List tickets from DB."""
    projects = load_registry()
    target = resolve_project_id(projects, args.project)

    conn = get_db()
    init_db(conn)

    for proj in target:
        project_id = proj["id"]
        query = "SELECT * FROM tickets WHERE project_id = ?"
        params = [project_id]

        if args.section:
            section = resolve_section(args.section)
            query += " AND section = ?"
            params.append(section)

        if args.status:
            query += " AND status = ?"
            params.append(args.status.lower())

        query += " ORDER BY CASE section "
        for i, s in enumerate(SECTION_ORDER):
            escaped = s.replace("'", "''")
            query += f"WHEN '{escaped}' THEN {i} "
        query += "END, sort_order ASC"

        rows = conn.execute(query, params).fetchall()

        if not rows:
            print(f"No tickets found for {proj['name']}")
            continue

        print(f"\n{proj['name']} ({len(rows)} tickets)")
        print(f"{'Section':<14} {'ID':<10} {'Title':<40} {'Priority':<8} {'Status':<12} {'Cx'}")
        print("-" * 90)

        current_section = None
        for r in rows:
            if r["section"] != current_section:
                current_section = r["section"]
            title = r["title"][:38] + ".." if len(r["title"]) > 40 else r["title"]
            print(f"{r['section']:<14} {r['id']:<10} {title:<40} {r['priority']:<8} {r['status']:<12} {r['complexity']}")

        # Summary
        counts = {}
        for r in rows:
            counts[r["section"]] = counts.get(r["section"], 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in counts.items())
        print(f"\nSummary: {summary}")

    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: add
# ---------------------------------------------------------------------------

def cmd_add(args):
    """Add a new ticket."""
    projects = load_registry()
    proj = find_project(projects, args.project)
    project_id = proj["id"]

    section = resolve_section(args.section) if args.section else "Backlog"
    column = SECTION_TO_COLUMN[section]
    status = DEFAULT_STATUS_BY_SECTION[section]
    priority = args.priority or "medium"
    complexity = args.complexity or "M"

    conn = get_db()
    init_db(conn)
    ingest_markdown(conn, proj)

    ticket_id = auto_generate_id(conn, project_id, section)

    # sort_order = max in section + 1
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM tickets WHERE project_id = ? AND section = ?",
        (project_id, section)
    ).fetchone()
    sort_order = row["next_order"]

    conn.execute("""
        INSERT INTO tickets (id, project_id, title, priority, complexity, status,
                             section, column, description, parent, rationale, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticket_id, project_id, args.title, priority, complexity, status,
        section, column, args.description or "", args.parent, args.rationale or "", sort_order,
    ))
    conn.commit()

    # Sync markdown
    sync_to_markdown(conn, proj)
    conn.close()

    print(f"Added {ticket_id}: \"{args.title}\" to {section}")


# ---------------------------------------------------------------------------
# Subcommand: update
# ---------------------------------------------------------------------------

def cmd_update(args):
    """Partial update of a ticket."""
    projects = load_registry()
    proj = find_project(projects, args.project)
    project_id = proj["id"]

    conn = get_db()
    init_db(conn)
    ingest_markdown(conn, proj)

    # Verify ticket exists
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE id = ? AND project_id = ?",
        (args.id.upper(), project_id)
    ).fetchone()
    if not ticket:
        # Try case-insensitive
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (args.id, project_id)
        ).fetchone()
    if not ticket:
        print(f"Ticket '{args.id}' not found in {proj['name']}.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    tid = ticket["id"]

    # Build SET clause for ticket fields
    updates = {}
    if args.title is not None:
        updates["title"] = args.title
    if args.priority is not None:
        updates["priority"] = args.priority.lower()
    if args.complexity is not None:
        updates["complexity"] = args.complexity.upper()
    if args.status is not None:
        updates["status"] = args.status.lower()
    if args.description is not None:
        updates["description"] = args.description
    if args.rationale is not None:
        updates["rationale"] = args.rationale
    if args.parent is not None:
        updates["parent"] = args.parent if args.parent else None
    if args.summary is not None:
        updates["summary"] = args.summary

    if updates:
        updates["updated_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [tid, project_id]
        conn.execute(
            f"UPDATE tickets SET {set_clause} WHERE id = ? AND project_id = ?",
            values
        )

    # Acceptance criteria operations
    if args.add_criteria:
        for text in args.add_criteria:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
                (tid, project_id)
            ).fetchone()
            conn.execute(
                "INSERT INTO acceptance_criteria (ticket_id, project_id, text, checked, sort_order) VALUES (?, ?, ?, 0, ?)",
                (tid, project_id, text, row["next"])
            )

    if args.check_criteria is not None:
        _update_criterion(conn, tid, project_id, args.check_criteria, checked=1)

    if args.uncheck_criteria is not None:
        _update_criterion(conn, tid, project_id, args.uncheck_criteria, checked=0)

    if args.remove_criteria is not None:
        _remove_criterion(conn, tid, project_id, args.remove_criteria)

    # Depends operations
    if args.add_depends:
        for dep in args.add_depends:
            conn.execute(
                "INSERT OR IGNORE INTO depends (ticket_id, project_id, depends_on_id) VALUES (?, ?, ?)",
                (tid, project_id, dep)
            )

    if args.remove_depends:
        for dep in args.remove_depends:
            conn.execute(
                "DELETE FROM depends WHERE ticket_id = ? AND project_id = ? AND depends_on_id = ?",
                (tid, project_id, dep)
            )

    conn.commit()
    sync_to_markdown(conn, proj)
    conn.close()
    print(f"Updated {tid}")


def _update_criterion(conn, tid, project_id, index, checked):
    """Update the checked state of the Nth criterion (1-indexed)."""
    criteria = conn.execute(
        "SELECT id FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
        (tid, project_id)
    ).fetchall()
    if 1 <= index <= len(criteria):
        conn.execute("UPDATE acceptance_criteria SET checked = ? WHERE id = ?", (checked, criteria[index - 1]["id"]))
    else:
        print(f"Criterion index {index} out of range (1-{len(criteria)})", file=sys.stderr)


def _remove_criterion(conn, tid, project_id, index):
    """Remove the Nth criterion (1-indexed)."""
    criteria = conn.execute(
        "SELECT id FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
        (tid, project_id)
    ).fetchall()
    if 1 <= index <= len(criteria):
        conn.execute("DELETE FROM acceptance_criteria WHERE id = ?", (criteria[index - 1]["id"],))
    else:
        print(f"Criterion index {index} out of range (1-{len(criteria)})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommand: move
# ---------------------------------------------------------------------------

def cmd_move(args):
    """Move a ticket to a different section."""
    projects = load_registry()
    proj = find_project(projects, args.project)
    project_id = proj["id"]

    section = resolve_section(args.section)
    column = SECTION_TO_COLUMN[section]
    status = DEFAULT_STATUS_BY_SECTION[section]

    conn = get_db()
    init_db(conn)
    ingest_markdown(conn, proj)

    # Find ticket (case-insensitive)
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
        (args.id, project_id)
    ).fetchone()
    if not ticket:
        print(f"Ticket '{args.id}' not found in {proj['name']}.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    tid = ticket["id"]

    # sort_order = max in target section + 1
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM tickets WHERE project_id = ? AND section = ?",
        (project_id, section)
    ).fetchone()

    conn.execute("""
        UPDATE tickets SET section = ?, column = ?, status = ?, sort_order = ?, updated_at = ?
        WHERE id = ? AND project_id = ?
    """, (section, column, status, row["next_order"], datetime.now().isoformat(), tid, project_id))

    conn.commit()
    sync_to_markdown(conn, proj)
    conn.close()
    print(f"{tid} \u2192 {section}")


# ---------------------------------------------------------------------------
# Subcommand: accept
# ---------------------------------------------------------------------------

def cmd_accept(args):
    """Accept a ticket: move to Done and append to PRODUCT_SPECIFICATION.md."""
    projects = load_registry()
    proj = find_project(projects, args.project)
    project_id = proj["id"]
    project_path = os.path.expanduser(proj.get("path", ""))

    conn = get_db()
    init_db(conn)
    ingest_markdown(conn, proj)

    # Find ticket
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
        (args.id, project_id)
    ).fetchone()
    if not ticket:
        print(f"Ticket '{args.id}' not found in {proj['name']}.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    tid = ticket["id"]

    # Move to Done
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM tickets WHERE project_id = ? AND section = 'Done'",
        (project_id,)
    ).fetchone()

    conn.execute("""
        UPDATE tickets SET section = 'Done', column = 'done', status = 'done',
                           sort_order = ?, updated_at = ?
        WHERE id = ? AND project_id = ?
    """, (row["next_order"], datetime.now().isoformat(), tid, project_id))

    conn.commit()

    # Append to PRODUCT_SPECIFICATION.md
    spec_path = Path(project_path) / "PRODUCT_SPECIFICATION.md"
    today = datetime.now().strftime("%Y-%m-%d")
    entry = f"\n### {tid}: {ticket['title']}\n"
    entry += f"Priority: {ticket['priority']} | Complexity: {ticket['complexity']} | Status: released\n"
    entry += f"Released: {today}\n"
    if ticket["description"]:
        entry += f"{ticket['description']}\n"

    if spec_path.exists():
        content = spec_path.read_text(encoding="utf-8")
        # Insert before ## Archive if it exists, otherwise append
        if "## Archive" in content:
            content = content.replace("## Archive", entry + "\n## Archive")
        else:
            content = content.rstrip() + "\n" + entry
        spec_path.write_text(content, encoding="utf-8")
    else:
        spec_path.write_text(f"# Product Specification \u2014 {proj['name']}\n{entry}\n", encoding="utf-8")

    # Sync markdown
    sync_to_markdown(conn, proj)
    conn.close()
    print(f"{tid} accepted \u2192 Done")


# ---------------------------------------------------------------------------
# Subcommand: sync
# ---------------------------------------------------------------------------

def cmd_sync(args):
    """Regenerate PRODUCT_BACKLOG.md from DB."""
    projects = load_registry()
    target = resolve_project_id(projects, args.project)

    conn = get_db()
    init_db(conn)
    sync_all(conn, target)
    conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="tickets-cli",
        description="Ticket Takeaway CLI \u2014 SQLite-backed ticket management"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # seed
    p_seed = sub.add_parser("seed", help="Parse PRODUCT_BACKLOG.md into DB")
    p_seed.add_argument("--project", help="Project ID (default: auto-detect or all)")

    # list
    p_list = sub.add_parser("list", help="List tickets")
    p_list.add_argument("--project", help="Project ID")
    p_list.add_argument("--section", help="Filter by section")
    p_list.add_argument("--status", help="Filter by status")

    # add
    p_add = sub.add_parser("add", help="Add a new ticket")
    p_add.add_argument("project", help="Project ID")
    p_add.add_argument("title", help="Ticket title")
    p_add.add_argument("--section", help="Section (default: Backlog)")
    p_add.add_argument("--priority", help="Priority (high/medium/low)")
    p_add.add_argument("--complexity", help="Complexity (S/M/L/XL)")
    p_add.add_argument("--parent", help="Parent ticket ID")
    p_add.add_argument("--description", help="Description text")
    p_add.add_argument("--rationale", help="Rationale text")

    # update
    p_upd = sub.add_parser("update", help="Update a ticket")
    p_upd.add_argument("project", help="Project ID")
    p_upd.add_argument("id", help="Ticket ID")
    p_upd.add_argument("--title", help="New title")
    p_upd.add_argument("--priority", help="New priority")
    p_upd.add_argument("--complexity", help="New complexity")
    p_upd.add_argument("--status", help="New status")
    p_upd.add_argument("--description", help="New description")
    p_upd.add_argument("--rationale", help="New rationale")
    p_upd.add_argument("--parent", help="New parent ID (empty to clear)")
    p_upd.add_argument("--summary", help="New summary")
    p_upd.add_argument("--add-criteria", action="append", help="Add acceptance criterion (repeatable)")
    p_upd.add_argument("--check-criteria", type=int, help="Check Nth criterion (1-indexed)")
    p_upd.add_argument("--uncheck-criteria", type=int, help="Uncheck Nth criterion (1-indexed)")
    p_upd.add_argument("--remove-criteria", type=int, help="Remove Nth criterion (1-indexed)")
    p_upd.add_argument("--add-depends", action="append", help="Add dependency (repeatable)")
    p_upd.add_argument("--remove-depends", action="append", help="Remove dependency (repeatable)")

    # move
    p_move = sub.add_parser("move", help="Move ticket to section")
    p_move.add_argument("project", help="Project ID")
    p_move.add_argument("id", help="Ticket ID")
    p_move.add_argument("section", help="Target section")

    # accept
    p_acc = sub.add_parser("accept", help="Accept ticket (move to Done + spec)")
    p_acc.add_argument("project", help="Project ID")
    p_acc.add_argument("id", help="Ticket ID")

    # sync
    p_sync = sub.add_parser("sync", help="Regenerate PRODUCT_BACKLOG.md from DB")
    p_sync.add_argument("--project", help="Project ID (default: auto-detect or all)")

    args = parser.parse_args()

    commands = {
        "seed": cmd_seed,
        "list": cmd_list,
        "add": cmd_add,
        "update": cmd_update,
        "move": cmd_move,
        "accept": cmd_accept,
        "sync": cmd_sync,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
