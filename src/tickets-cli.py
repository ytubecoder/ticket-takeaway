#!/usr/bin/env python3
"""Ticket Takeaway CLI — SQLite-backed ticket management with markdown sync.

Usage:
    tickets-cli.py seed [--project ID]
    tickets-cli.py list [--project ID] [--section S] [--status S]
    tickets-cli.py add <project> "title" [--section S] [--priority P] [--complexity C] [--parent ID] [--description D]
    tickets-cli.py update <project> <id> [--title T] [--priority P] [--complexity C] [--status S] [--description D] [--parent P] [--summary SUM] [--add-criteria "text"] [--check-criteria N] [--uncheck-criteria N] [--remove-criteria N] [--add-depends ID] [--remove-depends ID]
    tickets-cli.py move <project> <id> <section>
    tickets-cli.py accept <project> <id>
    tickets-cli.py sync [--project ID]
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Ensure the src/ directory is importable (handles running from other dirs)
_src_dir = str(Path(__file__).resolve().parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from constants import (
    SECTION_ORDER, SECTION_SLUGS, SLUG_TO_SECTION,
    DEFAULT_STATUS_BY_SECTION, SECTION_PREFIX, STATUSES,
    VALID_STATUSES_BY_SECTION, compute_status_on_move,
    DASHBOARD_DIR, DB_PATH, REGISTRY_PATH,
)
from db import get_db, init_db
from actions import (
    move_ticket, accept_ticket, add_ticket, update_ticket,
    capture_commit_hash,
)


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
            default_status = DEFAULT_STATUS_BY_SECTION.get(current_section, "proposed")

            current_ticket = {
                "id": ticket_id,
                "title": title,
                "priority": "medium",
                "complexity": "M",
                "status": default_status,
                "section": current_section,
                "description": "",
                "parent": None,
                "depends": [],
                "acceptance_criteria": [],
                "sort_order": sort_order,
                "commit_hash": "",
                "release_tag": "",
                "readiness_content": {},  # {flag: content} for Tests/Reviewed/Smoke
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

        # Depends
        if current_ticket and line_stripped.startswith("Depends:"):
            val = line_stripped.split(":", 1)[1].strip()
            if val:
                current_ticket["depends"] = [d.strip() for d in val.split(",") if d.strip()]
            continue

        # Commit hash
        if current_ticket and line_stripped.startswith("Commit:"):
            val = line_stripped.split(":", 1)[1].strip()
            if val:
                current_ticket["commit_hash"] = val
            continue

        # Release tag
        if current_ticket and line_stripped.startswith("Release:"):
            val = line_stripped.split(":", 1)[1].strip()
            if val:
                current_ticket["release_tag"] = val
            continue

        # Acceptance criteria
        if current_ticket and re.match(r"^- \[[ xX]\]", line_stripped):
            checked = line_stripped[3] in ("x", "X")
            text_content = line_stripped[5:].strip()
            current_ticket["acceptance_criteria"].append((checked, text_content))
            continue

        # Readiness content: Tests, Reviewed, Smoke
        if current_ticket and line_stripped.startswith(("Tests:", "Reviewed:", "Smoke:")):
            flag_label, _, val = line_stripped.partition(":")
            flag_key = {"Tests": "tests", "Reviewed": "reviewed", "Smoke": "smoke"}[flag_label]
            current_ticket["readiness_content"][flag_key] = val.strip()
            continue

        # Indented continuation of readiness content (4-space indent)
        if current_ticket and line.startswith("    ") and current_ticket.get("readiness_content"):
            # Append to the most recently set readiness flag
            last_flag = list(current_ticket["readiness_content"].keys())[-1]
            current_ticket["readiness_content"][last_flag] += "\n" + line_stripped
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
    # Slug alias
    if name.lower() in SLUG_TO_SECTION:
        return SLUG_TO_SECTION[name.lower()]
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
                    section=?, description=?, parent=?,
                    sort_order=?, commit_hash=COALESCE(NULLIF(?, ''), commit_hash),
                    release_tag=COALESCE(NULLIF(?, ''), release_tag), updated_at=?
                WHERE id=? AND project_id=?
            """, (
                t["title"], t["priority"], t["complexity"], t["status"],
                t["section"], t["description"], t["parent"],
                t["sort_order"],
                t.get("commit_hash", ""), t.get("release_tag", ""),
                datetime.now().isoformat(),
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

            # Upsert readiness content from markdown
            for flag, content in t.get("readiness_content", {}).items():
                if content:
                    conn.execute("""
                        INSERT INTO readiness_flags (ticket_id, project_id, flag, content, set_by)
                        VALUES (?, ?, ?, ?, 'markdown')
                        ON CONFLICT (ticket_id, project_id, flag)
                        DO UPDATE SET content = excluded.content
                    """, (tid, project_id, flag, content))
                else:
                    conn.execute(
                        "DELETE FROM readiness_flags WHERE ticket_id=? AND project_id=? AND flag=?",
                        (tid, project_id, flag)
                    )
        else:
            # New ticket added directly to markdown — insert into DB
            conn.execute("""
                INSERT INTO tickets (id, project_id, title, priority, complexity, status,
                                     section, description, parent, sort_order,
                                     commit_hash, release_tag)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                tid, project_id, t["title"], t["priority"], t["complexity"],
                t["status"], t["section"], t["description"],
                t["parent"], t["sort_order"],
                t.get("commit_hash", ""), t.get("release_tag", ""),
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

            # Insert readiness content for new tickets
            for flag, content in t.get("readiness_content", {}).items():
                if content:
                    conn.execute("""
                        INSERT OR REPLACE INTO readiness_flags (ticket_id, project_id, flag, content, set_by)
                        VALUES (?, ?, ?, ?, 'markdown')
                    """, (tid, project_id, flag, content))

    # DB is the single source of truth. Tickets only in the DB (not in markdown)
    # are preserved — they may have been added via CLI or direct DB insert.
    # To delete a ticket, use the CLI explicitly.

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

            # Depends
            deps = conn.execute(
                "SELECT depends_on_id FROM depends WHERE ticket_id = ? AND project_id = ?",
                (t["id"], project_id)
            ).fetchall()
            if deps:
                dep_ids = ", ".join(d["depends_on_id"] for d in deps)
                lines.append(f"Depends: {dep_ids}")

            # Commit hash and release tag
            if t["commit_hash"]:
                lines.append(f"Commit: {t['commit_hash']}")
            if t["release_tag"]:
                lines.append(f"Release: {t['release_tag']}")

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

            # Readiness content (Tests, Reviewed, Smoke)
            flags = conn.execute(
                "SELECT flag, content FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND content != '' ORDER BY flag",
                (t["id"], project_id)
            ).fetchall()
            for f in flags:
                label = {"tests": "Tests", "reviewed": "Reviewed", "smoke": "Smoke"}.get(f["flag"])
                if label and f["content"]:
                    content_lines = f["content"].split("\n")
                    lines.append(f"{label}: {content_lines[0]}")
                    for continuation in content_lines[1:]:
                        lines.append(f"    {continuation}")

            lines.append("")

    # Append any custom sections that aren't managed by the DB
    if custom_sections:
        lines.append("")
        lines.extend(custom_sections)

    content = "\n".join(lines) + "\n"
    out_path.write_text(content, encoding="utf-8")

    # Store hash of the written markdown so we can detect external edits later
    md_hash = hashlib.sha256(content.encode()).hexdigest()
    conn.execute(
        "INSERT OR REPLACE INTO _sync_state (project_id, last_md_hash, last_sync_at) VALUES (?, ?, ?)",
        (project_id, md_hash, datetime.now().isoformat())
    )
    conn.commit()


def regenerate_dashboard(project: dict):
    """Regenerate the HTML dashboard after a data change."""
    gen_script = DASHBOARD_DIR / "generate.py"
    if not gen_script.exists():
        gen_script = Path.home() / ".claude" / "dashboard" / "generate.py"
    if not gen_script.exists():
        return
    project_path = os.path.expanduser(project.get("path", ""))
    if project_path:
        subprocess.run(
            [sys.executable, str(gen_script), "--no-open"],
            cwd=project_path,
            capture_output=True,
        )


def sync_all(conn: sqlite3.Connection, projects: list[dict]):
    """Ingest markdown edits, then sync DB to markdown for a list of projects."""
    for proj in projects:
        ingest_markdown(conn, proj)
        sync_to_markdown(conn, proj)
        regenerate_dashboard(proj)
        print(f"Synced {proj['name']}: {proj['path']}/PRODUCT_BACKLOG.md")


def detect_external_edits(conn: sqlite3.Connection, project: dict) -> bool:
    """Detect if PRODUCT_BACKLOG.md was edited outside the CLI.

    Compares the current file hash against the last known hash stored in
    ``_sync_state``.  When they differ the markdown is parsed, deltas are
    merged into the DB, and the file is regenerated from the DB (clean
    state) with an updated hash.

    Returns True if external edits were detected and absorbed, False otherwise.
    """
    project_id = project["id"]
    project_path = os.path.expanduser(project.get("path", ""))
    if not project_path:
        return False

    md_path = Path(project_path) / "PRODUCT_BACKLOG.md"
    if not md_path.exists():
        return False

    # Read current file and compute hash
    current_content = md_path.read_text(encoding="utf-8")
    current_hash = hashlib.sha256(current_content.encode()).hexdigest()

    # Look up stored hash
    row = conn.execute(
        "SELECT last_md_hash FROM _sync_state WHERE project_id = ?",
        (project_id,)
    ).fetchone()
    stored_hash = row["last_md_hash"] if row else ""

    if current_hash == stored_hash:
        return False  # No external edits

    # --- External edit detected — merge into DB ---

    md_tickets = parse_backlog(str(md_path))
    if not md_tickets:
        # File was blanked or unparseable — don't destroy DB state,
        # just update the hash so we don't re-check every cycle
        conn.execute(
            "INSERT OR REPLACE INTO _sync_state (project_id, last_md_hash, last_sync_at) VALUES (?, ?, ?)",
            (project_id, current_hash, datetime.now().isoformat())
        )
        conn.commit()
        return False

    md_ids = set()

    # Index DB tickets by upper-cased ID for comparison
    db_rows = conn.execute(
        "SELECT id FROM tickets WHERE project_id = ?", (project_id,)
    ).fetchall()
    db_ids = {r["id"].upper(): r["id"] for r in db_rows}

    for t in md_tickets:
        tid = t["id"]
        if not tid:
            continue
        md_ids.add(tid.upper())

        if tid.upper() in db_ids:
            # Existing ticket — update fields from markdown (markdown wins)
            real_id = db_ids[tid.upper()]
            conn.execute("""
                UPDATE tickets SET title=?, priority=?, complexity=?, status=?,
                    section=?, description=?, parent=?,
                    sort_order=?, commit_hash=COALESCE(NULLIF(?, ''), commit_hash),
                    release_tag=COALESCE(NULLIF(?, ''), release_tag), updated_at=?
                WHERE id=? AND project_id=?
            """, (
                t["title"], t["priority"], t["complexity"], t["status"],
                t["section"], t["description"], t["parent"],
                t["sort_order"],
                t.get("commit_hash", ""), t.get("release_tag", ""),
                datetime.now().isoformat(),
                real_id, project_id,
            ))

            # Replace acceptance criteria
            conn.execute(
                "DELETE FROM acceptance_criteria WHERE ticket_id=? AND project_id=?",
                (real_id, project_id)
            )
            for i, (checked, text) in enumerate(t["acceptance_criteria"]):
                conn.execute(
                    "INSERT INTO acceptance_criteria (ticket_id, project_id, text, checked, sort_order) VALUES (?,?,?,?,?)",
                    (real_id, project_id, text, int(checked), i)
                )

            # Replace depends
            conn.execute(
                "DELETE FROM depends WHERE ticket_id=? AND project_id=?",
                (real_id, project_id)
            )
            for dep_id in t["depends"]:
                conn.execute(
                    "INSERT OR IGNORE INTO depends (ticket_id, project_id, depends_on_id) VALUES (?,?,?)",
                    (real_id, project_id, dep_id)
                )

            # Upsert readiness content
            for flag, content in t.get("readiness_content", {}).items():
                if content:
                    conn.execute("""
                        INSERT INTO readiness_flags (ticket_id, project_id, flag, content, set_by)
                        VALUES (?, ?, ?, ?, 'external-edit')
                        ON CONFLICT (ticket_id, project_id, flag)
                        DO UPDATE SET content = excluded.content
                    """, (real_id, project_id, flag, content))
                else:
                    conn.execute(
                        "DELETE FROM readiness_flags WHERE ticket_id=? AND project_id=? AND flag=?",
                        (real_id, project_id, flag)
                    )
        else:
            # New ticket added directly to markdown — insert into DB
            conn.execute("""
                INSERT INTO tickets (id, project_id, title, priority, complexity, status,
                                     section, description, parent, sort_order,
                                     commit_hash, release_tag)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                tid, project_id, t["title"], t["priority"], t["complexity"],
                t["status"], t["section"], t["description"],
                t["parent"], t["sort_order"],
                t.get("commit_hash", ""), t.get("release_tag", ""),
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
            for flag, content in t.get("readiness_content", {}).items():
                if content:
                    conn.execute("""
                        INSERT OR REPLACE INTO readiness_flags (ticket_id, project_id, flag, content, set_by)
                        VALUES (?, ?, ?, ?, 'external-edit')
                    """, (tid, project_id, flag, content))

    # Do NOT delete tickets that are in DB but missing from markdown.
    # They may have been added via CLI or direct DB insert. Just flag them
    # with a log message for awareness.
    missing_from_md = set(db_ids.keys()) - md_ids
    if missing_from_md:
        real_missing = [db_ids[uid] for uid in missing_from_md]
        print(f"[{project_id}] Note: {len(real_missing)} ticket(s) in DB but not in markdown (preserved): {', '.join(real_missing)}")

    conn.commit()

    # Regenerate markdown from DB (clean state) and update the hash
    sync_to_markdown(conn, project)

    return True


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
                                     section, description, parent, sort_order,
                                     commit_hash, release_tag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                t["id"], project_id, t["title"], t["priority"], t["complexity"],
                t["status"], t["section"], t["description"],
                t["parent"], t["sort_order"],
                t.get("commit_hash", ""), t.get("release_tag", ""),
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

            # Readiness content
            for flag, content in t.get("readiness_content", {}).items():
                if content:
                    conn.execute("""
                        INSERT OR REPLACE INTO readiness_flags (ticket_id, project_id, flag, content, set_by)
                        VALUES (?, ?, ?, ?, 'seed')
                    """, (t["id"], project_id, flag, content))

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

    conn = get_db()
    init_db(conn)
    ingest_markdown(conn, proj)

    ticket_id = add_ticket(
        conn, project_id, args.title,
        section=section,
        priority=args.priority or "medium",
        complexity=args.complexity or "M",
        description=args.description or "",
        parent=args.parent,
        draft=args.draft,
    )
    conn.commit()

    sync_to_markdown(conn, proj)
    regenerate_dashboard(proj)
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

    # Build kwargs for actions.update_ticket — only pass fields that were
    # explicitly provided on the command line (None means "not provided" for
    # most fields; parent uses the ... sentinel in actions.py to distinguish
    # "not provided" from "clear parent").
    kwargs = {}
    if args.title is not None:
        kwargs["title"] = args.title
    if args.priority is not None:
        kwargs["priority"] = args.priority
    if args.complexity is not None:
        kwargs["complexity"] = args.complexity
    if args.status is not None:
        kwargs["status"] = args.status
    if args.description is not None:
        kwargs["description"] = args.description
    if args.parent is not None:
        # Empty string means "clear parent"; non-empty means "set parent"
        kwargs["parent"] = args.parent if args.parent else None
    if args.summary is not None:
        kwargs["summary"] = args.summary
    if args.add_criteria:
        kwargs["add_criteria"] = args.add_criteria
    if args.check_criteria is not None:
        kwargs["check_criteria"] = args.check_criteria
    if args.uncheck_criteria is not None:
        kwargs["uncheck_criteria"] = args.uncheck_criteria
    if args.remove_criteria is not None:
        kwargs["remove_criteria"] = args.remove_criteria
    if args.add_depends:
        kwargs["add_depends"] = args.add_depends
    if args.remove_depends:
        kwargs["remove_depends"] = args.remove_depends

    try:
        tid = update_ticket(conn, project_id, args.id, **kwargs)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        conn.close()
        sys.exit(1)
    except IndexError as e:
        print(str(e), file=sys.stderr)
        conn.close()
        sys.exit(1)

    if args.confirm:
        from actions import confirm_ticket
        confirm_ticket(conn, project_id, args.id)

    conn.commit()
    sync_to_markdown(conn, proj)
    regenerate_dashboard(proj)
    conn.close()
    print(f"Updated {tid}")


# ---------------------------------------------------------------------------
# Subcommand: move
# ---------------------------------------------------------------------------

def cmd_move(args):
    """Move a ticket to a different section."""
    projects = load_registry()
    proj = find_project(projects, args.project)
    project_id = proj["id"]
    project_path = os.path.expanduser(proj.get("path", ""))

    section = resolve_section(args.section)

    conn = get_db()
    init_db(conn)
    ingest_markdown(conn, proj)

    try:
        tid = move_ticket(conn, project_id, args.id, section, project_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.commit()
    sync_to_markdown(conn, proj)
    regenerate_dashboard(proj)
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
    project_name = proj.get("name", project_id)

    conn = get_db()
    init_db(conn)
    ingest_markdown(conn, proj)

    try:
        tid = accept_ticket(conn, project_id, args.id, project_path, project_name)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.commit()
    sync_to_markdown(conn, proj)
    regenerate_dashboard(proj)
    conn.close()
    print(f"{tid} accepted \u2192 Done")


# ---------------------------------------------------------------------------
# Subcommand: sync
# ---------------------------------------------------------------------------

VALID_FLAGS = {"tests", "reviewed", "smoke"}


def cmd_flag(args):
    """Set a readiness flag on a ticket."""
    projects = load_registry()
    target = resolve_project_id(projects, args.project)
    project_id = target[0]["id"]
    flag = args.flag.lower()

    if flag not in VALID_FLAGS:
        print(f"Invalid flag '{flag}'. Valid: {', '.join(sorted(VALID_FLAGS))}")
        sys.exit(1)

    conn = get_db()
    init_db(conn)

    row = conn.execute(
        "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
        (args.ticket_id, project_id)
    ).fetchone()
    if not row:
        print(f"Ticket {args.ticket_id} not found")
        conn.close()
        sys.exit(1)

    tid = row["id"]
    conn.execute(
        "INSERT OR REPLACE INTO readiness_flags (ticket_id, project_id, flag, set_by) VALUES (?, ?, ?, ?)",
        (tid, project_id, flag, args.by or "cli")
    )
    conn.commit()
    print(f"Set {flag} on {tid}")

    proj = find_project(target, project_id)
    sync_to_markdown(conn, proj)
    regenerate_dashboard(proj)
    conn.close()


def cmd_unflag(args):
    """Clear a readiness flag on a ticket."""
    projects = load_registry()
    target = resolve_project_id(projects, args.project)
    project_id = target[0]["id"]
    flag = args.flag.lower()

    conn = get_db()
    init_db(conn)

    row = conn.execute(
        "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
        (args.ticket_id, project_id)
    ).fetchone()
    if not row:
        print(f"Ticket {args.ticket_id} not found")
        conn.close()
        sys.exit(1)

    tid = row["id"]
    conn.execute(
        "DELETE FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
        (tid, project_id, flag)
    )
    conn.commit()
    print(f"Cleared {flag} on {tid}")

    proj = find_project(target, project_id)
    sync_to_markdown(conn, proj)
    regenerate_dashboard(proj)
    conn.close()


def cmd_sync(args):
    """Regenerate PRODUCT_BACKLOG.md from DB."""
    projects = load_registry()
    target = resolve_project_id(projects, args.project)

    conn = get_db()
    init_db(conn)
    sync_all(conn, target)
    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: watch
# ---------------------------------------------------------------------------

def cmd_register(args):
    """Register a new project in the registry."""
    import re as _re
    _SLUG_RE = _re.compile(r'^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$')

    pid = args.id
    if not _SLUG_RE.match(pid):
        print("Error: ID must be 2-40 chars, lowercase alphanumeric and hyphens", file=sys.stderr)
        sys.exit(1)

    path = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(path):
        print(f"Error: Path does not exist: {path}", file=sys.stderr)
        sys.exit(1)

    if REGISTRY_PATH.exists():
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            registry = json.load(f)
    else:
        registry = {"projects": []}

    for p in registry["projects"]:
        if p["id"] == pid:
            print(f"Error: Project '{pid}' already exists in registry", file=sys.stderr)
            sys.exit(1)

    new_project = {
        "id": pid,
        "name": args.name or pid,
        "path": path,
        "description": args.description or "",
        "active": True,
    }
    registry["projects"].append(new_project)

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)

    conn = get_db()
    init_db(conn)
    conn.close()

    print(f"Registered: {pid} → {path}")

    backlog = Path(path) / "PRODUCT_BACKLOG.md"
    if backlog.exists():
        print(f"Found {backlog}. Run 'tickets-cli.py seed --project {pid}' to import existing tickets.")


def cmd_unregister(args):
    """Deactivate a project in the registry."""
    if not REGISTRY_PATH.exists():
        print("Registry not found.", file=sys.stderr)
        sys.exit(1)

    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        registry = json.load(f)

    found = False
    for p in registry["projects"]:
        if p["id"] == args.id:
            p["active"] = False
            found = True
            break

    if not found:
        print(f"Project '{args.id}' not found in registry.", file=sys.stderr)
        sys.exit(1)

    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)

    print(f"Deactivated: {args.id}")

    if args.delete_tickets:
        conn = get_db()
        init_db(conn)
        conn.execute("DELETE FROM acceptance_criteria WHERE project_id = ?", (args.id,))
        conn.execute("DELETE FROM depends WHERE project_id = ?", (args.id,))
        conn.execute("DELETE FROM readiness_flags WHERE project_id = ?", (args.id,))
        conn.execute("DELETE FROM scheduled_events WHERE project_id = ?", (args.id,))
        conn.execute("DELETE FROM tickets WHERE project_id = ?", (args.id,))
        conn.execute("DELETE FROM _sync_state WHERE project_id = ?", (args.id,))
        conn.commit()
        conn.close()
        print(f"Deleted all tickets for {args.id} from database.")


def cmd_watch(args):
    """Watch PRODUCT_BACKLOG.md files for changes and auto-regenerate dashboard."""
    import time

    projects = load_registry()
    target = resolve_project_id(projects, args.project)
    interval = args.interval

    # Build initial mtime map
    mtimes = {}
    for proj in target:
        path = Path(os.path.expanduser(proj.get("path", ""))) / "PRODUCT_BACKLOG.md"
        mtimes[proj["id"]] = path.stat().st_mtime if path.exists() else 0

    print(f"Watching {len(target)} project(s) for changes (every {interval}s). Ctrl+C to stop.")

    try:
        while True:
            time.sleep(interval)
            for proj in target:
                path = Path(os.path.expanduser(proj.get("path", ""))) / "PRODUCT_BACKLOG.md"
                current_mtime = path.stat().st_mtime if path.exists() else 0

                if current_mtime != mtimes[proj["id"]]:
                    mtimes[proj["id"]] = current_mtime
                    print(f"[{proj['id']}] PRODUCT_BACKLOG.md changed — syncing...")

                    conn = get_db()
                    init_db(conn)
                    ingest_markdown(conn, proj)
                    sync_to_markdown(conn, proj)
                    regenerate_dashboard(proj)
                    conn.close()

                    print(f"[{proj['id']}] Dashboard updated.")
    except KeyboardInterrupt:
        print("\nStopped watching.")


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
    p_add.add_argument("--draft", action="store_true", help="Create as draft ticket")

    # update
    p_upd = sub.add_parser("update", help="Update a ticket")
    p_upd.add_argument("project", help="Project ID")
    p_upd.add_argument("id", help="Ticket ID")
    p_upd.add_argument("--title", help="New title")
    p_upd.add_argument("--priority", help="New priority")
    p_upd.add_argument("--complexity", help="New complexity")
    p_upd.add_argument("--status", help="New status")
    p_upd.add_argument("--description", help="New description")
    p_upd.add_argument("--parent", help="New parent ID (empty to clear)")
    p_upd.add_argument("--summary", help="New summary")
    p_upd.add_argument("--add-criteria", action="append", help="Add acceptance criterion (repeatable)")
    p_upd.add_argument("--check-criteria", type=int, help="Check Nth criterion (1-indexed)")
    p_upd.add_argument("--uncheck-criteria", type=int, help="Uncheck Nth criterion (1-indexed)")
    p_upd.add_argument("--remove-criteria", type=int, help="Remove Nth criterion (1-indexed)")
    p_upd.add_argument("--add-depends", action="append", help="Add dependency (repeatable)")
    p_upd.add_argument("--remove-depends", action="append", help="Remove dependency (repeatable)")
    p_upd.add_argument("--confirm", action="store_true", help="Confirm a draft ticket (set draft=false)")

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

    # watch
    p_watch = sub.add_parser("watch", help="Watch markdown for changes, auto-regenerate dashboard")
    p_watch.add_argument("--project", help="Project ID (default: auto-detect or all)")
    p_watch.add_argument("--interval", type=int, default=2, help="Poll interval in seconds (default: 2)")

    p_flag = sub.add_parser("flag", help="Set a readiness flag on a ticket")
    p_flag.add_argument("project", help="Project ID")
    p_flag.add_argument("ticket_id", help="Ticket ID")
    p_flag.add_argument("flag", help=f"Flag name: {', '.join(sorted(VALID_FLAGS))}")
    p_flag.add_argument("--by", help="Who set this flag (default: cli)", default="")

    p_unflag = sub.add_parser("unflag", help="Clear a readiness flag on a ticket")
    p_unflag.add_argument("project", help="Project ID")
    p_unflag.add_argument("ticket_id", help="Ticket ID")
    p_unflag.add_argument("flag", help="Flag name to clear")

    p_reg = sub.add_parser("register", help="Register a new project")
    p_reg.add_argument("--id", required=True, help="Project ID (lowercase, hyphens OK)")
    p_reg.add_argument("--name", help="Display name (default: same as ID)")
    p_reg.add_argument("--path", required=True, help="Path to project root")
    p_reg.add_argument("--description", help="Project description")

    p_unreg = sub.add_parser("unregister", help="Deactivate a project")
    p_unreg.add_argument("id", help="Project ID to deactivate")
    p_unreg.add_argument("--delete-tickets", action="store_true", help="Also delete tickets from DB (destructive)")

    args = parser.parse_args()

    commands = {
        "seed": cmd_seed,
        "list": cmd_list,
        "add": cmd_add,
        "update": cmd_update,
        "move": cmd_move,
        "accept": cmd_accept,
        "sync": cmd_sync,
        "watch": cmd_watch,
        "flag": cmd_flag,
        "unflag": cmd_unflag,
        "register": cmd_register,
        "unregister": cmd_unregister,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
