#!/usr/bin/env python3
"""Ticket Takeaway CLI — SQLite-backed ticket management with markdown sync.

Usage:
    tickets-cli.py seed [--project ID]
    tickets-cli.py list [--project ID] [--section S] [--status S]
    tickets-cli.py add <project> "title" [--section S] [--priority P] [--parent ID] [--description D] [--tag T]
    tickets-cli.py update <project> <id> [--title T] [--priority P] [--status S] [--description D] [--parent P] [--summary SUM] [--add-criteria "text"] [--check-criteria N] [--uncheck-criteria N] [--remove-criteria N] [--add-depends ID] [--remove-depends ID] [--add-tag T] [--remove-tag T]
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
    link_branch, unlink_branch, get_ticket_branches, get_project_branches,
    scan_branches, scan_prs,
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
    """Parse 'Priority: high | Status: in-progress'.

    Legacy 'Complexity:' segments are silently skipped — see migration #13.
    """
    result = {}
    for part in line.split("|"):
        part = part.strip()
        if ":" in part:
            key, value = part.split(":", 1)
            key = key.strip().lower()
            value = value.strip().lower()
            if key == "priority" and value in ("high", "medium", "low"):
                result["priority"] = value
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
                "status": default_status,
                "section": current_section,
                "description": "",
                "parent": None,
                "depends": [],
                "tags": [],
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

        # Tags
        if current_ticket and line_stripped.startswith("Tags:"):
            val = line_stripped.split(":", 1)[1].strip()
            if val:
                current_ticket["tags"] = [t.strip().lower() for t in val.split(",") if t.strip()]
            continue

        # Branches
        if current_ticket and line_stripped.startswith("Branch:"):
            val = line_stripped.split(":", 1)[1].strip()
            if val:
                current_ticket["branches"] = [b.strip() for b in val.split(",") if b.strip()]
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

        # Readiness content: Reviewed (Learnings). Tests/Smoke are no longer
        # tracked separately — their content moved into acceptance criteria
        # (migration 15). Legacy `Tests:` / `Smoke:` lines from older markdown
        # are silently ignored on ingest; the next sync will drop them, along
        # with any 4-space-indented continuation lines that followed them.
        if current_ticket and line_stripped.startswith("Reviewed:"):
            _, _, val = line_stripped.partition(":")
            current_ticket["readiness_content"]["reviewed"] = val.strip()
            current_ticket["_swallow_indented"] = False
            continue
        if current_ticket and line_stripped.startswith(("Tests:", "Smoke:")):
            current_ticket["_swallow_indented"] = True
            continue

        # Indented continuation of readiness content (4-space indent)
        if current_ticket and line.startswith("    "):
            if current_ticket.get("_swallow_indented"):
                continue
            if current_ticket.get("readiness_content"):
                # Append to the most recently set readiness flag
                last_flag = list(current_ticket["readiness_content"].keys())[-1]
                current_ticket["readiness_content"][last_flag] += "\n" + line_stripped
                continue

        # Description
        if current_ticket and line_stripped and not line_stripped.startswith("#"):
            current_ticket["_swallow_indented"] = False
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
                UPDATE tickets SET title=?, priority=?, status=?,
                    section=?, description=?, parent=?,
                    sort_order=?, commit_hash=COALESCE(NULLIF(?, ''), commit_hash),
                    release_tag=COALESCE(NULLIF(?, ''), release_tag), updated_at=?
                WHERE id=? AND project_id=?
            """, (
                t["title"], t["priority"], t["status"],
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

            # Replace tags
            if "tags" in t:
                conn.execute(
                    "DELETE FROM ticket_tags WHERE ticket_id=? AND project_id=?",
                    (tid, project_id)
                )
                for tag in t["tags"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO ticket_tags (ticket_id, project_id, tag) VALUES (?,?,?)",
                        (tid, project_id, tag)
                    )

            # Replace branches (from markdown only — preserve metadata for existing links)
            if "branches" in t:
                existing = {r["branch_name"] for r in conn.execute(
                    "SELECT branch_name FROM ticket_branches WHERE ticket_id=? AND project_id=?",
                    (tid, project_id)
                ).fetchall()}
                md_branches = set(t["branches"])
                # Remove branches no longer in markdown
                for removed in existing - md_branches:
                    conn.execute(
                        "DELETE FROM ticket_branches WHERE ticket_id=? AND project_id=? AND branch_name=?",
                        (tid, project_id, removed)
                    )
                # Add new branches from markdown
                for added in md_branches - existing:
                    conn.execute(
                        "INSERT OR IGNORE INTO ticket_branches (ticket_id, project_id, branch_name) VALUES (?,?,?)",
                        (tid, project_id, added)
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
                INSERT INTO tickets (id, project_id, title, priority, status,
                                     section, description, parent, sort_order,
                                     commit_hash, release_tag)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                tid, project_id, t["title"], t["priority"],
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

            # Insert tags for new tickets
            for tag in t.get("tags", []):
                conn.execute(
                    "INSERT OR IGNORE INTO ticket_tags (ticket_id, project_id, tag) VALUES (?,?,?)",
                    (tid, project_id, tag)
                )

            # Insert branches for new tickets
            for branch in t.get("branches", []):
                conn.execute(
                    "INSERT OR IGNORE INTO ticket_branches (ticket_id, project_id, branch_name) VALUES (?,?,?)",
                    (tid, project_id, branch)
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
            "SELECT * FROM tickets WHERE project_id = ? AND section = ? AND draft = 0 ORDER BY sort_order ASC",
            (project_id, section)
        ).fetchall()

        for t in tickets:
            # Header
            lines.append(f"### {t['id']}: {t['title']}")

            # Metadata
            lines.append(f"Priority: {t['priority']} | Status: {t['status']}")

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

            # Tags
            tags = conn.execute(
                "SELECT tag FROM ticket_tags WHERE ticket_id = ? AND project_id = ? ORDER BY tag",
                (t["id"], project_id)
            ).fetchall()
            if tags:
                tag_names = ", ".join(tg["tag"] for tg in tags)
                lines.append(f"Tags: {tag_names}")

            # Branches
            try:
                branches = conn.execute(
                    "SELECT branch_name FROM ticket_branches WHERE ticket_id = ? AND project_id = ? ORDER BY created_at",
                    (t["id"], project_id)
                ).fetchall()
                if branches:
                    branch_names = ", ".join(b["branch_name"] for b in branches)
                    lines.append(f"Branch: {branch_names}")
            except Exception:
                pass

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

            # Readiness content (Reviewed → Learnings). Tests/Smoke were
            # collapsed into acceptance criteria in migration 15.
            flags = conn.execute(
                "SELECT flag, content FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND content != '' ORDER BY flag",
                (t["id"], project_id)
            ).fetchall()
            for f in flags:
                label = {"reviewed": "Reviewed"}.get(f["flag"])
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
                UPDATE tickets SET title=?, priority=?, status=?,
                    section=?, description=?, parent=?,
                    sort_order=?, commit_hash=COALESCE(NULLIF(?, ''), commit_hash),
                    release_tag=COALESCE(NULLIF(?, ''), release_tag), updated_at=?
                WHERE id=? AND project_id=?
            """, (
                t["title"], t["priority"], t["status"],
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
                INSERT INTO tickets (id, project_id, title, priority, status,
                                     section, description, parent, sort_order,
                                     commit_hash, release_tag)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                tid, project_id, t["title"], t["priority"],
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

def seed_project(conn: sqlite3.Connection, project: dict) -> int:
    """Parse PRODUCT_BACKLOG.md for a single project and import into DB. Returns ticket count."""
    project_id = project["id"]
    project_path = os.path.expanduser(project.get("path", ""))
    backlog_path = os.path.join(project_path, "PRODUCT_BACKLOG.md")

    tickets = parse_backlog(backlog_path)
    if not tickets:
        return 0

    # Clear existing data for this project (idempotent)
    conn.execute("DELETE FROM tickets WHERE project_id = ?", (project_id,))

    for t in tickets:
        conn.execute("""
            INSERT INTO tickets (id, project_id, title, priority, status,
                                 section, description, parent, sort_order,
                                 commit_hash, release_tag)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            t["id"], project_id, t["title"], t["priority"],
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

        # Tags
        for tag in t.get("tags", []):
            conn.execute("""
                INSERT OR IGNORE INTO ticket_tags (ticket_id, project_id, tag)
                VALUES (?, ?, ?)
            """, (t["id"], project_id, tag))

        # Readiness content
        for flag, content in t.get("readiness_content", {}).items():
            if content:
                conn.execute("""
                    INSERT OR REPLACE INTO readiness_flags (ticket_id, project_id, flag, content, set_by)
                    VALUES (?, ?, ?, ?, 'seed')
                """, (t["id"], project_id, flag, content))

    conn.commit()
    return len(tickets)


def scaffold_project(conn: sqlite3.Connection, project: dict):
    """Create minimal PRODUCT_BACKLOG.md and PRODUCT_SPECIFICATION.md for a new project."""
    project_path = os.path.expanduser(project.get("path", ""))
    if not project_path:
        return
    project_name = project.get("name", project["id"])

    backlog = Path(project_path) / "PRODUCT_BACKLOG.md"
    if not backlog.exists():
        sync_to_markdown(conn, project)
        print(f"Created {backlog}")

    spec = Path(project_path) / "PRODUCT_SPECIFICATION.md"
    if not spec.exists():
        spec.write_text(f"# Product Specification \u2014 {project_name}\n\n", encoding="utf-8")
        print(f"Created {spec}")


def cmd_seed(args):
    """Parse PRODUCT_BACKLOG.md files and insert into DB."""
    projects = load_registry()
    target = resolve_project_id(projects, args.project)

    conn = get_db()
    init_db(conn)

    for proj in target:
        count = seed_project(conn, proj)
        if count:
            print(f"Seeded {count} tickets for {proj['name']}")
        else:
            print(f"No tickets found in {os.path.join(os.path.expanduser(proj.get('path', '')), 'PRODUCT_BACKLOG.md')}")

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
            print(f"{r['section']:<14} {r['id']:<10} {title:<40} {r['priority']:<8} {r['status']:<12}")

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

    add_kwargs = dict(
        section=section,
        priority=args.priority or "medium",
        description=args.description or "",
        parent=args.parent,
        draft=args.draft,
        tags=args.tag,
    )
    if args.container:
        add_kwargs["is_container"] = 1

    ticket_id = add_ticket(conn, project_id, args.title, **add_kwargs)
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
    if args.add_tag:
        kwargs["add_tags"] = args.add_tag
    if args.remove_tag:
        kwargs["remove_tags"] = args.remove_tag
    if args.add_branch:
        kwargs["add_branches"] = args.add_branch
    if args.remove_branch:
        kwargs["remove_branches"] = args.remove_branch
    if args.container is not None:
        kwargs["is_container"] = 1 if args.container else 0

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

VALID_FLAGS = {"reviewed"}


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

def cmd_seek(args):
    """Discover ticket-like items in project files and create draft tickets."""
    projects = load_registry()
    target = resolve_project_id(projects, args.project)
    if len(target) != 1:
        print("Error: seek requires a single project", file=sys.stderr)
        sys.exit(1)
    proj = target[0]
    project_path = os.path.expanduser(proj.get("path", ""))
    sources = args.sources.split(",") if args.sources else None

    conn = get_db()
    init_db(conn)
    ingest_markdown(conn, proj)

    from seek import run_seek
    result = run_seek(conn, proj["id"], project_path, sources=sources)

    sync_to_markdown(conn, proj)
    regenerate_dashboard(proj)
    conn.close()

    print(f"Discovered: {result['discovered']} items")
    print(f"Created: {result['created']} draft ticket(s)")
    print(f"Skipped: {result['skipped_duplicates']} duplicate(s)")
    if result['tickets']:
        print("New drafts:")
        for tid in result['tickets']:
            print(f"  {tid}")


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

    print(f"Registered: {pid} → {path}")

    backlog = Path(path) / "PRODUCT_BACKLOG.md"
    if backlog.exists():
        count = seed_project(conn, new_project)
        if count:
            print(f"Seeded {count} tickets from existing PRODUCT_BACKLOG.md")
        else:
            print(f"Found {backlog} but no tickets parsed.")
    else:
        scaffold_project(conn, new_project)

    conn.close()


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
# Subcommand: criteria
# ---------------------------------------------------------------------------

def cmd_criteria(args):
    """Manage acceptance criteria on a ticket."""
    projects = load_registry()
    proj = find_project(projects, args.project)
    project_id = proj["id"]

    conn = get_db()
    init_db(conn)
    ingest_markdown(conn, proj)

    if args.criteria_command == "list":
        ticket = conn.execute(
            "SELECT id, title FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
            (args.id, project_id)
        ).fetchone()
        if not ticket:
            print(f"Ticket {args.id} not found.", file=sys.stderr)
            conn.close()
            sys.exit(1)
        rows = conn.execute(
            "SELECT sort_order, checked, text FROM acceptance_criteria "
            "WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
            (ticket["id"], project_id)
        ).fetchall()
        if not rows:
            print(f"{ticket['id']}: {ticket['title']} — no criteria yet")
        else:
            print(f"{ticket['id']}: {ticket['title']}")
            for i, r in enumerate(rows, start=1):
                mark = "x" if r["checked"] else " "
                print(f"  {i}. [{mark}] {r['text']}")

    elif args.criteria_command == "add":
        try:
            tid = update_ticket(conn, project_id, args.id, add_criteria=[args.text])
        except (ValueError, IndexError) as e:
            print(str(e), file=sys.stderr)
            conn.close()
            sys.exit(1)
        conn.commit()
        sync_to_markdown(conn, proj)
        regenerate_dashboard(proj)
        print(f"Added criterion to {tid}")

    elif args.criteria_command == "remove":
        try:
            tid = update_ticket(conn, project_id, args.id, remove_criteria=args.n)
        except (ValueError, IndexError) as e:
            print(str(e), file=sys.stderr)
            conn.close()
            sys.exit(1)
        conn.commit()
        sync_to_markdown(conn, proj)
        regenerate_dashboard(proj)
        print(f"Removed criterion {args.n} from {tid}")

    elif args.criteria_command == "check":
        try:
            tid = update_ticket(conn, project_id, args.id, check_criteria=args.n)
        except (ValueError, IndexError) as e:
            print(str(e), file=sys.stderr)
            conn.close()
            sys.exit(1)
        conn.commit()
        sync_to_markdown(conn, proj)
        regenerate_dashboard(proj)
        print(f"Checked criterion {args.n} on {tid}")

    elif args.criteria_command == "uncheck":
        try:
            tid = update_ticket(conn, project_id, args.id, uncheck_criteria=args.n)
        except (ValueError, IndexError) as e:
            print(str(e), file=sys.stderr)
            conn.close()
            sys.exit(1)
        conn.commit()
        sync_to_markdown(conn, proj)
        regenerate_dashboard(proj)
        print(f"Unchecked criterion {args.n} on {tid}")

    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: agent
# ---------------------------------------------------------------------------

def cmd_agent(args):
    """Manage workflow agents."""
    conn = get_db()
    init_db(conn)

    if args.agent_command == "list":
        rows = conn.execute("SELECT * FROM workflow_agents ORDER BY name").fetchall()
        if not rows:
            print("No workflow agents defined.")
        else:
            print(f"{'ID':<20} {'Name':<25} {'Command':<15} {'Args':<20} {'Prompt'}")
            print("-" * 95)
            for r in rows:
                prompt_preview = (r["system_prompt"] or "")[:40]
                if len(r["system_prompt"] or "") > 40:
                    prompt_preview += "..."
                print(f"{r['id']:<20} {r['name']:<25} {r['command']:<15} {r['args']:<20} {prompt_preview}")

    elif args.agent_command == "add":
        name = args.name or args.agent_id.replace("-", " ").replace("_", " ").title()
        try:
            conn.execute(
                "INSERT INTO workflow_agents (id, name, command, args, system_prompt) VALUES (?, ?, ?, ?, ?)",
                (args.agent_id, name, args.cmd, args.args, args.system_prompt)
            )
            conn.commit()
            print(f"Added agent: {args.agent_id} ({name})")
        except sqlite3.IntegrityError:
            print(f"Error: Agent '{args.agent_id}' already exists.", file=sys.stderr)
            conn.close()
            sys.exit(1)

    elif args.agent_command == "update":
        fields = {}
        if args.name is not None:
            fields["name"] = args.name
        if args.cmd is not None:
            fields["command"] = args.cmd
        if args.args is not None:
            fields["args"] = args.args
        if args.system_prompt is not None:
            fields["system_prompt"] = args.system_prompt

        if not fields:
            print("Nothing to update. Provide at least one of --name, --cmd, --args, --system-prompt.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [args.agent_id]
        conn.execute(f"UPDATE workflow_agents SET {set_clause} WHERE id = ?", values)
        conn.commit()
        if conn.total_changes:
            print(f"Updated agent: {args.agent_id}")
        else:
            print(f"Agent '{args.agent_id}' not found.", file=sys.stderr)
            conn.close()
            sys.exit(1)

    elif args.agent_command == "remove":
        conn.execute("DELETE FROM workflow_agents WHERE id = ?", (args.agent_id,))
        conn.commit()
        print(f"Removed agent: {args.agent_id}")

    elif args.agent_command == "set-default":
        # Verify the agent exists
        row = conn.execute(
            "SELECT id FROM workflow_agents WHERE id = ?", (args.agent_id,)
        ).fetchone()
        if not row:
            print(f"Error: Agent '{args.agent_id}' not found.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        projects = load_registry()
        if args.project:
            target = [find_project(projects, args.project)]
        else:
            target = resolve_project_id(projects, None)

        for proj in target:
            # Settings table is global (key/value, no project_id column).
            # Use "{project_id}.agent.default" as the namespaced key so each
            # project can have an independent default.
            setting_key = f"{proj['id']}.agent.default"
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (setting_key, args.agent_id)
            )
        conn.commit()
        proj_names = ", ".join(p["id"] for p in target)
        print(f"Default agent set to '{args.agent_id}' for: {proj_names}")

    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: endpoint
# ---------------------------------------------------------------------------

def cmd_endpoint(args):
    """Manage runtime endpoints."""
    conn = get_db()
    init_db(conn)

    if args.endpoint_command == "list":
        from endpoints import list_endpoints
        eps = list_endpoints(conn)
        if not eps:
            print("No endpoints defined.")
        else:
            if getattr(args, "format", "table") == "json":
                from dataclasses import asdict
                print(json.dumps([asdict(e) for e in eps], indent=2, default=str))
            else:
                print(f"{'ID':<25} {'TYPE':<10} {'CMD':<15} {'SYS'}")
                print("-" * 60)
                for e in eps:
                    print(f"{e.id:<25} {e.endpoint_type:<10} "
                          f"{(e.command or ''):<15} {e.system}")

    elif args.endpoint_command == "add":
        if args.type != "cli":
            print(
                f"endpoint add: --type {args.type} requires API endpoint "
                f"execution support (not in phase 1). Create via the HTTP API "
                f"instead.",
                file=sys.stderr,
            )
            conn.close()
            sys.exit(2)
        from endpoints import Endpoint, create_endpoint, EndpointMisconfigured
        try:
            parsed_args = json.loads(args.args) if args.args else []
        except json.JSONDecodeError as e:
            print(f"--args is not valid JSON: {e}", file=sys.stderr)
            conn.close()
            sys.exit(2)
        ep = Endpoint(
            id=args.id,
            name=args.name or args.id,
            endpoint_type="cli",
            command=args.cmd,
            args=parsed_args,
            timeout_s=args.timeout_s,
        )
        try:
            created = create_endpoint(conn, ep)
        except EndpointMisconfigured as e:
            print(f"endpoint add: {e}", file=sys.stderr)
            conn.close()
            sys.exit(2)
        print(f"created endpoint {created.id}")

    elif args.endpoint_command == "update":
        from endpoints import update_endpoint, EndpointMisconfigured
        fields = {}
        if args.name is not None:
            fields["name"] = args.name
        if args.cmd is not None:
            fields["command"] = args.cmd
        if args.args is not None:
            try:
                fields["args"] = json.loads(args.args)
            except json.JSONDecodeError as e:
                print(f"--args invalid JSON: {e}", file=sys.stderr)
                conn.close()
                sys.exit(2)
        if args.timeout_s is not None:
            fields["timeout_s"] = args.timeout_s
        if not fields:
            print("Nothing to update. Provide at least one of --name, --cmd, --args, --timeout-s.", file=sys.stderr)
            conn.close()
            sys.exit(1)
        try:
            updated = update_endpoint(conn, args.id, **fields)
        except KeyError:
            print(f"endpoint not found: {args.id}", file=sys.stderr)
            conn.close()
            sys.exit(1)
        except PermissionError:
            print(
                f"endpoint {args.id} is a system row — edit "
                f"src/workflows_seed.py and restart",
                file=sys.stderr,
            )
            conn.close()
            sys.exit(2)
        except EndpointMisconfigured as e:
            print(f"endpoint update: {e}", file=sys.stderr)
            conn.close()
            sys.exit(2)
        print(f"updated endpoint {updated.id}")

    elif args.endpoint_command == "remove":
        from endpoints import delete_endpoint
        try:
            n = delete_endpoint(conn, args.id)
        except KeyError:
            print(f"endpoint not found: {args.id}", file=sys.stderr)
            conn.close()
            sys.exit(1)
        except PermissionError:
            print(f"endpoint {args.id} is a system row — cannot remove", file=sys.stderr)
            conn.close()
            sys.exit(2)
        print(f"removed endpoint {args.id} (unlinked {n} agents)")

    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: workflow
# ---------------------------------------------------------------------------

def cmd_workflow(args):
    """Manage workflow definitions."""
    conn = get_db()
    init_db(conn)

    if args.workflow_command == "list":
        rows = conn.execute("SELECT * FROM workflows ORDER BY name").fetchall()
        if not rows:
            print("No workflows defined.")
        else:
            for r in rows:
                steps = json.loads(r["steps"] or "[]")
                desc = r["description"] or ""
                print(f"{r['id']:<20} {r['name']:<30} ({len(steps)} steps)  {desc}")
                for i, step in enumerate(steps):
                    label = step.get("label", step.get("agent_id", "?"))
                    agent = step.get("agent_id", "?")
                    modifier = step.get("prompt_modifier", "")
                    mod_preview = f"  [{modifier[:30]}...]" if len(modifier) > 30 else (f"  [{modifier}]" if modifier else "")
                    print(f"  {i}: agent={agent}  label={label}{mod_preview}")

    elif args.workflow_command == "add":
        name = args.name or args.workflow_id.replace("-", " ").replace("_", " ").title()
        try:
            conn.execute(
                "INSERT INTO workflows (id, name, description, steps) VALUES (?, ?, ?, ?)",
                (args.workflow_id, name, args.description or "", "[]")
            )
            conn.commit()
            print(f"Added workflow: {args.workflow_id} ({name})")
        except sqlite3.IntegrityError:
            print(f"Error: Workflow '{args.workflow_id}' already exists.", file=sys.stderr)
            conn.close()
            sys.exit(1)

    elif args.workflow_command == "add-step":
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (args.workflow_id,)).fetchone()
        if not row:
            print(f"Workflow '{args.workflow_id}' not found.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        steps = json.loads(row["steps"] or "[]")
        steps.append({
            "agent_id": args.agent,
            "label": args.label or args.agent,
            "prompt_modifier": args.prompt_modifier or "",
        })
        conn.execute("UPDATE workflows SET steps = ? WHERE id = ?", (json.dumps(steps), args.workflow_id))
        conn.commit()
        print(f"Added step {len(steps) - 1} (agent={args.agent}) to workflow {args.workflow_id}")

    elif args.workflow_command == "remove-step":
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (args.workflow_id,)).fetchone()
        if not row:
            print(f"Workflow '{args.workflow_id}' not found.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        steps = json.loads(row["steps"] or "[]")
        idx = args.step
        if idx < 0 or idx >= len(steps):
            print(f"Step index {idx} out of range (0-{len(steps) - 1}).", file=sys.stderr)
            conn.close()
            sys.exit(1)

        removed = steps.pop(idx)
        conn.execute("UPDATE workflows SET steps = ? WHERE id = ?", (json.dumps(steps), args.workflow_id))
        conn.commit()
        print(f"Removed step {idx} (agent={removed.get('agent_id', '?')}) from workflow {args.workflow_id}")

    elif args.workflow_command == "remove":
        conn.execute("DELETE FROM workflows WHERE id = ?", (args.workflow_id,))
        conn.commit()
        print(f"Removed workflow: {args.workflow_id}")

    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: branches
# ---------------------------------------------------------------------------

def cmd_branches(args):
    """Manage branch-ticket links."""
    projects = load_registry()
    proj = find_project(projects, args.project)
    project_id = proj["id"]
    project_path = os.path.expanduser(proj.get("path", ""))

    conn = get_db()
    init_db(conn)

    if args.branches_command == "list":
        if args.ticket:
            rows = get_ticket_branches(conn, project_id, args.ticket)
        else:
            rows = get_project_branches(conn, project_id)
        if not rows:
            print("No branches linked.")
        else:
            print(f"{'Ticket':<12} {'Branch':<40} {'PR':<8} {'Status':<10} {'Ahead':<6} {'Behind':<6} {'Auto'}")
            print("-" * 96)
            for r in rows:
                pr_str = f"#{r['pr_number']}" if r.get("pr_number") else ""
                auto_str = "auto" if r.get("auto_linked") else ""
                print(f"{r['ticket_id']:<12} {r['branch_name']:<40} {pr_str:<8} {r.get('pr_status', ''):<10} {r.get('ahead', 0):<6} {r.get('behind', 0):<6} {auto_str}")

    elif args.branches_command == "link":
        ingest_markdown(conn, proj)
        try:
            created = link_branch(conn, project_id, args.ticket_id, args.branch_name)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            conn.close()
            sys.exit(1)
        conn.commit()
        sync_to_markdown(conn, proj)
        regenerate_dashboard(proj)
        if created:
            print(f"Linked {args.branch_name} → {args.ticket_id}")
        else:
            print(f"Already linked.")

    elif args.branches_command == "unlink":
        ingest_markdown(conn, proj)
        removed = unlink_branch(conn, project_id, args.ticket_id, args.branch_name)
        conn.commit()
        sync_to_markdown(conn, proj)
        regenerate_dashboard(proj)
        if removed:
            print(f"Unlinked {args.branch_name} from {args.ticket_id}")
        else:
            print(f"Link not found.")

    elif args.branches_command == "scan":
        ingest_markdown(conn, proj)
        result = scan_branches(conn, project_id, project_path)
        print(f"Scanned {result.get('total_remote', 0)} remote branches, auto-linked {result.get('linked', 0)} new.")
        if result.get("error"):
            print(f"  Warning: {result['error']}")

        if not args.no_prs:
            pr_result = scan_prs(conn, project_id, project_path)
            print(f"PR enrichment: updated {pr_result.get('updated', 0)} branch links.")
            if pr_result.get("error"):
                print(f"  Warning: {pr_result['error']}")

        conn.commit()
        sync_to_markdown(conn, proj)
        regenerate_dashboard(proj)

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
    p_add.add_argument("--parent", help="Parent ticket ID")
    p_add.add_argument("--description", help="Description text")
    p_add.add_argument("--tag", action="append", help="Add tag (repeatable)")
    p_add.add_argument("--draft", action="store_true", help="Create as draft ticket")
    p_add.add_argument("--container", action="store_true", default=False, help="Mark as container (epic) ticket")

    # update
    p_upd = sub.add_parser("update", help="Update a ticket")
    p_upd.add_argument("project", help="Project ID")
    p_upd.add_argument("id", help="Ticket ID")
    p_upd.add_argument("--title", help="New title")
    p_upd.add_argument("--priority", help="New priority")
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
    p_upd.add_argument("--add-tag", action="append", help="Add tag (repeatable)")
    p_upd.add_argument("--remove-tag", action="append", help="Remove tag (repeatable)")
    p_upd.add_argument("--add-branch", action="append", help="Link branch (repeatable)")
    p_upd.add_argument("--remove-branch", action="append", help="Unlink branch (repeatable)")
    p_upd.add_argument("--confirm", action="store_true", help="Confirm a draft ticket (set draft=false)")
    container_grp = p_upd.add_mutually_exclusive_group()
    container_grp.add_argument("--container", dest="container", action="store_const", const=True, default=None, help="Mark ticket as container (epic)")
    container_grp.add_argument("--no-container", dest="container", action="store_const", const=False, help="Clear container flag")

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

    p_seek = sub.add_parser("seek", help="Discover ticket-like items and create drafts")
    p_seek.add_argument("project", help="Project ID")
    p_seek.add_argument("--sources", help="Comma-separated: md_task,readme_todo,code_todo,changelog,github_issue")

    p_reg = sub.add_parser("register", help="Register a new project")
    p_reg.add_argument("--id", required=True, help="Project ID (lowercase, hyphens OK)")
    p_reg.add_argument("--name", help="Display name (default: same as ID)")
    p_reg.add_argument("--path", required=True, help="Path to project root")
    p_reg.add_argument("--description", help="Project description")

    p_unreg = sub.add_parser("unregister", help="Deactivate a project")
    p_unreg.add_argument("id", help="Project ID to deactivate")
    p_unreg.add_argument("--delete-tickets", action="store_true", help="Also delete tickets from DB (destructive)")

    # criteria
    p_crit = sub.add_parser("criteria", help="Manage acceptance criteria on a ticket")
    p_crit.add_argument("project", help="Project ID")
    crit_sub = p_crit.add_subparsers(dest="criteria_command", required=True)

    p_crit_list = crit_sub.add_parser("list", help="List criteria for a ticket")
    p_crit_list.add_argument("id", help="Ticket ID")

    p_crit_add = crit_sub.add_parser("add", help="Add a criterion to a ticket")
    p_crit_add.add_argument("id", help="Ticket ID")
    p_crit_add.add_argument("text", help="Criterion text")

    p_crit_rm = crit_sub.add_parser("remove", help="Remove Nth criterion (1-indexed)")
    p_crit_rm.add_argument("id", help="Ticket ID")
    p_crit_rm.add_argument("n", type=int, help="Criterion index (1-indexed)")

    p_crit_check = crit_sub.add_parser("check", help="Mark Nth criterion as done (1-indexed)")
    p_crit_check.add_argument("id", help="Ticket ID")
    p_crit_check.add_argument("n", type=int, help="Criterion index (1-indexed)")

    p_crit_uncheck = crit_sub.add_parser("uncheck", help="Unmark Nth criterion (1-indexed)")
    p_crit_uncheck.add_argument("id", help="Ticket ID")
    p_crit_uncheck.add_argument("n", type=int, help="Criterion index (1-indexed)")

    # agent
    p_agent = sub.add_parser("agent", help="Manage workflow agents")
    agent_sub = p_agent.add_subparsers(dest="agent_command", required=True)

    agent_sub.add_parser("list", help="List all workflow agents")

    p_agent_add = agent_sub.add_parser("add", help="Add a workflow agent")
    p_agent_add.add_argument("agent_id", help="Agent ID")
    p_agent_add.add_argument("--name", help="Display name (default: derived from ID)")
    p_agent_add.add_argument("--cmd", default="claude", help="Command to run (default: claude)")
    p_agent_add.add_argument("--args", default="[]", help="JSON array of command args")
    p_agent_add.add_argument("--system-prompt", default="", help="System prompt for the agent")

    p_agent_upd = agent_sub.add_parser("update", help="Update a workflow agent")
    p_agent_upd.add_argument("agent_id", help="Agent ID")
    p_agent_upd.add_argument("--name", help="New display name")
    p_agent_upd.add_argument("--cmd", help="New command")
    p_agent_upd.add_argument("--args", help="New JSON array of command args")
    p_agent_upd.add_argument("--system-prompt", help="New system prompt")

    p_agent_rm = agent_sub.add_parser("remove", help="Remove a workflow agent")
    p_agent_rm.add_argument("agent_id", help="Agent ID to remove")

    p_agent_setdef = agent_sub.add_parser("set-default", help="Set the project default agent (stored in settings as agent.default)")
    p_agent_setdef.add_argument("agent_id", help="Agent ID to set as default")
    p_agent_setdef.add_argument("--project", help="Project ID (default: auto-detect or all)")

    # endpoint
    p_ep = sub.add_parser("endpoint", help="Manage runtime endpoints")
    ep_sub = p_ep.add_subparsers(dest="endpoint_command", required=True)

    ep_list = ep_sub.add_parser("list", help="List all endpoints")
    ep_list.add_argument("--format", choices=["table", "json"], default="table")

    ep_add = ep_sub.add_parser("add", help="Add an endpoint")
    ep_add.add_argument("id", help="Endpoint ID")
    ep_add.add_argument("--type", default="cli", help="Endpoint type (phase 1: cli only)")
    ep_add.add_argument("--name", default=None, help="Display name (default: same as ID)")
    ep_add.add_argument("--cmd", required=True, help="Command to run")
    ep_add.add_argument("--args", default="[]", help="JSON array of command args")
    ep_add.add_argument("--timeout-s", type=int, default=120, dest="timeout_s", help="Timeout in seconds (default: 120)")

    ep_upd = ep_sub.add_parser("update", help="Update an endpoint")
    ep_upd.add_argument("id", help="Endpoint ID")
    ep_upd.add_argument("--name", default=None, help="New display name")
    ep_upd.add_argument("--cmd", default=None, help="New command")
    ep_upd.add_argument("--args", default=None, help="New JSON array of command args")
    ep_upd.add_argument("--timeout-s", type=int, default=None, dest="timeout_s", help="New timeout in seconds")

    ep_rm = ep_sub.add_parser("remove", help="Remove an endpoint")
    ep_rm.add_argument("id", help="Endpoint ID to remove")

    # workflow
    p_wf = sub.add_parser("workflow", help="Manage workflow definitions")
    wf_sub = p_wf.add_subparsers(dest="workflow_command", required=True)

    wf_sub.add_parser("list", help="List all workflows")

    p_wf_add = wf_sub.add_parser("add", help="Add a workflow")
    p_wf_add.add_argument("workflow_id", help="Workflow ID")
    p_wf_add.add_argument("--name", help="Display name (default: derived from ID)")
    p_wf_add.add_argument("--description", help="Workflow description")

    p_wf_step = wf_sub.add_parser("add-step", help="Add a step to a workflow")
    p_wf_step.add_argument("workflow_id", help="Workflow ID")
    p_wf_step.add_argument("--agent", required=True, help="Agent ID for this step")
    p_wf_step.add_argument("--label", help="Step label (default: agent ID)")
    p_wf_step.add_argument("--prompt-modifier", default="", help="Prompt modifier for this step")

    p_wf_rmstep = wf_sub.add_parser("remove-step", help="Remove a step from a workflow")
    p_wf_rmstep.add_argument("workflow_id", help="Workflow ID")
    p_wf_rmstep.add_argument("--step", type=int, required=True, help="Step index to remove (0-based)")

    p_wf_rm = wf_sub.add_parser("remove", help="Remove a workflow")
    p_wf_rm.add_argument("workflow_id", help="Workflow ID to remove")

    # branches
    p_br = sub.add_parser("branches", help="Manage branch-ticket links")
    p_br.add_argument("project", help="Project ID")
    br_sub = p_br.add_subparsers(dest="branches_command")
    br_sub.required = True

    p_br_list = br_sub.add_parser("list", help="List branch links")
    p_br_list.add_argument("--ticket", help="Filter by ticket ID")

    p_br_link = br_sub.add_parser("link", help="Link a branch to a ticket")
    p_br_link.add_argument("ticket_id", help="Ticket ID")
    p_br_link.add_argument("branch_name", help="Branch name")

    p_br_unlink = br_sub.add_parser("unlink", help="Unlink a branch from a ticket")
    p_br_unlink.add_argument("ticket_id", help="Ticket ID")
    p_br_unlink.add_argument("branch_name", help="Branch name")

    p_br_scan = br_sub.add_parser("scan", help="Scan remote branches and PRs")
    p_br_scan.add_argument("--no-prs", action="store_true", help="Skip PR enrichment via gh")

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
        "seek": cmd_seek,
        "register": cmd_register,
        "unregister": cmd_unregister,
        "criteria": cmd_criteria,
        "agent": cmd_agent,
        "endpoint": cmd_endpoint,
        "workflow": cmd_workflow,
        "branches": cmd_branches,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
