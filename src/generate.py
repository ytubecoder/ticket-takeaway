#!/usr/bin/env python3
"""
Ticket Takeaway Dashboard Generator

Reads project registry and PRODUCT_BACKLOG.md files to generate
a self-contained HTML kanban dashboard at {project}/docs/sdlc-dashboard.html
"""

import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from constants import (SECTION_ORDER, SECTION_SLUGS, SLUG_TO_SECTION,
                       DEFAULT_STATUS_BY_SECTION, CARD_CLASS_BY_SLUG, STATUSES, FEEDBACKS_REPO_URL)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DASHBOARD_DIR = Path.home() / ".claude" / "ticket-takeaway"
REGISTRY_PATH = DASHBOARD_DIR / "registry.json"
# OUTPUT_PATH is now per-project: {project.path}/docs/sdlc-dashboard.html


# ---------------------------------------------------------------------------
# SVG Icons (Lucide-style, 24x24 viewBox, stroke-based)
# ---------------------------------------------------------------------------

SVG_ICONS = {
    "file-text": '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/>',
    "check-square": '<polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
    "flame": '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    "flask-conical": '<path d="M10 2v7.527a2 2 0 0 1-.211.896L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.069-10.127A2 2 0 0 1 14 9.527V2"/><path d="M8.5 2h7"/><path d="M7 16.5h10"/>',
    "eye": '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "arrow-up-right": '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
    "settings": '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "trash-2": '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
    "undo-2": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "snowflake": '<line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/><path d="m20 16-4-4 4-4"/><path d="m4 8 4 4-4 4"/><path d="m16 4-4 4-4-4"/><path d="m8 20 4-4 4 4"/>',
    "arrow-left": '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    "mic": '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/>',
    "route": '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>',
    "square": '<rect width="18" height="18" x="3" y="3" rx="2"/>',
    "rotate-ccw": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
    "send": '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/>',
}


def _svg_icon(name: str, size: int = 16, cls: str = "") -> str:
    """Return an inline SVG icon element."""
    extra = f' class="{cls}"' if cls else ""
    return (f'<svg{extra} width="{size}" height="{size}" viewBox="0 0 24 24" '
            f'fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">'
            f'{SVG_ICONS.get(name, "")}</svg>')


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Ticket:
    id: str
    title: str
    priority: str = "medium"
    complexity: str = "M"
    status: str = "proposed"
    section: str = "Ideas"
    description: str = ""
    acceptance_criteria: list = field(default_factory=list)
    parent: Optional[str] = None
    depends: list = field(default_factory=list)
    summary: str = ""
    archived: bool = False
    commit_hash: str = ""
    release_tag: str = ""
    readiness_flags: set = field(default_factory=set)  # explicit flags from DB
    readiness_content: dict = field(default_factory=dict)  # {flag: content_text}
    draft: bool = False
    attachment_count: int = 0
    # Kitchen (M1a) — derived for the card badge.
    automation_mode: str = "manual"   # manual | auto | held
    latest_run_status: Optional[str] = None  # None until M3 produces real runs
    # Kitchen (M2) — computed eligibility (auto ∧ all DCSTL gates clear).
    automation_eligible: bool = False

    @property
    def slug(self) -> str:
        return SECTION_SLUGS.get(self.section, "backlog")


@dataclass
class CodeStats:
    files: int = 0
    loc: str = "0"
    deps: str = "0"
    last_commit: str = "n/a"
    releases: int = 0
    version: str = "v0.0.0"
    sparkline: list = field(default_factory=list)


@dataclass
class Project:
    id: str
    name: str
    path: str
    description: str = ""
    active: bool = True
    tickets: list = field(default_factory=list)
    code_stats: CodeStats = field(default_factory=CodeStats)


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd: str, cwd: str = None, default: str = "") -> str:
    """Run a shell command and return stdout, or default on failure."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_backlog(filepath: str) -> list[Ticket]:
    """Parse a PRODUCT_BACKLOG.md file into a list of Tickets."""
    path = Path(filepath)
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    tickets: list[Ticket] = []
    current_section: Optional[str] = None
    current_ticket: Optional[Ticket] = None

    for line in text.splitlines():
        line_stripped = line.strip()

        # Detect ## section headers
        if line_stripped.startswith("## ") and not line_stripped.startswith("### "):
            section_name = line_stripped[3:].strip()
            if section_name in SECTION_ORDER:
                current_section = section_name
            # Flush any open ticket
            if current_ticket:
                tickets.append(current_ticket)
                current_ticket = None
            continue

        # Detect ### ticket headers
        if line_stripped.startswith("### ") and current_section:
            # Flush previous ticket
            if current_ticket:
                tickets.append(current_ticket)

            header = line_stripped[4:].strip()
            ticket_id, title = _parse_ticket_header(header)
            default_status = DEFAULT_STATUS_BY_SECTION.get(current_section, "proposed")

            current_ticket = Ticket(
                id=ticket_id,
                title=title,
                section=current_section,
                status=default_status,
            )
            continue

        # Detect metadata line: Priority: X | Complexity: Y | Status: Z
        if current_ticket and line_stripped.startswith("Priority:"):
            meta = _parse_metadata_line(line_stripped)
            current_ticket.priority = meta.get("priority", current_ticket.priority)
            current_ticket.complexity = meta.get("complexity", current_ticket.complexity)
            current_ticket.status = meta.get("status", current_ticket.status)
            continue

        # Detect Parent: field (appears on its own line after metadata)
        if current_ticket and line_stripped.startswith("Parent:"):
            parent_value = line_stripped.split(":", 1)[1].strip()
            if parent_value:
                current_ticket.parent = parent_value
            continue

        # Detect Depends: field (comma-separated ticket IDs)
        if current_ticket and line_stripped.startswith("Depends:"):
            deps_value = line_stripped.split(":", 1)[1].strip()
            if deps_value:
                current_ticket.depends = [d.strip() for d in deps_value.split(",") if d.strip()]
            continue

        # Acceptance criteria (checkbox lines)
        if current_ticket and re.match(r"^- \[[ xX]\]", line_stripped):
            checked = line_stripped[3] in ("x", "X")
            text_content = line_stripped[5:].strip()
            current_ticket.acceptance_criteria.append((checked, text_content))
            continue

        # Description lines
        if current_ticket and line_stripped and not line_stripped.startswith("#"):
            if current_ticket.description:
                current_ticket.description += " " + line_stripped
            else:
                current_ticket.description = line_stripped

    # Flush final ticket
    if current_ticket:
        tickets.append(current_ticket)

    return tickets


def _parse_ticket_header(header: str) -> tuple[str, str]:
    """Parse '### ID: Title' into (id, title). If no colon, use full text as title."""
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


def load_tickets_from_db(db_path: str, project_id: str) -> list[Ticket]:
    """Load tickets from SQLite database, returning Ticket objects matching parse_backlog format."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT * FROM tickets WHERE project_id = ? ORDER BY sort_order ASC",
        (project_id,)
    ).fetchall()

    tickets = []
    for r in rows:
        # Acceptance criteria
        criteria_rows = conn.execute(
            "SELECT text, checked FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ? ORDER BY sort_order ASC",
            (r["id"], project_id)
        ).fetchall()
        criteria = [(bool(c["checked"]), c["text"]) for c in criteria_rows]

        # Dependencies
        dep_rows = conn.execute(
            "SELECT depends_on_id FROM depends WHERE ticket_id = ? AND project_id = ?",
            (r["id"], project_id)
        ).fetchall()
        depends = [d["depends_on_id"] for d in dep_rows]

        # Safe access for columns that may not exist in older DBs
        try:
            commit_hash = r["commit_hash"]
        except (IndexError, KeyError):
            commit_hash = ""
        try:
            release_tag = r["release_tag"]
        except (IndexError, KeyError):
            release_tag = ""

        # Readiness flags and content
        try:
            flag_rows = conn.execute(
                "SELECT flag, content FROM readiness_flags WHERE ticket_id = ? AND project_id = ?",
                (r["id"], project_id)
            ).fetchall()
            flags = {f["flag"] for f in flag_rows}
            readiness_content = {f["flag"]: f["content"] for f in flag_rows}
        except Exception:
            flags = set()
            readiness_content = {}

        # Draft flag
        try:
            is_draft = bool(r["draft"])
        except (IndexError, KeyError):
            is_draft = False

        # Attachment count
        try:
            att_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM ticket_attachments WHERE ticket_id = ? AND project_id = ?",
                (r["id"], project_id)
            ).fetchone()
            attachment_count = att_row["cnt"] if att_row else 0
        except Exception:
            attachment_count = 0

        # Kitchen state (M1a) — automation_mode + latest_run_status. Pre-migration DBs
        # silently fall back to defaults.
        automation_mode = "manual"
        latest_run_status = None
        automation_eligible = False
        try:
            am = conn.execute(
                "SELECT automation_mode FROM automation_subjects "
                "WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ?",
                (project_id, r["id"]),
            ).fetchone()
            if am:
                automation_mode = am["automation_mode"]
            lr = conn.execute(
                "SELECT status FROM runs WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (project_id, r["id"]),
            ).fetchone()
            if lr:
                latest_run_status = lr["status"]
            # M2: compute eligibility once at load. Cheap because conn is hot.
            try:
                from actions import eligibility as _kitchen_eligibility  # local import: avoids hard dep at module load
                er = _kitchen_eligibility(conn, project_id, "ticket", r["id"])
                automation_eligible = er.eligible
            except Exception:
                automation_eligible = False
        except Exception:
            pass

        tickets.append(Ticket(
            id=r["id"],
            title=r["title"],
            priority=r["priority"],
            complexity=r["complexity"],
            status=r["status"],
            section=r["section"],
            description=r["description"],
            acceptance_criteria=criteria,
            parent=r["parent"],
            depends=depends,
            summary=r["summary"],
            archived=bool(r["archived"]),
            commit_hash=commit_hash,
            release_tag=release_tag,
            readiness_flags=flags,
            readiness_content=readiness_content,
            draft=is_draft,
            attachment_count=attachment_count,
            automation_mode=automation_mode,
            latest_run_status=latest_run_status,
            automation_eligible=automation_eligible,
        ))

    conn.close()
    return tickets


def parse_spec_for_done(filepath: str) -> list[Ticket]:
    """Parse PRODUCT_SPECIFICATION.md for done items (### headings with IDs).

    Supports an optional ## Archive section — tickets below it get archived=True.
    Captures description text as summary for each entry.
    """
    path = Path(filepath)
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    tickets: list[Ticket] = []
    current_ticket: Ticket | None = None
    in_archive = False

    for line in text.splitlines():
        line_stripped = line.strip()

        # Section detection (## Archive)
        if line_stripped.startswith("## "):
            section_name = line_stripped[3:].strip()
            in_archive = section_name.lower() == "archive"
            if current_ticket:
                tickets.append(current_ticket)
                current_ticket = None
            continue

        # Ticket heading
        if line_stripped.startswith("### ") and ":" in line_stripped[4:]:
            if current_ticket:
                tickets.append(current_ticket)
            header = line_stripped[4:].strip()
            ticket_id, title = _parse_ticket_header(header)
            if ticket_id:
                current_ticket = Ticket(
                    id=ticket_id,
                    title=title,
                    status="released",
                    section="Done",
                    archived=in_archive,
                )
            else:
                current_ticket = None
            continue

        # Skip metadata/release lines, capture description as summary
        if current_ticket and line_stripped:
            if line_stripped.startswith("Priority:") or line_stripped.startswith("Released:"):
                continue
            if line_stripped.startswith("#") or line_stripped.startswith("---"):
                continue
            if current_ticket.summary:
                current_ticket.summary += " " + line_stripped
            else:
                current_ticket.summary = line_stripped

    if current_ticket:
        tickets.append(current_ticket)

    return tickets


def compute_dependency_state(tickets: list[Ticket]) -> dict[str, dict]:
    """For each ticket, compute whether its dependencies are resolved."""
    status_by_id = {t.id: t.status for t in tickets}
    DONE_STATUSES = {"done", "released", "wont-do"}
    result = {}
    for t in tickets:
        if not t.depends:
            result[t.id] = {"deps_resolved": True, "blocking_deps": []}
            continue
        blocking = [dep for dep in t.depends
                    if status_by_id.get(dep, "unknown") not in DONE_STATUSES]
        result[t.id] = {"deps_resolved": len(blocking) == 0, "blocking_deps": blocking}
    return result


def auto_promote_parents(
    by_section: dict[str, list[Ticket]],
    child_tickets: dict[str, list[Ticket]],
) -> set[str]:
    """Move parents to For Review when all children are resolved.

    Checks parents in WIP, Backlog, and Bugs sections. If every child ticket
    has status in {"for-review", "bug-fixed", "done"}, the parent is moved
    to For Review.

    Returns the set of promoted ticket IDs.
    """
    review_statuses = {"for-review", "bug-fixed", "done"}
    promoted_ids: set[str] = set()
    for parent_id, children in child_tickets.items():
        if all(c.status in review_statuses for c in children):
            for sec in ("WIP", "Backlog", "Bugs"):
                for t in by_section.get(sec, []):
                    if t.id == parent_id:
                        by_section[sec].remove(t)
                        by_section.setdefault("For Review", []).append(t)
                        promoted_ids.add(t.id)
                        break
    return promoted_ids


# ---------------------------------------------------------------------------
# Code stats collection
# ---------------------------------------------------------------------------

def collect_code_stats(project_path: str) -> CodeStats:
    """Collect git and codebase stats for a project."""
    stats = CodeStats()
    cwd = project_path

    if not Path(cwd).exists():
        return stats

    # Total files in src/
    src_path = os.path.join(cwd, "src")
    if os.path.isdir(src_path):
        count = run_cmd(f"find '{src_path}' -type f | wc -l", cwd=cwd, default="0")
        stats.files = int(count) if count.isdigit() else 0

    # LOC count (approximate via wc -l on src/)
    if os.path.isdir(src_path):
        loc_raw = run_cmd(
            f"find '{src_path}' -type f \\( -name '*.ts' -o -name '*.tsx' -o -name '*.js' "
            f"-o -name '*.jsx' -o -name '*.py' -o -name '*.css' \\) "
            f"-exec cat {{}} + 2>/dev/null | wc -l",
            cwd=cwd, default="0"
        )
        loc_num = int(loc_raw) if loc_raw.isdigit() else 0
        if loc_num >= 1000:
            stats.loc = f"{loc_num // 1000}k"
        else:
            stats.loc = str(loc_num)

    # Dependencies from package.json
    pkg_path = os.path.join(cwd, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
            deps = len(pkg.get("dependencies", {}))
            dev_deps = len(pkg.get("devDependencies", {}))
            stats.deps = f"{deps}+{dev_deps}dev"
        except Exception:
            pass

    # Version from package.json
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
            version = pkg.get("version", "0.0.0")
            stats.version = f"v{version}"
        except Exception:
            pass

    # Last commit age
    last_commit_ts = run_cmd("git log -1 --format=%ct 2>/dev/null", cwd=cwd, default="")
    if last_commit_ts.isdigit():
        age_seconds = int(datetime.now().timestamp()) - int(last_commit_ts)
        if age_seconds < 3600:
            stats.last_commit = f"{max(1, age_seconds // 60)}m ago"
        elif age_seconds < 86400:
            stats.last_commit = f"{age_seconds // 3600}h ago"
        else:
            stats.last_commit = f"{age_seconds // 86400}d ago"

    # Release/tag count
    tag_count = run_cmd("git tag 2>/dev/null | wc -l", cwd=cwd, default="0")
    stats.releases = int(tag_count) if tag_count.strip().isdigit() else 0

    # Sparkline data: commits per week for last 12 weeks
    sparkline_raw = run_cmd(
        "git log --since='12 weeks ago' --format=%ct 2>/dev/null",
        cwd=cwd, default=""
    )
    if sparkline_raw:
        now = datetime.now().timestamp()
        week_buckets = [0] * 12
        for ts_str in sparkline_raw.splitlines():
            if ts_str.strip().isdigit():
                age = now - int(ts_str.strip())
                week_idx = min(11, int(age / (7 * 86400)))
                week_buckets[11 - week_idx] += 1
        stats.sparkline = week_buckets
    else:
        stats.sparkline = [0] * 12

    return stats


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(project: Project) -> str:
    """Generate the full self-contained HTML dashboard for a single project."""
    primary = project
    all_tickets: list[Ticket] = list(project.tickets)

    # Categorize tickets by section
    by_section: dict[str, list[Ticket]] = {s: [] for s in SECTION_ORDER}
    for t in all_tickets:
        if t.section in by_section:
            by_section[t.section].append(t)

    # Code stats
    cs = primary.code_stats if primary else CodeStats()

    # Date
    now = datetime.now()
    date_str = now.strftime("%b %-d")
    project_name = primary.name if primary else "Dashboard"
    project_short = primary.id.title() if primary else "Project"

    sparkline_json = json.dumps(cs.sparkline if cs.sparkline else [0] * 12)
    gen_ts = str(int(now.timestamp() * 1000))

    # Build parent → child ticket mapping
    child_tickets: dict[str, list[Ticket]] = {}
    for t in all_tickets:
        if t.parent:
            child_tickets.setdefault(t.parent, []).append(t)

    # Auto-promote parents to For Review when all child tickets are resolved
    promoted_ids = auto_promote_parents(by_section, child_tickets)
    parented_ids = {t.id for t in all_tickets if t.parent}

    # Reorder sections: place children directly after their parent
    for sec in by_section:
        ordered = []
        seen = set()
        for t in by_section[sec]:
            if t.id in seen:
                continue
            seen.add(t.id)
            ordered.append(t)
            for child in child_tickets.get(t.id, []):
                if child.id not in seen:
                    seen.add(child.id)
                    ordered.append(child)
        by_section[sec] = ordered

    # Count totals (exclude children from headline counts)
    count_backlog = sum(1 for t in by_section["Backlog"] if t.id not in parented_ids)
    count_wip = sum(1 for t in by_section["WIP"] if t.id not in parented_ids)
    count_ideas = sum(1 for t in by_section["Ideas"] if t.id not in parented_ids)
    count_wontdo = sum(1 for t in by_section["Won't Do"] if t.id not in parented_ids)
    count_review = sum(1 for t in by_section["For Review"] if t.id not in parented_ids)
    count_done = sum(1 for t in by_section["Done"] if t.id not in parented_ids)
    count_icebox = sum(1 for t in by_section["Icebox"] if t.id not in parented_ids)
    count_bugs = sum(1 for t in by_section["Bugs"] if t.id not in parented_ids)
    count_total = count_backlog + count_wip + count_review + count_ideas + count_done

    # Cross-cutting filter counts (across all sections, excluding children)
    all_visible = [t for sec in by_section.values() for t in sec if t.id not in parented_ids]
    count_status_proposed = sum(1 for t in all_visible if t.status.replace(" ", "-").lower() == "proposed")
    count_status_inprogress = sum(1 for t in all_visible if t.status.replace(" ", "-").lower() == "in-progress")
    count_status_forreview = sum(1 for t in all_visible if t.status.replace(" ", "-").lower() == "for-review")
    count_type_bug = sum(1 for t in all_visible if t.section == "Bugs" or t.status.replace(" ", "-").lower() in ("bug", "bug-fixed"))
    count_size_s = sum(1 for t in all_visible if t.complexity == "S")
    count_size_m = sum(1 for t in all_visible if t.complexity == "M")
    count_size_l = sum(1 for t in all_visible if t.complexity == "L")

    # Kitchen filter counts (M2)
    count_auto = sum(1 for t in all_visible if t.automation_mode == "auto")
    count_held = sum(1 for t in all_visible if t.automation_mode == "held")
    count_eligible = sum(1 for t in all_visible if t.automation_eligible)
    count_needs_input = sum(1 for t in all_visible if t.latest_run_status == "needs_input")
    count_failed = sum(1 for t in all_visible if t.latest_run_status in ("failed", "stalled"))

    # Progress: done items / (done + remaining)
    total_all = count_total + count_wontdo + count_icebox
    progress_pct = round((count_done / total_all * 100)) if total_all > 0 else 0

    # Compute dependency state
    dep_state = compute_dependency_state(all_tickets)

    # Build card HTML
    backlog_cards = _render_cards(by_section["Backlog"], "backlog", child_tickets, dep_state)
    wip_cards = _render_cards(by_section["WIP"], "wip", child_tickets, dep_state)
    ideas_cards = _render_cards(by_section["Ideas"], "ideas", child_tickets, dep_state)
    # Bottom list sections: newest first (reverse insertion order)
    wontdo_cards = _render_list_rows(list(reversed(by_section["Won't Do"])), "wontdo", child_tickets, dep_state)
    review_cards = _render_cards(by_section["For Review"], "review", child_tickets, dep_state)
    done_cards = _render_list_rows(list(reversed(by_section["Done"])), "done", child_tickets, dep_state)
    icebox_cards = _render_list_rows(list(reversed(by_section["Icebox"])), "icebox", child_tickets, dep_state)
    bugs_cards = _render_list_rows(list(reversed(by_section["Bugs"])), "bugs", child_tickets, dep_state)

    releases_text = f"{cs.releases} releases" if cs.releases != 1 else "1 release"

    # Pre-computed SVG icons for use inside the HTML f-string
    _icon_settings = _svg_icon("settings", 14)
    _icon_journeys = _svg_icon("route", 14)
    _icon_close = _svg_icon("x", 14)
    _icon_open = _svg_icon("arrow-up-right", 12)
    _dctrs_icons = ''.join([
        f'<button class="readiness-dot" data-flag="description" title="Description" aria-label="Description">{_svg_icon("file-text", 12)}</button>',
        f'<button class="readiness-dot" data-flag="criteria" title="Criteria" aria-label="Criteria">{_svg_icon("check-square", 12)}</button>',
        f'<button class="readiness-dot" data-flag="smoke" title="Smoke" aria-label="Smoke">{_svg_icon("flame", 12)}</button>',
        f'<button class="readiness-dot" data-flag="tests" title="Tests" aria-label="Tests">{_svg_icon("flask-conical", 12)}</button>',
        f'<button class="readiness-dot" data-flag="reviewed" title="Learnings" aria-label="Learnings">{_svg_icon("eye", 12)}</button>',
    ])

    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="gen-ts" content="{gen_ts}">
<meta name="schema-version" content="2">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0%25' stop-color='%233b82f6'/%3E%3Cstop offset='100%25' stop-color='%238b5cf6'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect x='3' y='2' width='26' height='28' rx='4' fill='url(%23g)'/%3E%3Ccircle cx='3' cy='12' r='3.5' fill='%230a0a0b'/%3E%3Ccircle cx='29' cy='12' r='3.5' fill='%230a0a0b'/%3E%3Cline x1='6.5' y1='12' x2='25.5' y2='12' stroke='%230a0a0b' stroke-width='1' stroke-dasharray='2.5 2'/%3E%3Crect x='8' y='5' width='11' height='2.5' rx='1.2' fill='%23ffffffcc'/%3E%3Crect x='8' y='16' width='16' height='1.5' rx='.7' fill='%23ffffff55'/%3E%3Crect x='8' y='19.5' width='12' height='1.5' rx='.7' fill='%23ffffff33'/%3E%3Crect x='8' y='23' width='14' height='1.5' rx='.7' fill='%23ffffff22'/%3E%3C/svg%3E">
<title>Ticket Takeaway — {escape(project_short)}</title>
<script>
(function(){{
  var s=localStorage.getItem('tt-theme');
  if(s==='light')document.documentElement.setAttribute('data-theme','light');
  else if(s==='dark')document.documentElement.setAttribute('data-theme','dark');
  else document.documentElement.setAttribute('data-theme',
    window.matchMedia('(prefers-color-scheme:light)').matches?'light':'dark');
}})();
</script>
<style>
:root, [data-theme="dark"] {{
  --bg-page: #0c0c0e; --bg-surface: #151518; --bg-card: #1b1b20; --bg-hover: #232329;
  --border-subtle: #1f1f26; --border-default: #2c2c35; --border-strong: #3c3c47;
  --text-primary: #eaeaed; --text-secondary: #9e9eab; --text-tertiary: #6a6a76;
  --accent: #3b82f6;
  --status-backlog: #6b7280; --status-wip: #3b82f6; --status-review: #f59e0b;
  --status-done: #22c55e; --status-idea: #8b5cf6; --status-wontdo: #4b5563;
  --status-icebox: #94a3b8; --status-icebox-bg: #94a3b815;
  --priority-high: #ef4444; --priority-medium: #f59e0b; --priority-low: #3b82f6;
  --status-backlog-bg: #6b728015; --status-wip-bg: #3b82f615; --status-review-bg: #f59e0b15;
  --status-done-bg: #22c55e15; --status-idea-bg: #8b5cf615;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-mono: ui-monospace, "Cascadia Code", "Source Code Pro", Menlo, Consolas, monospace;
}}
[data-theme="light"] {{
  --bg-page: #f8f9fa; --bg-surface: #ffffff; --bg-card: #ffffff; --bg-hover: #f3f4f6;
  --border-subtle: #e5e7eb; --border-default: #d1d5db; --border-strong: #9ca3af;
  --text-primary: #111827; --text-secondary: #6b7280; --text-tertiary: #9ca3af;
  --accent: #2563eb;
  --status-backlog: #6b7280; --status-wip: #2563eb; --status-review: #d97706;
  --status-done: #059669; --status-idea: #7c3aed; --status-wontdo: #4b5563;
  --status-icebox: #6b7280; --status-icebox-bg: rgba(107,114,128,0.08);
  --priority-high: #dc2626; --priority-medium: #d97706; --priority-low: #2563eb;
  --status-backlog-bg: rgba(107,114,128,0.08); --status-wip-bg: rgba(37,99,235,0.08);
  --status-review-bg: rgba(217,119,6,0.08); --status-done-bg: rgba(5,150,105,0.08);
  --status-idea-bg: rgba(124,58,237,0.08);
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
body {{ background: var(--bg-page); color: var(--text-primary); font-family: var(--font-sans); font-size: 13px; line-height: 1.4; }}
a {{ color: var(--accent); text-decoration: none; }}

/* Header */
.header-block {{
  padding: 16px 20px 12px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-surface);
}}
.header-row1 {{
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 6px;
}}
.header-title {{ font-size: 16px; font-weight: 700; letter-spacing: -0.3px; }}
.header-date {{ font-size: 11px; color: var(--text-tertiary); }}
.header-stats {{ display: flex; gap: 10px; margin-left: auto; }}
.header-stat {{
  font-size: 11px; color: var(--text-secondary); background: var(--bg-card);
  padding: 2px 8px; border-radius: 4px; border: 1px solid var(--border-subtle);
}}
.header-stat strong {{ color: var(--text-primary); font-weight: 600; }}

.header-row2 {{
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
}}
.project-name {{ font-size: 13px; font-weight: 600; color: var(--text-primary); }}
.proj-switcher {{ position: relative; display: inline-flex; align-items: center; }}
.proj-switcher-btn {{
  display: inline-flex; align-items: center; gap: 5px; font-size: 13px; font-weight: 600;
  color: var(--text-primary); background: none; border: none; padding: 2px 4px;
  border-radius: 4px; cursor: pointer; transition: background 0.15s;
  font-family: var(--font-sans); line-height: 1.4;
}}
.proj-switcher-btn:hover, .proj-switcher-btn[aria-expanded="true"] {{ background: var(--bg-hover); }}
.proj-switcher-btn:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.proj-switcher-chevron {{ width: 10px; height: 10px; opacity: 0.5; flex-shrink: 0; transition: transform 0.15s; }}
.proj-switcher-btn[aria-expanded="true"] .proj-switcher-chevron {{ transform: rotate(180deg); }}
.proj-switcher-menu {{
  position: absolute; top: calc(100% + 6px); left: 0; z-index: 500; min-width: 200px;
  background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.5); padding: 4px 0; display: none;
}}
.proj-switcher-menu.open {{ display: block; }}
.proj-switcher-item {{
  display: block; padding: 7px 14px; font-size: 13px; color: var(--text-primary);
  text-decoration: none; white-space: nowrap; cursor: pointer; transition: background 0.1s;
}}
.proj-switcher-item:hover, .proj-switcher-item:focus-visible {{ background: var(--bg-hover); outline: none; }}
.proj-switcher-item.current {{ color: var(--accent); font-weight: 600; }}
.proj-switcher-item.current::after {{ content: " \\2713"; font-size: 11px; }}
.proj-switcher-divider {{ height: 1px; background: var(--border-subtle); margin: 4px 0; }}
.proj-switcher-footer-item {{
  display: block; padding: 6px 14px; font-size: 11px; color: var(--text-tertiary);
  text-decoration: none; white-space: nowrap; transition: background 0.1s, color 0.1s;
}}
.proj-switcher-footer-item:hover, .proj-switcher-footer-item:focus-visible {{ background: var(--bg-hover); color: var(--text-secondary); outline: none; }}
.version-badge {{
  font-size: 10px; font-weight: 600; background: var(--bg-card); color: var(--text-secondary);
  padding: 1px 6px; border-radius: 4px; border: 1px solid var(--border-default);
  font-family: var(--font-mono);
}}
.progress-bar-wrap {{
  display: flex; align-items: center; gap: 6px;
}}
.progress-bar {{
  width: 80px; height: 6px; background: var(--bg-card); border-radius: 3px; overflow: hidden;
  border: 1px solid var(--border-subtle);
}}
.progress-fill {{ height: 100%; background: var(--status-done); border-radius: 3px; transition: width 0.3s; }}
.progress-pct {{ font-size: 11px; font-weight: 600; color: var(--status-done); font-family: var(--font-mono); }}

.release-pills {{ display: flex; gap: 4px; flex-wrap: wrap; }}
.release-pill {{
  font-size: 9px; padding: 1px 5px; border-radius: 3px; font-weight: 500;
  background: var(--status-done-bg); color: var(--status-done); font-family: var(--font-mono);
}}

.sparkline-wrap {{
  display: flex; align-items: flex-end; gap: 2px; height: 20px; margin-left: 8px;
}}
.spark-bar {{
  width: 6px; background: var(--accent); border-radius: 1px 1px 0 0; opacity: 0.7;
  min-height: 1px;
}}

.code-stats {{
  display: flex; gap: 10px; margin-left: auto;
}}
.code-stat {{ font-size: 10px; color: var(--text-tertiary); font-family: var(--font-mono); }}
.code-stat strong {{ color: var(--text-secondary); }}

/* Filter bar */
.filter-bar {{
  position: sticky; top: 0; z-index: 100;
  display: flex; align-items: center; gap: 6px; padding: 8px 20px;
  background: var(--bg-surface); border-bottom: 1px solid var(--border-default);
  backdrop-filter: blur(12px);
}}
.filter-btn {{
  font-size: 11px; padding: 4px 10px; border-radius: 6px; border: 1px solid var(--border-default);
  background: var(--bg-card); color: var(--text-secondary); cursor: pointer;
  font-weight: 500; transition: all 0.15s; font-family: var(--font-sans);
}}
.filter-btn:hover {{ border-color: var(--border-strong); color: var(--text-primary); }}
.filter-btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.filter-btn .count {{ font-size: 10px; opacity: 0.7; margin-left: 3px; font-family: var(--font-mono); }}
.filter-group {{ display: inline-flex; gap: 4px; align-items: center; }}
.filter-divider {{ width: 1px; height: 18px; background: var(--border-default); margin: 0 4px; opacity: 0.5; }}
.search-input {{
  margin-left: auto; font-size: 12px; padding: 4px 10px; border-radius: 6px;
  border: 1px solid var(--border-default); background: var(--bg-card); color: var(--text-primary);
  width: 180px; font-family: var(--font-sans); outline: none;
}}
.search-input::placeholder {{ color: var(--text-tertiary); }}
.search-input:focus {{ border-color: var(--accent); }}

/* Kanban */
.kanban {{
  display: flex; gap: 12px; padding: 16px 20px; overflow-x: auto;
  align-items: stretch;
}}
.column {{
  flex: 0 0 280px; min-width: 280px; background: var(--bg-surface);
  border-radius: 8px; border: 1px solid var(--border-subtle);
  display: flex; flex-direction: column; max-height: calc(100vh - 100px);
}}
.column.hidden {{ display: none; }}
.column-header, .bottom-section-header {{
  position: relative;
}}
.column-header {{
  display: flex; align-items: center; gap: 8px; padding: 10px 12px;
  border-bottom: 1px solid var(--border-subtle); position: sticky; top: 0;
  background: var(--bg-surface); border-radius: 8px 8px 0 0; z-index: 1;
}}
.column-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.column-name {{ font-size: 12px; font-weight: 600; }}
.column-count {{
  font-size: 10px; color: var(--text-tertiary); font-family: var(--font-mono);
  margin-left: auto; background: var(--bg-card); padding: 1px 6px; border-radius: 8px;
}}
.column-body {{
  padding: 8px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 6px;
}}
.column-body::-webkit-scrollbar, .detail-body::-webkit-scrollbar, .bottom-section-body::-webkit-scrollbar {{ width: 4px; }}
.column-body::-webkit-scrollbar-thumb, .detail-body::-webkit-scrollbar-thumb, .bottom-section-body::-webkit-scrollbar-thumb {{ background: var(--border-default); border-radius: 2px; }}

/* Cards */
.card {{
  background: var(--bg-card); border: 1px solid var(--border-subtle);
  border-radius: 6px; padding: 8px 10px; cursor: default;
  transition: background 0.15s, border-color 0.15s; position: relative;
  user-select: none;
}}
.card:hover {{ background: var(--bg-hover); border-color: var(--border-default); }}
.card.wip-card {{ border-left: 3px solid var(--status-wip); }}
.card.review-card {{ border-left: 3px solid var(--status-review); background: rgba(245,158,11,0.03); }}
.card.idea-card {{ border-left: 3px solid var(--status-idea); }}
.card.backlog-card {{ border-left: 3px solid var(--status-backlog); }}
.card.done-card {{ border-left: 3px solid var(--status-done); }}
.card.icebox-card {{ border-left: 3px solid var(--status-icebox); }}
.card.bug-card {{ border-left: 3px solid var(--priority-high); }}

.card-top {{ display: flex; align-items: flex-start; gap: 6px; margin-bottom: 4px; }}
.priority-dot {{
  width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; margin-top: 4px;
}}
.priority-dot.high {{ background: var(--priority-high); }}
.priority-dot.medium {{ background: var(--priority-medium); }}
.priority-dot.low {{ background: var(--priority-low); }}
.priority-dot.high {{ animation: pulse 2s ease-in-out infinite; }}
@keyframes pulse {{
  0%, 100% {{ opacity: 1; box-shadow: 0 0 0 0 rgba(239,68,68,0.4); }}
  50% {{ opacity: 0.7; box-shadow: 0 0 0 4px rgba(239,68,68,0); }}
}}
.card-title {{ font-size: 12px; font-weight: 600; line-height: 1.3; color: var(--text-primary); }}

.card-meta {{ display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }}
.card-id {{ font-size: 10px; color: var(--accent); opacity: 0.6; font-family: var(--font-mono); font-weight: 600; flex-shrink: 0; }}

/* Kitchen badge — small status dot indicating automation state. M1a only renders
   it when there's something meaningful (auto, held, or an actual run); manual
   tickets get no badge so the kanban stays clean. */
.kitchen-badge {{
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent); opacity: 0.6; flex-shrink: 0;
    margin-left: 2px;
}}
.kitchen-badge.kb-idle    {{ background: #94a3b8; opacity: 0.55; }}
.kitchen-badge.kb-held    {{ background: #f59e0b; opacity: 0.85; }}
.kitchen-badge.kb-queued,
.kitchen-badge.kb-running {{ background: #3b82f6; opacity: 0.95; box-shadow: 0 0 0 2px rgba(59,130,246,0.18); }}
.kitchen-badge.kb-needs-input {{ background: #f59e0b; opacity: 1; box-shadow: 0 0 0 2px rgba(245,158,11,0.22); }}
.kitchen-badge.kb-failed   {{ background: #ef4444; opacity: 1; }}
.kitchen-badge.kb-cancelled {{ background: #6b7280; opacity: 0.7; }}
@keyframes kitchen-pulse {{ 0%, 100% {{ opacity: 0.95; }} 50% {{ opacity: 0.55; }} }}
.kitchen-badge.kb-running, .kitchen-badge.kb-queued {{ animation: kitchen-pulse 1.6s ease-in-out infinite; }}


.status-badge {{ font-size: 9px; padding: 1px 6px; border-radius: 10px; font-weight: 600; text-transform: uppercase; }}
.status-badge.proposed {{ background: var(--status-backlog-bg); color: var(--status-backlog); }}
.status-badge.specified {{ background: rgba(99,102,241,0.12); color: #818cf8; }}
.status-badge.ready {{ background: rgba(34,197,94,0.12); color: var(--status-done); }}
.status-badge.in-progress {{ background: var(--status-wip-bg); color: var(--status-wip); }}
.status-badge.blocked {{ background: rgba(239,68,68,0.12); color: var(--priority-high); }}
.status-badge.for-review {{ background: var(--status-review-bg); color: var(--status-review); }}
.status-badge.released {{ background: var(--status-done-bg); color: var(--status-done); }}
.status-badge.done {{ background: var(--status-done-bg); color: var(--status-done); }}
.status-badge.rework {{ background: rgba(239,68,68,0.12); color: var(--priority-high); }}
.status-badge.icebox {{ background: var(--status-icebox-bg); color: var(--status-icebox); }}
.status-badge.bug {{ background: rgba(239,68,68,0.12); color: var(--priority-high); }}
.status-badge.bug-fixed {{ background: rgba(34,197,94,0.12); color: var(--status-done); }}
.status-badge.wont-do {{ background: rgba(75,85,99,0.15); color: var(--status-wontdo); }}
.status-badge.wontdo {{ background: rgba(75,85,99,0.15); color: var(--status-wontdo); }}

.card-open-btn {{
  background: none; border: none; color: var(--text-tertiary); cursor: pointer;
  font-size: 14px; padding: 2px 4px; line-height: 1; opacity: 0.6;
  transition: all 0.15s; border-radius: 4px;
}}
.card-open-btn:hover {{ opacity: 1; color: var(--accent); background: var(--bg-hover); }}
.card-record-btn {{
  background: none; border: none; color: var(--text-tertiary); cursor: pointer;
  font-size: 12px; padding: 1px 3px; line-height: 1; opacity: 0.4;
  transition: all 0.15s; border-radius: 4px;
}}
.card-record-btn:hover {{ opacity: 1; color: #22c55e; background: rgba(34,197,94,0.1); }}
.card-record-btn svg {{ fill: none; stroke: currentColor; stroke-width: 2; }}

.card-desc {{ font-size: 11px; color: var(--text-secondary); line-height: 1.3; margin-top: 6px; display: none; }}
.card-criteria {{ font-size: 11px; color: var(--text-tertiary); line-height: 1.4; margin-top: 4px; display: none; }}
.card-criteria .criterion {{ margin: 2px 0; }}
.card-criteria .criterion.checked {{ color: var(--status-done); text-decoration: line-through; opacity: 0.7; }}
.card.expanded .card-desc,
.card.expanded .card-criteria {{ display: block; }}
.card-footer {{ display: flex; align-items: center; gap: 6px; }}
.complexity-badge {{
  font-size: 9px; padding: 1px 5px; border-radius: 3px; font-weight: 600;
  background: var(--bg-page); color: var(--text-tertiary); border: 1px solid var(--border-subtle);
  font-family: var(--font-mono);
}}
.child-count-badge {{
  font-size: 9px; padding: 1px 6px; border-radius: 10px;
  background: rgba(99,102,241,0.12); color: #818cf8;
  font-weight: 600; margin-left: auto;
}}
.child-count-badge.has-bugs {{
  background: rgba(239,68,68,0.12); color: var(--priority-high);
}}
.card-parent-link {{
  font-size: 9px; color: var(--text-tertiary); font-family: var(--font-mono);
  margin-bottom: 4px;
}}
.card-deps {{
  font-size: 9px; color: var(--text-tertiary); font-family: var(--font-mono);
  margin-bottom: 4px;
}}
.card-blocked-badge {{
  font-size: 9px; padding: 1px 6px; border-radius: 10px;
  background: rgba(251,146,60,0.15); color: #fb923c;
  font-weight: 600; display: inline-block; margin-top: 2px;
}}
.card.blocked {{ opacity: 0.7; border-left: 3px solid #fb923c; }}
/* Child card groups — parent + indented children with connector */
.child-group {{
  display: flex; flex-direction: column; gap: 4px;
  margin-left: 8px; padding-left: 10px;
  border-left: 1px solid var(--border-default);
}}
.child-group .card {{ margin-left: 0; position: relative; }}
.child-group .card::before {{
  content: ''; position: absolute; left: -11px; top: 12px;
  width: 6px; border-top: 1px solid var(--border-default);
}}
.child-group.collapsed {{ display: none; }}
/* Parent toggle */
.children-toggle {{
  font-size: 9px; color: var(--text-tertiary); cursor: pointer; margin-left: auto;
  padding: 1px 5px; border-radius: 3px; user-select: none;
}}
.children-toggle:hover {{ color: var(--accent); background: rgba(59,130,246,0.08); }}
.children-toggle .arrow {{ display: inline-block; transition: transform 0.15s; }}
.children-toggle.collapsed .arrow {{ transform: rotate(-90deg); }}

/* Bug section below kanban */
/* Collapsible bottom sections (Done, Won't Do, Bugs) */
.bottom-section {{
  margin: 8px 20px; background: var(--bg-surface); border-radius: 8px;
  border: 1px solid var(--border-subtle); overflow: hidden;
}}
.bottom-section-header {{
  display: flex; align-items: center; gap: 8px; padding: 10px 12px;
  font-size: 12px; font-weight: 600; cursor: pointer; user-select: none;
  border-bottom: 1px solid var(--border-subtle);
}}
.bottom-section-header:hover {{ background: var(--bg-hover); }}
.bottom-section-header .toggle-arrow {{ font-size: 10px; color: var(--text-tertiary); transition: transform 0.2s; }}
.bottom-section.expanded .bottom-section-header .toggle-arrow {{ transform: rotate(90deg); }}
.bottom-section-header .section-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.bottom-section-title {{ font-size: 13px; font-weight: 600; color: var(--text-primary); }}
.bottom-section-count {{
  font-size: 10px; color: var(--text-tertiary); font-family: var(--font-mono);
  background: var(--bg-card); padding: 1px 6px; border-radius: 8px;
}}
.bottom-section-body {{
  padding: 4px 8px;
}}
.bottom-section:not(.expanded) .bottom-section-body {{ display: none; }}
.bottom-section.expanded .bottom-section-body {{
  display: flex; flex-direction: column; gap: 1px;
}}

/* List rows — compact single-line items for bottom sections */
.list-row {{
  display: flex; flex-direction: column;
  background: var(--bg-card); border: 1px solid var(--border-subtle);
  border-radius: 6px; margin: 3px 8px;
  transition: background 0.15s, border-color 0.15s;
}}
.list-row:hover {{ background: var(--bg-hover); border-color: var(--border-default); }}
.list-row-main {{
  display: flex; align-items: center; gap: 8px; padding: 6px 10px;
}}
.list-row[data-section="bugs"] {{ border-left: 3px solid var(--priority-high); }}
.list-row[data-section="done"] {{ border-left: 3px solid var(--status-done); }}
.list-row[data-section="icebox"] {{ border-left: 3px solid var(--status-icebox); }}
.list-row[data-section="wontdo"] {{ border-left: 3px solid var(--status-wontdo); }}
.list-row-main .priority-dot {{ margin-top: 0; }}
.list-row-main .card-id {{ min-width: 50px; }}
.list-row-main .card-title {{ font-size: 11px; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.list-row-main .complexity-badge {{ font-size: 8px; padding: 0 4px; }}
.commit-badge {{
  font-family: var(--font-mono); font-size: 9px; color: var(--text-tertiary);
  background: var(--bg-hover); padding: 0 4px; border-radius: 3px;
}}
.release-badge {{
  font-size: 9px; color: var(--status-done); background: var(--status-done-bg);
  padding: 0 4px; border-radius: 3px; font-weight: 600;
}}
.list-row-main .commit-badge, .list-row-main .release-badge {{ font-size: 8px; }}
.list-row .readiness-row {{ padding: 2px 10px 6px; display: flex; gap: 3px; }}
.list-row .readiness-dot {{ width: 14px; height: 14px; }}
.list-row .readiness-dot svg {{ width: 10px; height: 10px; }}
/* Quick-edit cursors (active only when edit-api meta tag is present) */
.edit-enabled .priority-dot {{ cursor: pointer; }}
.edit-enabled .status-badge {{ cursor: pointer; }}
.edit-enabled .criterion {{ cursor: pointer; }}
.edit-enabled .complexity-badge {{ cursor: pointer; }}
.edit-enabled .priority-dot:hover {{ transform: scale(1.5); transition: transform 0.15s; }}
.edit-enabled .status-badge:hover {{ filter: brightness(1.3); transition: filter 0.15s; }}
.edit-enabled .complexity-badge:hover {{ filter: brightness(1.3); transition: filter 0.15s; }}
/* Click-to-edit text fields */
.edit-enabled .card-title {{ cursor: text; }}
.edit-enabled .card.expanded .card-desc {{ cursor: text; }}
.edit-enabled .card-title:hover,
.edit-enabled .card.expanded .card-desc:hover {{ background: var(--bg-hover); border-radius: 3px; }}
/* Empty field placeholders (only visible on expanded cards) */
.card-parent-link.empty, .card-deps.empty, .card-desc.empty {{
  display: none; color: var(--text-tertiary); font-size: 10px; cursor: pointer;
  opacity: 0.5; font-style: italic;
}}
.card.expanded .card-parent-link.empty,
.card.expanded .card-deps.empty,
.card.expanded .card-desc.empty {{ display: block; }}
.edit-enabled .card-parent-link.empty:hover,
.edit-enabled .card-deps.empty:hover,
.edit-enabled .card-desc.empty:hover {{ opacity: 1; background: var(--bg-hover); border-radius: 3px; }}
.edit-enabled .card-parent-link {{ cursor: pointer; }}
.edit-enabled .card-deps {{ cursor: pointer; }}
/* Add criterion button */
.add-criterion-btn {{
  display: none; font-size: 10px; color: var(--accent); background: none;
  border: 1px dashed var(--border-default); border-radius: 4px; padding: 2px 8px;
  cursor: pointer; margin-top: 4px;
}}
.edit-enabled .card.expanded .add-criterion-btn {{ display: inline-block; }}
.add-criterion-btn:hover {{ border-color: var(--accent); background: var(--bg-hover); }}
/* Git traceability on expanded cards */
.card-commit, .card-release {{ display: none; margin-top: 4px; }}
.card.expanded .card-commit, .card.expanded .card-release {{ display: block; }}
/* Unified toast system */
#app-toast {{
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(20px);
  background: var(--bg-card); border: 1px solid var(--border-default);
  border-left: 3px solid var(--status-done); border-radius: 8px;
  padding: 10px 16px; z-index: 9999;
  box-shadow: 0 4px 20px rgba(0,0,0,0.5); font-size: 12px; color: var(--text-secondary);
  opacity: 0; transition: opacity 0.2s, transform 0.2s;
  pointer-events: none; max-width: 500px; display: flex; align-items: center; gap: 8px;
}}
#app-toast.visible {{ opacity: 1; transform: translateX(-50%) translateY(0); pointer-events: auto; }}
#app-toast.toast-error {{ border-left-color: var(--priority-high); }}
#app-toast.toast-undo {{ border-left-color: var(--border-strong); }}
#app-toast .toast-undo-btn {{
  color: var(--accent); cursor: pointer; font-weight: 600; margin-left: 4px;
  background: none; border: none; font-size: 12px; font-family: var(--font-sans);
  text-decoration: underline; padding: 0;
}}

/* Drag-drop (edit mode) */
.edit-enabled .card {{ cursor: grab; }}
.edit-enabled .card:active {{ cursor: grabbing; }}
.card.dragging {{ opacity: 0.4; }}
.card.drag-target {{ border-color: var(--accent) !important; box-shadow: 0 0 0 1px var(--accent), 0 0 8px rgba(59,130,246,0.2); }}
.column.drag-over {{ background: rgba(59,130,246,0.06); border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent), 0 0 12px rgba(59,130,246,0.15); }}
.column.drag-over .column-header {{ background: rgba(59,130,246,0.10); }}
.bottom-section.drag-over {{ background: rgba(59,130,246,0.06); border-color: var(--accent); }}

/* Workflow action buttons (edit mode) */
.card-actions {{ display: none; gap: 4px; margin-top: 6px; }}
.edit-enabled .card.expanded .card-actions {{ display: flex; }}
.action-btn {{
  font-size: 9px; padding: 2px 8px; border-radius: 4px; border: 1px solid var(--border-default);
  background: var(--bg-page); color: var(--text-secondary); cursor: pointer; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.3px;
}}
.action-btn:hover {{ background: var(--bg-hover); border-color: var(--accent); color: var(--accent); }}
.action-btn.primary {{ background: rgba(59,130,246,0.12); color: var(--accent); border-color: var(--accent); }}
.action-btn.primary:hover {{ background: rgba(59,130,246,0.2); }}
.action-btn.danger {{ color: var(--priority-high); border-color: var(--priority-high); }}
.action-btn.danger:hover {{ background: rgba(239,68,68,0.1); }}

/* Gate-check: card pulsing state */
@keyframes gatePulse {{
  0%, 100% {{ box-shadow: 0 0 0 0 rgba(59,130,246,0.3); }}
  50% {{ box-shadow: 0 0 0 6px rgba(59,130,246,0); }}
}}
.card.gate-checking {{
  animation: gatePulse 1.5s ease-in-out infinite;
  border-color: var(--accent);
}}
.card.gate-checking .card-actions {{ display: none !important; }}

/* Gate-check panel */
/* Gate banner (shown inside detail overlay during column moves) */
.detail-gate-banner {{
  padding: 12px 16px; margin-bottom: 12px; border-radius: 8px;
  background: var(--bg-card); border: 1px solid var(--border-default);
  animation: panelSlide 0.2s ease-out;
}}
.detail-gate-banner.hidden {{ display: none; }}
.detail-gate-verdict {{
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
}}
.gate-verdict-badge {{
  font-size: 10px; font-weight: 700; padding: 3px 10px; border-radius: 10px;
  text-transform: uppercase; letter-spacing: 0.5px;
}}
.gate-verdict-badge.ready {{ background: rgba(34,197,94,0.15); color: #22c55e; }}
.gate-verdict-badge.needs-work {{ background: rgba(234,179,8,0.15); color: #eab308; }}
.gate-verdict-badge.blocked {{ background: rgba(239,68,68,0.15); color: #ef4444; }}
.gate-verdict-badge.loading {{ background: var(--bg-hover); color: var(--text-tertiary); animation: assess-spin 1.5s ease-in-out infinite; }}
.detail-gate-confirm:disabled {{ opacity: 0.4; cursor: not-allowed; }}
.detail-gate-summary {{ color: var(--text-secondary); font-size: 13px; }}
.detail-gate-actions {{
  display: flex; gap: 8px; margin-top: 8px;
}}
.detail-gate-confirm {{
  font-size: 12px; padding: 6px 18px; border-radius: 6px; border: none;
  background: var(--accent); color: #fff; cursor: pointer; font-weight: 600;
  font-family: var(--font-sans); transition: background 0.15s;
}}
.detail-gate-confirm:hover {{ background: #2563eb; }}
.detail-gate-cancel {{
  font-size: 12px; padding: 6px 18px; border-radius: 6px;
  border: 1px solid var(--border-default); background: none;
  color: var(--text-secondary); cursor: pointer; font-family: var(--font-sans);
  transition: all 0.15s;
}}
.detail-gate-cancel:hover {{ color: var(--text-primary); border-color: var(--text-secondary); }}

/* Diff panel (AI enrich round-trip) */
.diff-panel {{
  margin-bottom: 12px; border-radius: 8px; border: 1px solid var(--border-default);
  background: var(--bg-card); overflow: hidden; animation: panelSlide 0.2s ease-out;
}}
.diff-panel.hidden {{ display: none; }}
.diff-header {{
  display: flex; align-items: center; gap: 8px; padding: 10px 12px;
  background: rgba(59,130,246,0.06); border-bottom: 1px solid var(--border-default);
}}
.diff-header span {{ flex: 1; font-size: 12px; font-weight: 600; color: var(--text-secondary); }}
.diff-accept-all, .diff-reject-all {{
  font-size: 11px; padding: 3px 10px; border-radius: 5px; cursor: pointer;
  border: 1px solid var(--border-default); background: none; font-family: var(--font-sans);
  transition: all 0.15s;
}}
.diff-accept-all {{ color: #22c55e; border-color: rgba(34,197,94,0.4); }}
.diff-accept-all:hover {{ background: rgba(34,197,94,0.1); }}
.diff-reject-all {{ color: #ef4444; border-color: rgba(239,68,68,0.4); }}
.diff-reject-all:hover {{ background: rgba(239,68,68,0.1); }}
.diff-hunks {{ padding: 8px 0; max-height: 320px; overflow-y: auto; }}
.diff-hunk {{
  padding: 4px 12px; display: flex; align-items: flex-start; gap: 8px;
  border-bottom: 1px solid rgba(255,255,255,0.04); font-family: var(--font-mono); font-size: 12px;
  transition: background 0.15s;
}}
.diff-hunk:last-child {{ border-bottom: none; }}
.diff-hunk.accepted {{ background: rgba(34,197,94,0.06); }}
.diff-hunk.rejected {{ background: rgba(239,68,68,0.04); opacity: 0.6; }}
.diff-hunk-lines {{ flex: 1; min-width: 0; }}
.diff-hunk-old {{
  color: #ef4444; background: rgba(239,68,68,0.08); padding: 2px 6px; border-radius: 3px;
  margin-bottom: 2px; white-space: pre-wrap; word-break: break-all; line-height: 1.4;
}}
.diff-hunk-old:empty {{ display: none; }}
.diff-hunk-new {{
  color: #22c55e; background: rgba(34,197,94,0.08); padding: 2px 6px; border-radius: 3px;
  white-space: pre-wrap; word-break: break-all; line-height: 1.4;
}}
.diff-hunk-new:empty {{ display: none; }}
.diff-hunk-new[contenteditable="true"] {{ cursor: text; outline: none; }}
.diff-hunk-new[contenteditable="true"]:focus {{ background: rgba(34,197,94,0.15); border-radius: 3px; }}
.diff-hunk-actions {{ display: flex; gap: 4px; flex-shrink: 0; padding-top: 2px; }}
.diff-accept, .diff-reject {{
  width: 22px; height: 22px; border-radius: 4px; border: 1px solid var(--border-default);
  background: none; cursor: pointer; font-size: 12px; display: flex; align-items: center;
  justify-content: center; transition: all 0.15s; padding: 0; line-height: 1;
}}
.diff-accept {{ color: #22c55e; }}
.diff-accept:hover, .diff-hunk.accepted .diff-accept {{ background: rgba(34,197,94,0.15); border-color: rgba(34,197,94,0.5); }}
.diff-reject {{ color: #ef4444; }}
.diff-reject:hover, .diff-hunk.rejected .diff-reject {{ background: rgba(239,68,68,0.15); border-color: rgba(239,68,68,0.5); }}
.diff-footer {{
  padding: 8px 12px; border-top: 1px solid var(--border-default);
  display: flex; gap: 8px; align-items: center;
}}
.diff-apply {{
  font-size: 12px; padding: 6px 16px; border-radius: 6px; border: none;
  background: var(--accent); color: #fff; cursor: pointer; font-weight: 600;
  font-family: var(--font-sans); transition: background 0.15s;
}}
.diff-apply:hover {{ background: #2563eb; }}
.diff-apply:disabled {{ background: var(--border-default); color: var(--text-tertiary); cursor: not-allowed; }}
.diff-discard {{
  font-size: 12px; padding: 6px 14px; border-radius: 6px;
  border: 1px solid var(--border-default); background: none;
  color: var(--text-secondary); cursor: pointer; font-family: var(--font-sans); transition: all 0.15s;
}}
.diff-discard:hover {{ color: var(--text-primary); border-color: var(--text-secondary); }}
.diff-status {{ font-size: 11px; color: var(--text-tertiary); flex: 1; }}

/* (Properties moved to meta-strip chips) */

/* Assessment results area */
.detail-assessment {{
  margin-bottom: 12px; padding: 10px 12px; border-radius: 6px;
  background: var(--bg-card); border-left: 3px solid var(--border-default);
  animation: panelSlide 0.2s ease-out;
}}
.detail-assessment.hidden {{ display: none; }}
.detail-assessment.ok {{ border-left-color: #22c55e; }}
.detail-assessment.needs-work {{ border-left-color: #eab308; }}
.assessment-header {{
  display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;
}}
.assessment-status {{
  font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 8px;
  text-transform: uppercase; letter-spacing: 0.3px;
}}
.assessment-status.ok {{ background: rgba(34,197,94,0.12); color: #22c55e; }}
.assessment-status.needs-work {{ background: rgba(234,179,8,0.12); color: #eab308; }}
.assessment-dismiss {{
  background: none; border: none; color: var(--text-tertiary); cursor: pointer;
  font-size: 14px; padding: 0 2px; line-height: 1;
}}
.assessment-dismiss:hover {{ color: var(--text-primary); }}
.assessment-summary {{ font-size: 12px; color: var(--text-secondary); margin-bottom: 4px; }}
.assessment-suggestion {{
  font-size: 12px; color: var(--accent); font-style: italic;
  padding: 4px 8px; background: rgba(59,130,246,0.06); border-radius: 4px; margin-bottom: 6px;
}}
.assessment-add-criteria {{
  list-style: none; padding: 0; margin: 6px 0 0 0;
}}
.assessment-add-criteria li {{
  display: flex; align-items: flex-start; gap: 6px; padding: 3px 0;
  font-size: 12px; color: var(--text-secondary);
}}
.assessment-add-criteria button {{
  font-size: 10px; padding: 2px 8px; border-radius: 4px; border: 1px solid var(--accent);
  background: none; color: var(--accent); cursor: pointer; white-space: nowrap; flex-shrink: 0;
}}
.assessment-add-criteria button:hover {{ background: rgba(59,130,246,0.1); }}
.assessment-add-criteria button.added {{ color: #22c55e; border-color: #22c55e; pointer-events: none; }}
.assessment-apply-btn {{
  font-size: 11px; padding: 4px 12px; border-radius: 4px; border: 1px solid var(--accent);
  background: rgba(59,130,246,0.1); color: var(--accent); cursor: pointer;
  font-weight: 600; margin-top: 4px;
}}
.assessment-apply-btn:hover {{ background: rgba(59,130,246,0.2); }}
.assessment-action-row {{ margin-top: 8px; }}
.assessment-action-btn {{
  font-size: 11px; padding: 5px 12px; border-radius: 6px;
  border: 1px solid var(--border-default); background: var(--bg-hover);
  color: var(--text-secondary); cursor: pointer; font-weight: 600;
  transition: all 0.15s; display: inline-flex; align-items: center; gap: 4px;
}}
.assessment-action-btn:hover {{ border-color: var(--accent); color: var(--accent); background: rgba(59,130,246,0.08); }}

/* Assessment loading indicator */
.detail-assess-loading {{
  display: flex; align-items: center; gap: 8px; padding: 12px;
  color: var(--text-tertiary); font-size: 12px; font-style: italic;
}}
.detail-assess-loading.hidden {{ display: none; }}
.detail-assess-loading::before {{
  content: ''; width: 14px; height: 14px; border: 2px solid var(--border-default);
  border-top-color: var(--accent); border-radius: 50%;
  animation: assess-spin 0.6s linear infinite;
}}
@keyframes assess-spin {{ to {{ transform: rotate(360deg); }} }}
@keyframes sectionGlow {{
  0% {{ box-shadow: 0 0 0 0 rgba(59,130,246,0.4); }}
  50% {{ box-shadow: 0 0 12px 2px rgba(59,130,246,0.25); }}
  100% {{ box-shadow: 0 0 0 0 rgba(59,130,246,0); }}
}}
.detail-section.assess-complete {{
  animation: sectionGlow 1.2s ease-out;
  border-color: var(--accent);
  transition: border-color 1.2s ease-out;
}}

/* New ticket button + panel (edit mode) */
.new-ticket-btn {{
  display: none; font-size: 11px; padding: 4px 12px; border-radius: 6px;
  border: 1px solid var(--accent); background: rgba(59,130,246,0.12);
  color: var(--accent); cursor: pointer; font-weight: 600; font-family: var(--font-sans);
  transition: all 0.15s; margin-left: 6px;
}}
.new-ticket-btn:hover {{ background: rgba(59,130,246,0.25); }}
.edit-enabled .new-ticket-btn {{ display: inline-block; }}
.new-ticket-panel {{
  position: absolute; top: 100%; left: 0; right: 0; z-index: 99;
  background: var(--bg-surface); border-bottom: 1px solid var(--border-default);
  padding: 10px 20px 12px; animation: panelSlide 0.15s ease-out;
  box-shadow: 0 6px 16px rgba(0,0,0,0.4);
}}
@keyframes panelSlide {{ from {{ opacity: 0; transform: translateY(-6px); }} to {{ opacity: 1; transform: translateY(0); }} }}
.new-ticket-quick {{
  display: flex; align-items: center; gap: 8px;
}}
.new-ticket-input {{
  flex: 1; font-size: 13px; padding: 6px 12px; border-radius: 6px;
  border: 1px solid var(--border-default); background: var(--bg-card);
  color: var(--text-primary); font-family: var(--font-sans); outline: none;
  min-width: 0;
}}
.new-ticket-input::placeholder {{ color: var(--text-tertiary); }}
.new-ticket-input:focus {{ border-color: var(--accent); }}
.new-ticket-select {{
  font-size: 11px; padding: 6px 8px; border-radius: 6px;
  border: 1px solid var(--border-default); background: var(--bg-card);
  color: var(--text-secondary); font-family: var(--font-sans); outline: none;
  cursor: pointer;
}}
.new-ticket-select:focus {{ border-color: var(--accent); }}
.new-ticket-submit {{
  font-size: 11px; padding: 6px 16px; border-radius: 6px; border: none;
  background: var(--accent); color: #fff; cursor: pointer; font-weight: 600;
  font-family: var(--font-sans); transition: background 0.15s; white-space: nowrap;
}}
.new-ticket-submit:hover {{ background: #2563eb; }}
.new-ticket-submit:disabled {{ opacity: 0.5; cursor: not-allowed; }}

/* Inline editing (edit mode) */
.edit-enabled .card-title {{ cursor: text; }}
.edit-enabled .card-desc {{ cursor: text; }}
.card-title[contenteditable="true"] {{ outline: 1px solid var(--accent); border-radius: 2px; padding: 1px 3px; background: var(--bg-page); }}
.card-desc[contenteditable="true"] {{ outline: 1px solid var(--accent); border-radius: 2px; padding: 2px 4px; background: var(--bg-page); min-height: 2em; }}

/* Readiness indicator dots */
.readiness-row {{ display: flex; gap: 3px; margin: 3px 0; }}
.readiness-dot {{
  width: 18px; height: 18px; border-radius: 50%; font-size: 11px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-sans); line-height: 1; cursor: default;
}}
.readiness-dot.filled {{
  background: rgba(34,197,94,0.15); color: var(--status-done); border: 1px solid rgba(34,197,94,0.3);
}}
.readiness-dot.empty {{
  background: transparent; color: var(--text-tertiary); border: 1px solid var(--border-subtle);
  opacity: 0.5;
}}
.edit-enabled .readiness-dot {{ cursor: pointer; }}
.edit-enabled .readiness-dot:hover {{ opacity: 1; border-color: var(--accent); }}
.readiness-dot svg {{ width: 12px; height: 12px; flex-shrink: 0; }}
.action-btn svg {{ width: 12px; height: 12px; vertical-align: -2px; margin-right: 2px; }}
.settings-toggle svg, .detail-close svg, .settings-drawer-close svg {{ width: 14px; height: 14px; pointer-events: none; }}
.card-open-btn svg {{ width: 14px; height: 14px; }}

/* Detail overlay */
.detail-overlay {{ position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; }}
.detail-overlay.hidden {{ display: none; }}
.detail-backdrop {{ position: absolute; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px); }}
.detail-panel {{ position: relative; width: 92vw; max-width: 760px; max-height: 90vh; background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 12px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 24px 60px rgba(0,0,0,0.5); }}
/* Header strip — fixed */
.detail-header {{ display: flex; align-items: center; gap: 10px; padding: 14px 20px; border-bottom: 1px solid var(--border-subtle); }}
.detail-header .detail-id {{ font-family: var(--font-mono); font-size: 13px; color: var(--accent); font-weight: 700; flex-shrink: 0; }}
.detail-header .detail-title {{ font-size: 15px; font-weight: 600; color: var(--text-primary); flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.detail-header .detail-title[contenteditable] {{ cursor: text; border-bottom: 1px solid transparent; transition: border-color 0.15s; outline: none; }}
.detail-header .detail-title[contenteditable]:hover {{ border-bottom-color: var(--border-subtle); }}
.detail-header .detail-title[contenteditable]:focus {{ border-bottom-color: var(--accent); white-space: normal; overflow: visible; text-overflow: clip; }}
.detail-header .detail-path {{ font-family: var(--font-mono); font-size: 11px; color: #888; cursor: pointer; padding: 2px 6px; border-radius: 3px; white-space: nowrap; flex-shrink: 0; }}
.detail-header .detail-path:hover {{ background: rgba(255,255,255,0.1); }}
.detail-dctrs-strip {{ display: flex; gap: 4px; align-items: center; flex-shrink: 0; }}
.detail-dctrs-strip .readiness-dot {{ cursor: pointer; }}
.detail-dctrs-strip .readiness-dot:hover {{ opacity: 1; border-color: var(--accent); }}
.detail-close {{ background: none; border: none; color: var(--text-tertiary); font-size: 22px; cursor: pointer; padding: 0 4px; line-height: 1; flex-shrink: 0; }}
.detail-close:hover {{ color: var(--text-primary); }}
.detail-record-btn {{
  display: inline-flex; align-items: center; gap: 4px; font-size: 11px; padding: 4px 12px;
  border-radius: 6px; border: 1px solid rgba(34,197,94,0.4); background: rgba(34,197,94,0.1);
  color: #22c55e; cursor: pointer; font-weight: 600; font-family: var(--font-sans);
  transition: all 0.15s; flex-shrink: 0; margin-left: auto;
}}
.detail-record-btn:hover {{ background: rgba(34,197,94,0.2); border-color: #22c55e; }}
.detail-record-btn svg {{ fill: none; stroke: currentColor; stroke-width: 2; }}
.record-action-btn {{ color: #22c55e !important; border-color: rgba(34,197,94,0.4) !important; }}
.record-action-btn:hover {{ background: rgba(34,197,94,0.12) !important; }}
/* Meta strip — fixed below header */
.detail-meta-strip {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 20px; border-bottom: 1px solid var(--border-subtle); }}
.meta-chip {{ display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; font-family: var(--font-sans); background: rgba(255,255,255,0.06); border: 1px solid var(--border-subtle); cursor: pointer; color: var(--text-secondary); transition: all 0.15s; user-select: none; white-space: nowrap; }}
.meta-chip:hover {{ background: rgba(255,255,255,0.10); color: var(--text-primary); }}
.meta-chip .chip-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.meta-chip--priority .chip-dot.high {{ background: #ef4444; }}
.meta-chip--priority .chip-dot.medium {{ background: #eab308; }}
.meta-chip--priority .chip-dot.low {{ background: #22c55e; }}
.meta-chip--status {{ }}
.meta-chip--section {{ cursor: default; color: var(--text-tertiary); border-color: transparent; background: transparent; }}
.meta-chip--section:hover {{ background: transparent; color: var(--text-tertiary); }}
.meta-chip--parent {{ }}
.meta-chip--parent .chip-label {{ color: var(--text-tertiary); }}
.meta-chip--parent .chip-value {{ color: var(--accent); font-family: var(--font-mono); }}
.meta-chip--parent input {{ width: 60px; font-size: 11px; background: var(--bg-card); border: 1px solid var(--accent); color: var(--text-primary); border-radius: 4px; padding: 1px 4px; font-family: var(--font-mono); outline: none; }}
/* Status dropdown for meta chip */
.meta-status-dropdown {{ position: absolute; z-index: 1010; background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 8px; padding: 4px; box-shadow: 0 8px 24px rgba(0,0,0,0.4); min-width: 140px; }}
.meta-status-opt {{ display: block; width: 100%; text-align: left; font-size: 12px; padding: 6px 10px; border: none; background: none; color: var(--text-secondary); cursor: pointer; border-radius: 4px; font-family: var(--font-sans); }}
.meta-status-opt:hover {{ background: var(--bg-hover); color: var(--text-primary); }}
.meta-status-opt.active {{ color: var(--accent); font-weight: 600; }}

/* Kitchen automation chip + picker (M1a) */
.meta-chip--automation .chip-label {{ color: var(--text-tertiary); }}
.meta-chip--automation .chip-value {{ font-weight: 700; }}
.meta-chip--automation[data-mode="manual"] .chip-value {{ color: var(--text-tertiary); }}
.meta-chip--automation[data-mode="auto"]    .chip-value {{ color: #3b82f6; }}
.meta-chip--automation[data-mode="held"]    .chip-value {{ color: #f59e0b; }}
.automation-picker {{ position: absolute; z-index: 1010; background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 8px; padding: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.4); min-width: 240px; }}
.automation-picker-row {{ display: flex; gap: 4px; margin-bottom: 6px; }}
.automation-picker-opt {{ flex: 1; font-size: 12px; padding: 6px 8px; border: 1px solid var(--border-subtle); background: none; color: var(--text-secondary); cursor: pointer; border-radius: 6px; font-family: var(--font-sans); }}
.automation-picker-opt:hover {{ border-color: var(--accent); }}
.automation-picker-opt.active {{ border-color: var(--accent); color: var(--accent); font-weight: 700; }}
.automation-picker-reason {{ width: 100%; box-sizing: border-box; font-size: 12px; padding: 6px 8px; border: 1px solid var(--border-subtle); border-radius: 6px; background: var(--bg-card); color: var(--text-primary); font-family: var(--font-sans); resize: vertical; min-height: 50px; outline: none; }}
.automation-picker-reason:focus {{ border-color: var(--accent); }}
.automation-picker-actions {{ display: flex; gap: 6px; justify-content: flex-end; margin-top: 8px; }}
.automation-picker-actions button {{ font-size: 11px; padding: 4px 12px; border-radius: 4px; border: 1px solid var(--border-subtle); background: none; color: var(--text-secondary); cursor: pointer; font-family: var(--font-sans); }}
.automation-picker-actions button.primary {{ background: var(--accent); color: white; border-color: var(--accent); }}
.automation-picker-error {{ font-size: 11px; color: #ef4444; margin-top: 4px; min-height: 14px; }}

/* Kitchen activity history list (M1b) */
.history-list {{ display: flex; flex-direction: column; gap: 4px; max-height: 360px; overflow-y: auto; }}
.history-list.hidden {{ display: none; }}
.history-row {{ display: grid; grid-template-columns: 80px 70px 1fr 110px; gap: 8px; padding: 6px 8px; border-radius: 6px; font-size: 11px; border: 1px solid var(--border-subtle); background: rgba(255,255,255,0.02); align-items: start; }}
.history-row.discarded {{ opacity: 0.45; text-decoration: line-through; }}
.history-row .h-actor {{ font-size: 10px; padding: 1px 6px; border-radius: 8px; font-weight: 700; text-align: center; }}
.history-row .h-actor.human  {{ background: rgba(99,102,241,0.18); color: #818cf8; }}
.history-row .h-actor.agent  {{ background: rgba(34,197,94,0.18); color: #22c55e; }}
.history-row .h-actor.system {{ background: rgba(234,179,8,0.18); color: #eab308; }}
.history-row .h-kind {{ font-family: var(--font-mono); font-size: 10px; color: var(--text-tertiary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.history-row .h-summary {{ color: var(--text-secondary); font-family: var(--font-mono); font-size: 11px; word-break: break-word; }}
.history-row .h-summary .h-old {{ color: var(--text-tertiary); text-decoration: line-through; }}
.history-row .h-summary .h-new {{ color: var(--accent); }}
.history-row .h-time {{ font-size: 10px; color: var(--text-tertiary); text-align: right; font-variant-numeric: tabular-nums; }}
.history-empty {{ padding: 20px; text-align: center; color: var(--text-tertiary); font-size: 12px; }}

/* Live run panel (M3) */
.detail-runs {{ border: 1px solid var(--border-subtle); border-radius: 8px; padding: 10px 12px; background: var(--bg-card); }}
.detail-runs.hidden {{ display: none; }}
.detail-runs .detail-section-header {{ margin-bottom: 6px; padding-bottom: 6px; }}
.run-now-btn {{
  font-size: 11px; padding: 3px 10px; border-radius: 6px; border: 1px solid var(--accent);
  background: rgba(59,130,246,0.10); color: var(--accent); cursor: pointer;
  font-family: var(--font-sans); transition: all 0.15s; display: inline-flex; align-items: center; gap: 4px;
}}
.run-now-btn:hover {{ background: rgba(59,130,246,0.20); }}
.run-now-btn:disabled {{ opacity: 0.4; cursor: not-allowed; }}
.run-card {{ border: 1px solid var(--border-subtle); border-radius: 6px; background: rgba(255,255,255,0.02); padding: 8px 10px; margin-top: 6px; }}
.run-card-top {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }}
.run-pill {{
  font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 8px; text-transform: uppercase;
  letter-spacing: 0.4px; white-space: nowrap;
}}
.run-pill-queued, .run-pill-preparing {{ background: rgba(59,130,246,0.15); color: #3b82f6; animation: kitchen-pulse 1.6s ease-in-out infinite; }}
.run-pill-running  {{ background: rgba(59,130,246,0.20); color: #60a5fa; animation: kitchen-pulse 1.6s ease-in-out infinite; }}
.run-pill-needs_input {{ background: rgba(245,158,11,0.20); color: #f59e0b; }}
.run-pill-succeeded {{ background: rgba(34,197,94,0.15); color: #22c55e; }}
.run-pill-failed, .run-pill-stalled {{ background: rgba(239,68,68,0.15); color: #ef4444; }}
.run-pill-cancelled {{ background: rgba(107,114,128,0.15); color: #9ca3af; }}
.run-summary {{ font-size: 12px; color: var(--text-secondary); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.run-meta {{ font-size: 11px; color: var(--text-tertiary); display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 6px; font-variant-numeric: tabular-nums; }}
.run-ws-link {{ font-size: 11px; color: var(--accent); text-decoration: none; word-break: break-all; }}
.run-ws-link:hover {{ text-decoration: underline; }}
.run-actions {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }}
.run-action-btn {{
  font-size: 11px; padding: 4px 12px; border-radius: 5px; border: 1px solid var(--border-default);
  background: var(--bg-page); color: var(--text-secondary); cursor: pointer;
  font-family: var(--font-sans); display: inline-flex; align-items: center; gap: 4px; transition: all 0.15s;
}}
.run-action-btn:hover {{ background: var(--bg-hover); border-color: var(--accent); color: var(--accent); }}
.run-action-btn.primary {{ background: rgba(59,130,246,0.12); color: var(--accent); border-color: var(--accent); }}
.run-action-btn.primary:hover {{ background: rgba(59,130,246,0.22); }}
.run-action-btn.danger {{ color: #ef4444; border-color: rgba(239,68,68,0.4); }}
.run-action-btn.danger:hover {{ background: rgba(239,68,68,0.1); }}
.run-action-btn svg {{ width: 12px; height: 12px; vertical-align: -1px; }}
.run-discard-confirm {{ display: flex; align-items: center; gap: 8px; margin-top: 6px; font-size: 12px; color: var(--text-secondary); }}
.run-discard-confirm.hidden {{ display: none; }}
.run-discard-confirm button {{ font-size: 11px; padding: 2px 10px; border-radius: 4px; border: 1px solid var(--border-default); background: none; color: var(--text-secondary); cursor: pointer; font-family: var(--font-sans); }}
.run-discard-confirm button.danger {{ color: #ef4444; border-color: rgba(239,68,68,0.5); }}
.run-discard-confirm button:hover {{ background: var(--bg-hover); }}
.run-history-list {{ margin-top: 8px; border-top: 1px dashed var(--border-subtle); padding-top: 8px; display: none; }}
.run-history-list.visible {{ display: block; }}
.run-history-toggle {{ font-size: 11px; color: var(--text-tertiary); cursor: pointer; background: none; border: none; padding: 0; font-family: var(--font-sans); }}
.run-history-toggle:hover {{ color: var(--text-secondary); }}
.run-history-row {{ display: flex; align-items: center; gap: 8px; padding: 4px 6px; border-radius: 4px; font-size: 11px; color: var(--text-secondary); cursor: default; }}
.run-history-row:hover {{ background: var(--bg-hover); }}
.run-history-row .run-pill {{ font-size: 9px; padding: 1px 6px; }}
.run-history-row .rh-time {{ color: var(--text-tertiary); margin-left: auto; white-space: nowrap; font-variant-numeric: tabular-nums; }}
/* needs_input inline response panel */
.run-ni-panel {{ margin-top: 8px; padding: 8px 10px; border: 1px solid rgba(245,158,11,0.35); border-radius: 6px; background: rgba(245,158,11,0.05); }}
.run-ni-panel.hidden {{ display: none; }}
.run-ni-prompt {{ font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; white-space: pre-wrap; word-break: break-word; }}
.run-ni-textarea {{ display: block; width: 100%; box-sizing: border-box; font-size: 12px; padding: 6px 8px; border: 1px solid var(--border-subtle); border-radius: 5px; background: var(--bg-card); color: var(--text-primary); font-family: var(--font-sans); resize: vertical; min-height: 56px; outline: none; }}
.run-ni-textarea:focus {{ border-color: var(--accent); }}
.run-ni-actions {{ display: flex; gap: 6px; margin-top: 6px; }}
.run-ni-send {{ font-size: 11px; padding: 4px 14px; border-radius: 5px; border: none; background: var(--accent); color: #fff; cursor: pointer; font-family: var(--font-sans); display: inline-flex; align-items: center; gap: 4px; }}
.run-ni-send:disabled {{ opacity: 0.4; cursor: not-allowed; }}
.run-ni-cancel {{ font-size: 11px; padding: 4px 12px; border-radius: 5px; border: 1px solid var(--border-default); background: none; color: var(--text-secondary); cursor: pointer; font-family: var(--font-sans); }}
/* Per-card run-now button */
.card-run-now-btn {{
  display: none; cursor: pointer; background: none; border: none; padding: 2px 4px;
  color: var(--accent); opacity: 0.7; line-height: 1; transition: opacity 0.15s;
}}
.card-run-now-btn:hover {{ opacity: 1; background: rgba(59,130,246,0.10); border-radius: 3px; }}
.edit-enabled .card[data-eligible="true"] .card-run-now-btn {{ display: inline-flex; align-items: center; }}
/* No-tests-required block in Tests section (M1a) */
.ntr-block {{ margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border-subtle); }}
.ntr-checkbox-row {{ display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-secondary); cursor: pointer; user-select: none; }}
.ntr-checkbox-row input[type="checkbox"] {{ cursor: pointer; }}
.ntr-note {{ display: block; width: 100%; box-sizing: border-box; margin-top: 8px; font-size: 12px; padding: 6px 8px; border: 1px solid var(--border-subtle); border-radius: 6px; background: var(--bg-card); color: var(--text-primary); font-family: var(--font-sans); resize: vertical; outline: none; }}
.ntr-note:focus {{ border-color: var(--accent); }}
.ntr-note.hidden {{ display: none; }}
/* Scroll body */
.detail-body {{ flex: 1; overflow-y: auto; padding: 16px 20px; }}
/* Sections — always visible, stacked */
.detail-section {{ display: block; margin-bottom: 20px; }}
.detail-section:last-child {{ margin-bottom: 0; }}
.detail-section-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid var(--border-subtle); }}
.detail-section-header h3 {{ margin: 0; font-size: 13px; font-weight: 600; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.3px; display: flex; align-items: center; gap: 6px; }}
.detail-section-header h3 .section-flag {{ font-size: 11px; width: 18px; height: 18px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--border-subtle); color: var(--text-tertiary); font-weight: 700; font-family: var(--font-mono); }}
.detail-section-header h3 .section-flag.filled {{ background: rgba(34,197,94,0.15); color: #22c55e; border-color: rgba(34,197,94,0.3); }}
.section-assess-btn {{ font-size: 11px; padding: 3px 10px; border-radius: 6px; border: 1px solid var(--border-subtle); background: none; color: var(--text-tertiary); cursor: pointer; font-family: var(--font-sans); transition: all 0.15s; opacity: 0.4; }}
.detail-section:hover .section-assess-btn, .section-assess-btn:focus {{ opacity: 1; }}
.section-assess-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
.section-assess-btn.loading {{ opacity: 1; color: var(--accent); pointer-events: none; }}
/* Editors */
.detail-editor {{ width: 100%; min-height: 80px; background: var(--bg-card); color: var(--text-primary); border: 1px solid transparent; border-radius: 6px; padding: 10px 12px; font-family: var(--font-mono); font-size: 13px; resize: vertical; line-height: 1.5; box-sizing: border-box; transition: border-color 0.15s; }}
.detail-editor:hover {{ border-color: var(--border-default); }}
.detail-editor:focus {{ outline: none; border-color: var(--accent); background: var(--bg-surface); }}
.detail-editor.desc-editor {{ min-height: 120px; }}
.detail-editor-empty {{ color: var(--text-tertiary); font-style: italic; }}
/* Criteria */
.detail-criteria-list {{ list-style: none; padding: 0; margin: 0 0 8px 0; }}
.detail-criteria-item {{ display: flex; align-items: center; gap: 6px; padding: 4px 0; font-size: 13px; color: var(--text-secondary); }}
.detail-criteria-item .criteria-bullet {{ color: var(--text-tertiary); font-size: 11px; flex-shrink: 0; user-select: none; }}
.detail-criteria-item .criteria-text {{ flex: 1; cursor: text; padding: 2px 4px; border-radius: 3px; transition: background 0.15s; line-height: 1.4; }}
.detail-criteria-item .criteria-text:hover {{ background: rgba(255,255,255,0.04); }}
.detail-criteria-item .criteria-text[contenteditable="true"] {{ background: var(--bg-card); outline: none; border: 1px solid var(--accent); padding: 1px 3px; }}
.detail-criteria-item .criteria-delete {{ background: none; border: none; color: var(--text-tertiary); cursor: pointer; font-size: 14px; padding: 0 2px; line-height: 1; opacity: 0; transition: opacity 0.15s; flex-shrink: 0; }}
.detail-criteria-item:hover .criteria-delete {{ opacity: 1; }}
.detail-criteria-item .criteria-delete:hover {{ color: #ef4444; }}
.criteria-add-input {{ width: 100%; font-size: 13px; padding: 8px 12px; background: var(--bg-card); border: 1px solid transparent; border-radius: 6px; color: var(--text-primary); font-family: var(--font-mono); outline: none; box-sizing: border-box; transition: border-color 0.15s; }}
.criteria-add-input:hover {{ border-color: var(--border-default); }}
.criteria-add-input:focus {{ border-color: var(--accent); background: var(--bg-surface); }}
.criteria-add-input::placeholder {{ color: var(--text-tertiary); font-style: italic; }}
@media (max-width: 560px) {{
  .detail-panel {{ max-width: 100vw; max-height: 100vh; border-radius: 0; inset: 0; }}
  .detail-meta-strip {{ gap: 6px; }}
  .detail-dctrs-strip {{ order: 10; width: 100%; justify-content: center; padding-top: 4px; }}
}}

.status-dropdown-opt:hover {{ background: var(--bg-hover); }}
.list-row-detail {{ display: none; padding: 6px 8px 4px 22px; }}
.list-row.expanded .list-row-detail {{ display: block; }}


/* Card moved highlight */
@keyframes card-moved {{
  0% {{ box-shadow: 0 0 0 2px var(--accent), 0 0 12px var(--accent); transform: scale(1.02); }}
  100% {{ box-shadow: none; transform: scale(1); }}
}}
.card.just-moved, .list-row.just-moved {{
  animation: card-moved 1.5s ease-out forwards;
}}

/* Live-update enter/exit */
.card.card-enter, .list-row.card-enter {{
  animation: card-enter 0.3s ease-out forwards;
}}
@keyframes card-enter {{
  from {{ opacity: 0; transform: translateY(4px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}
.card.card-exit, .list-row.card-exit {{
  animation: card-exit 0.3s ease-out forwards;
}}
@keyframes card-exit {{
  from {{ opacity: 1; }}
  to {{ opacity: 0; transform: translateY(-4px); }}
}}
/* Highlight changed content in-place */
.card.content-changed {{
  animation: content-flash 0.8s ease-out;
}}
@keyframes content-flash {{
  0% {{ background: rgba(59,130,246,0.08); }}
  100% {{ background: var(--bg-card); }}
}}

/* Column collapse (unused but kept for safety) */
.column.collapsed .column-body {{ display: none; }}
.column.collapsed {{ flex: 0 0 280px; }}

/* Draft tickets */
.kanban-card.is-draft, .list-row.is-draft, .card.is-draft {{
  opacity: 0.45; border-style: dashed;
}}
.card.is-draft::after {{
  content: 'DRAFT';
  position: absolute; top: 4px; right: 6px;
  font-size: 8px; font-weight: 700; letter-spacing: 0.5px;
  color: var(--text-tertiary); background: var(--bg-hover);
  padding: 1px 5px; border-radius: 3px; pointer-events: none;
}}
.card.is-draft .priority-dot {{ opacity: 0.4; }}

/* Settings drawer */
.settings-toggle {{
  font-size: 15px; background: none; border: none; color: var(--text-tertiary);
  cursor: pointer; padding: 8px 12px; border-radius: 6px; line-height: 1;
  transition: color 0.15s, background 0.15s; min-width: 36px; min-height: 36px;
  display: inline-flex; align-items: center; justify-content: center;
}}
.settings-toggle:hover {{ color: var(--text-primary); background: var(--bg-hover); }}
.settings-drawer {{
  position: fixed; top: 0; right: 0; height: 100vh; width: 320px; z-index: 1100;
  background: var(--bg-surface); border-left: 1px solid var(--border-default);
  box-shadow: -8px 0 32px rgba(0,0,0,0.4); display: flex; flex-direction: column;
  transform: translateX(0); transition: transform 0.25s ease;
}}
.settings-drawer.hidden {{ transform: translateX(100%); pointer-events: none; }}
.settings-drawer-header {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px; border-bottom: 1px solid var(--border-subtle);
}}
.settings-drawer-header h2 {{ margin: 0; font-size: 14px; font-weight: 700; color: var(--text-primary); }}
.settings-drawer-close {{
  background: none; border: none; color: var(--text-tertiary); cursor: pointer;
  font-size: 20px; line-height: 1; padding: 0 4px;
}}
.settings-drawer-close:hover {{ color: var(--text-primary); }}
.settings-drawer-body {{ flex: 1; overflow-y: auto; padding: 16px 20px; }}
.theme-toggle {{ display: inline-flex; gap: 2px; background: var(--bg-page); border: 1px solid var(--border-default); border-radius: 6px; padding: 2px; }}
.theme-opt {{ font-size: 14px; padding: 3px 10px; border: none; border-radius: 4px; background: none; color: var(--text-tertiary); cursor: pointer; transition: all 0.15s; font-family: var(--font-sans); }}
.theme-opt:hover {{ color: var(--text-secondary); }}
.theme-opt.active {{ background: var(--bg-hover); color: var(--text-primary); }}
.settings-section {{ margin-bottom: 20px; }}
.settings-section-title {{
  font-size: 11px; font-weight: 700; color: var(--text-tertiary); text-transform: uppercase;
  letter-spacing: 0.5px; margin-bottom: 12px;
}}
.settings-row {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 0; gap: 10px;
}}
.settings-row label {{
  font-size: 12px; color: var(--text-secondary); flex-shrink: 0;
}}
.settings-row input[type="text"] {{
  font-size: 11px; padding: 4px 8px; border-radius: 5px; flex: 1;
  border: 1px solid var(--border-default); background: var(--bg-card);
  color: var(--text-primary); font-family: var(--font-mono); outline: none; min-width: 0;
}}
.settings-row input[type="text"]:focus {{ border-color: var(--accent); }}
.settings-status-label {{ font-size: 10px; color: var(--text-tertiary); white-space: nowrap; }}
.settings-status-dot {{
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--text-tertiary);
}}
.settings-status-dot.ok {{ background: #22c55e; }}
.settings-status-dot.warn {{ background: #eab308; }}
.settings-status-dot.err {{ background: #ef4444; }}
.settings-toggle-switch {{
  position: relative; width: 32px; height: 18px; flex-shrink: 0;
}}
.settings-toggle-switch input {{ opacity: 0; width: 0; height: 0; }}
.settings-toggle-slider {{
  position: absolute; inset: 0; background: var(--border-default); border-radius: 9px;
  cursor: pointer; transition: background 0.2s;
}}
.settings-toggle-slider::before {{
  content: ''; position: absolute; width: 12px; height: 12px; left: 3px; bottom: 3px;
  background: #fff; border-radius: 50%; transition: transform 0.2s;
}}
.settings-toggle-switch input:checked + .settings-toggle-slider {{ background: var(--accent); }}
.settings-toggle-switch input:checked + .settings-toggle-slider::before {{ transform: translateX(14px); }}
.settings-toggle-switch input:disabled + .settings-toggle-slider {{ opacity: 0.35; cursor: not-allowed; }}
.settings-install-btn {{
  font-size: 11px; padding: 5px 14px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--accent); background: rgba(59,130,246,0.1);
  color: var(--accent); font-weight: 600; font-family: var(--font-sans); transition: all 0.15s;
}}
.settings-install-btn:hover {{ background: rgba(59,130,246,0.2); }}
.settings-install-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.settings-hint {{ font-size: 10px; color: var(--text-tertiary); padding: 0 0 8px 0; line-height: 1.4; }}
.settings-link {{
  font-size: 11px; color: var(--accent); text-decoration: none;
}}
.settings-link:hover {{ text-decoration: underline; }}
.empty-state {{
  display: none; flex-direction: column; align-items: center; justify-content: center;
  padding: 80px 20px; text-align: center; min-height: 400px;
}}
.empty-state-icon {{ font-size: 48px; color: var(--text-tertiary); margin-bottom: 16px; }}
.empty-state-title {{ font-size: 20px; font-weight: 600; color: var(--text-primary); margin-bottom: 8px; }}
.empty-state-desc {{ font-size: 14px; color: var(--text-secondary); margin-bottom: 24px; max-width: 400px; }}
.empty-state-actions {{ display: flex; gap: 12px; }}
.empty-state-btn {{
  padding: 10px 20px; border-radius: 8px; font-size: 14px; font-weight: 500;
  cursor: pointer; font-family: inherit; border: none;
}}
.empty-state-btn.primary {{ background: var(--accent); color: #fff; }}
.empty-state-btn.primary:hover {{ opacity: 0.9; }}
.empty-state-btn.secondary {{
  background: rgba(59,130,246,0.12); color: var(--accent); border: 1px solid rgba(59,130,246,0.3);
}}
.empty-state-btn.secondary:hover {{ background: rgba(59,130,246,0.2); }}
.empty-state-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.managed-files-list {{ display: flex; flex-direction: column; gap: 4px; }}
.managed-file-row {{
  display: flex; align-items: center; gap: 8px; padding: 6px 8px;
  border-radius: 6px; background: var(--bg-card); border: 1px solid var(--border-subtle);
}}
.managed-file-dot {{
  width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
}}
.managed-file-dot.exists {{ background: #22c55e; }}
.managed-file-dot.missing {{ background: var(--text-tertiary); opacity: 0.4; }}
.managed-file-path {{
  font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
  font-size: 11px; color: var(--text-primary); white-space: nowrap;
}}
.managed-file-desc {{
  font-size: 10px; color: var(--text-secondary); margin-left: auto; text-align: right;
}}
.managed-file-badge {{
  font-size: 9px; padding: 1px 5px; border-radius: 3px;
  background: var(--bg-badge); color: var(--text-tertiary);
}}

/* Attachments section in detail overlay */
.attachments-list {{
  display: flex; flex-direction: column; gap: 6px; margin-top: 4px;
}}
.attachment-row {{
  display: flex; align-items: center; gap: 10px; padding: 8px 10px;
  background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 6px;
  cursor: pointer; transition: background 0.15s, border-color 0.15s;
}}
.attachment-row:hover {{ background: var(--bg-hover); border-color: var(--border-default); }}
.attachment-thumb {{
  width: 60px; height: 40px; object-fit: cover; border-radius: 4px;
  flex-shrink: 0; background: var(--bg-hover); display: block;
}}
.attachment-info {{ flex: 1; min-width: 0; }}
.attachment-summary {{
  font-size: 12px; color: var(--text-primary); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; margin-bottom: 2px;
}}
.attachment-meta {{
  font-size: 10px; color: var(--text-tertiary); font-family: var(--font-mono);
}}
.attachment-placeholder {{
  animation: att-pulse 1.5s ease-in-out infinite;
}}
.attachment-placeholder .att-pulse-dot {{
  width: 8px; height: 8px; border-radius: 50%; background: var(--accent);
  animation: att-pulse 1.5s ease-in-out infinite; flex-shrink: 0;
}}
@keyframes att-pulse {{ 0%,100% {{ opacity: 0.4; }} 50% {{ opacity: 1; }} }}
.attachment-actions {{
  display: flex; gap: 4px; flex-shrink: 0;
}}
.attachment-action-btn {{
  font-size: 9px; padding: 2px 7px; border-radius: 4px; border: 1px solid var(--border-default);
  background: none; color: var(--text-tertiary); cursor: pointer; font-weight: 600;
  white-space: nowrap; transition: all 0.15s;
}}
.attachment-action-btn:hover {{ color: var(--text-primary); border-color: var(--text-secondary); }}
.attachment-action-btn.danger:hover {{ color: #ef4444; border-color: #ef4444; background: rgba(239,68,68,0.06); }}
.attachments-empty {{
  font-size: 12px; color: var(--text-tertiary); padding: 16px 0;
  text-align: center; font-style: italic;
}}
.attachments-actions {{ display: flex; gap: 6px; }}
.record-feedback-btn, .link-session-btn {{
  font-size: 10px; padding: 3px 10px; border-radius: 5px; border: 1px solid var(--border-default);
  background: none; color: var(--text-secondary); cursor: pointer; font-weight: 600;
  font-family: var(--font-sans); transition: all 0.15s;
}}
.record-feedback-btn.active {{
  background: rgba(34,197,94,0.12); color: #22c55e; border-color: rgba(34,197,94,0.4);
}}
.record-feedback-btn:hover {{ border-color: #22c55e; color: #22c55e; }}
.link-session-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
.attachment-count-badge {{
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 8px; font-weight: 700; min-width: 14px; height: 14px; padding: 0 3px;
  border-radius: 7px; background: rgba(59,130,246,0.15); color: var(--accent);
  margin-left: 3px; font-family: var(--font-mono);
}}
@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }}
}}
/* Workflow Bounce UI */
.wf-agents-list, .wf-workflows-list {{ display: flex; flex-direction: column; gap: 4px; }}
.wf-agent-row, .wf-workflow-row {{
  display: flex; align-items: center; gap: 6px; background: var(--bg-card);
  border: 1px solid var(--border); border-radius: 4px; padding: 6px 8px; font-size: 11px;
}}
.wf-agent-row.readonly {{ opacity: 0.5; }}
.wf-row-name {{ font-weight: 600; min-width: 80px; }}
.wf-row-cmd {{ font-family: var(--font-mono); font-size: 10px; color: var(--fg-dim); }}
.wf-row-source {{ font-size: 8px; text-transform: uppercase; color: var(--fg-dim); letter-spacing: 0.5px; }}
.wf-row-steps {{ font-size: 10px; color: var(--fg-dim); margin-left: auto; }}
.wf-row-actions {{ display: flex; gap: 4px; margin-left: auto; }}
.wf-row-actions button {{
  font-size: 9px; padding: 1px 5px; border: 1px solid var(--border); border-radius: 3px;
  background: transparent; color: var(--fg); cursor: pointer;
}}
.wf-row-actions button:hover {{ background: var(--bg-card); border-color: var(--accent); }}
.wf-row-actions button.danger:hover {{ border-color: #ef4444; color: #ef4444; }}
.wf-add-btn {{
  display: block; width: 100%; padding: 5px; font-size: 11px; border: 1px dashed var(--border);
  border-radius: 4px; background: transparent; color: var(--fg-dim); cursor: pointer;
  margin-top: 4px; text-align: center;
}}
.wf-add-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
.wf-form {{
  display: flex; flex-direction: column; gap: 6px; background: var(--bg-card);
  border: 1px solid var(--border); border-radius: 4px; padding: 8px; margin-top: 4px;
}}
.wf-form.hidden {{ display: none; }}
.wf-input, .wf-textarea {{
  font-size: 11px; padding: 4px 6px; border: 1px solid var(--border); border-radius: 3px;
  background: var(--bg); color: var(--fg); font-family: var(--font-sans);
}}
.wf-textarea {{ font-family: var(--font-mono); resize: vertical; }}
.wf-form-actions {{ display: flex; gap: 6px; }}
.wf-save-btn {{
  font-size: 11px; padding: 3px 10px; border: none; border-radius: 3px;
  background: var(--accent); color: #fff; cursor: pointer;
}}
.wf-save-btn:hover {{ opacity: 0.9; }}
.wf-cancel-btn {{
  font-size: 11px; padding: 3px 10px; border: 1px solid var(--border); border-radius: 3px;
  background: transparent; color: var(--fg); cursor: pointer;
}}
.wf-cancel-btn:hover {{ background: var(--bg-card); }}
.wf-step-list {{ display: flex; flex-direction: column; gap: 3px; margin-top: 4px; }}
.wf-step-row {{ display: flex; align-items: center; gap: 6px; font-size: 11px; }}
.wf-step-idx {{ font-weight: 600; min-width: 16px; text-align: center; }}
.wf-step-primary {{ font-size: 8px; text-transform: uppercase; color: var(--accent); letter-spacing: 0.5px; }}
#section-workflow {{ margin-top: 8px; }}
.workflow-select {{
  font-size: 11px; padding: 3px 6px; border: 1px solid var(--border); border-radius: 3px;
  background: var(--bg); color: var(--fg); min-width: 140px;
}}
.workflow-run-btn {{
  font-size: 11px; padding: 3px 10px; border: none; border-radius: 3px;
  background: var(--accent); color: #fff; cursor: pointer;
}}
.workflow-run-btn:disabled {{ opacity: 0.4; cursor: default; }}
.workflow-run-btn:hover:not(:disabled) {{ opacity: 0.9; }}
.workflow-runs-list {{ display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }}
.workflow-run-block {{
  border: 1px solid var(--border); border-radius: 4px; overflow: hidden;
  background: var(--bg-card);
}}
.workflow-run-header {{
  display: flex; align-items: center; gap: 8px; padding: 6px 8px;
  cursor: pointer; font-size: 11px;
}}
.workflow-run-header:hover {{ background: var(--bg); }}
.wf-run-status {{
  font-size: 9px; font-weight: 600; text-transform: uppercase; padding: 1px 6px;
  border-radius: 3px; letter-spacing: 0.5px;
}}
.wf-run-status.running {{ background: rgba(59,130,246,0.15); color: #3b82f6; animation: wfPulse 1.5s ease-in-out infinite; }}
.wf-run-status.paused {{ background: rgba(234,179,8,0.15); color: #eab308; }}
.wf-run-status.completed {{ background: rgba(34,197,94,0.15); color: #22c55e; }}
.wf-run-status.failed {{ background: rgba(239,68,68,0.15); color: #ef4444; }}
.wf-run-status.cancelled {{ background: rgba(107,114,128,0.15); color: #6b7280; }}
.wf-run-status.pending {{ background: rgba(107,114,128,0.1); color: #9ca3af; }}
@keyframes wfPulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}

/* Active workflow indicator on kanban cards */
.card-wf-indicator {{
  display: inline-flex; align-items: center; gap: 3px;
  font-size: 8px; color: #3b82f6; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.3px; animation: wfPulse 1.5s ease-in-out infinite;
}}
.workflow-conversation {{ display: none; }}
.workflow-run-block.expanded .workflow-conversation {{ display: block; }}
.workflow-turn {{ padding: 8px 10px; border-top: 1px solid var(--border); }}
.workflow-turn-header {{
  display: flex; align-items: center; gap: 8px; margin-bottom: 4px;
}}
.workflow-turn-header .agent-name {{ font-weight: 600; font-size: 11px; }}
.workflow-turn-header .turn-meta {{ font-size: 10px; font-family: var(--font-mono); color: var(--fg-dim); }}
.workflow-turn-content {{ font-size: 11px; white-space: pre-wrap; line-height: 1.5; }}
.workflow-turn.disagreement {{
  background: rgba(234,179,8,0.08); border-left: 3px solid rgba(234,179,8,0.4);
}}

/* ── Full-page settings ── */
.settings-page {{
  display: none; position: fixed; inset: 0; z-index: 600;
  background: var(--bg-primary); overflow-y: auto; padding: 32px 48px;
}}
body.settings-open .settings-page {{ display: block; }}
body.settings-open .kanban,
body.settings-open .filter-bar,
body.settings-open .bottom-section,
body.settings-open #settings-drawer,
body.settings-open .settings-toggle {{ display: none !important; }}
body.settings-open .settings-back-btn {{ display: inline-flex; }}
.settings-back-btn {{
  display: none; font-size: 12px; padding: 4px 10px; border-radius: 6px;
  border: 1px solid var(--border-default); background: var(--bg-card);
  color: var(--text-secondary); cursor: pointer; font-family: inherit;
  align-items: center; gap: 4px; margin-right: 8px;
}}
.settings-back-btn:hover {{ color: var(--text-primary); border-color: var(--text-tertiary); }}
.settings-page h2 {{
  font-size: 18px; font-weight: 700; color: var(--text-primary); margin: 0 0 24px;
}}
.settings-page .sp-section {{
  margin-bottom: 32px; border: 1px solid var(--border-default);
  border-radius: 10px; padding: 20px; background: var(--bg-card);
}}
.settings-page .sp-section h3 {{
  font-size: 14px; font-weight: 600; color: var(--text-primary); margin: 0 0 12px;
}}

/* Agent editor */
.sp-agent-item {{
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border: 1px solid var(--border-default); border-radius: 6px; margin-bottom: 6px;
  background: var(--bg-primary);
}}
.sp-agent-item .sp-agent-name {{ font-weight: 600; font-size: 12px; flex: 1; }}
.sp-agent-item .sp-agent-model {{ font-size: 11px; color: var(--text-tertiary); font-family: var(--font-mono); }}
.sp-agent-form label {{ display: block; font-size: 11px; font-weight: 600; color: var(--text-secondary); margin-bottom: 3px; margin-top: 10px; }}
.sp-agent-form input, .sp-agent-form textarea, .sp-agent-form select {{
  width: 100%; padding: 6px 8px; font-size: 12px; font-family: inherit;
  border: 1px solid var(--border-default); border-radius: 6px;
  background: var(--bg-primary); color: var(--text-primary);
}}
.sp-agent-form textarea {{ min-height: 60px; resize: vertical; font-family: var(--font-mono); font-size: 11px; }}
.sp-agent-form .sp-btn-row {{ display: flex; gap: 6px; margin-top: 12px; }}
.sp-agent-form .sp-btn {{
  font-size: 11px; padding: 5px 14px; border-radius: 6px; border: 1px solid var(--border-default);
  background: var(--bg-card); color: var(--text-secondary); cursor: pointer; font-family: inherit;
}}
.sp-agent-form .sp-btn.primary {{ background: var(--accent); color: #fff; border-color: var(--accent); }}

/* Workflow editor */
.sp-wf-item {{
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border: 1px solid var(--border-default); border-radius: 6px; margin-bottom: 6px;
  background: var(--bg-primary); cursor: pointer;
}}
.sp-wf-item:hover {{ border-color: var(--text-tertiary); }}
.sp-wf-item .sp-wf-name {{ font-weight: 600; font-size: 12px; flex: 1; }}
.sp-wf-item .sp-wf-steps {{ font-size: 10px; color: var(--text-tertiary); }}
.sp-step-item {{
  display: flex; align-items: center; gap: 6px; padding: 6px 8px;
  border: 1px solid var(--border-default); border-radius: 5px; margin-bottom: 4px;
  background: var(--bg-primary); font-size: 11px;
}}
.sp-step-item .sp-step-order {{ font-weight: 700; font-size: 10px; color: var(--text-tertiary); min-width: 16px; text-align: center; }}
.sp-step-item .sp-step-agent {{ font-weight: 600; color: var(--accent); }}
.sp-step-item .sp-step-prompt {{ flex: 1; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.sp-step-item .sp-step-del {{
  font-size: 10px; cursor: pointer; color: var(--text-tertiary);
  border: none; background: none; padding: 2px 4px;
}}
.sp-step-item .sp-step-del:hover {{ color: #ef4444; }}
</style>
</head>
<body>

<div class="header-block">
  <div class="header-row1">
    <span class="header-title">Ticket Takeaway</span>
    <span class="header-date">Updated {escape(date_str)}</span>
    <div class="header-stats">
      <span class="header-stat">Total <strong>{count_total}</strong></span>
      <span class="header-stat">WIP <strong>{count_wip}</strong></span>
      <span class="header-stat">Review <strong>{count_review}</strong></span>
      <span class="header-stat">Done <strong>{count_done}</strong></span>
    </div>
  </div>
  <div class="header-row2">
    <span class="project-name">{escape(project_name)}</span>
    <span class="version-badge">{escape(cs.version)}</span>
    <div class="progress-bar-wrap">
      <div class="progress-bar"><div class="progress-fill" style="width: {progress_pct}%"></div></div>
      <span class="progress-pct">{progress_pct}%</span>
    </div>
    <div class="release-pills">
      <span class="release-pill">{escape(releases_text)}</span>
    </div>
    <div class="sparkline-wrap" title="Commit activity (12 weeks)"></div>
    <div class="code-stats">
      <span class="code-stat">Files <strong>{cs.files}</strong></span>
      <span class="code-stat">LOC <strong>{escape(cs.loc)}</strong></span>
      <span class="code-stat">Deps <strong>{escape(cs.deps)}</strong></span>
      <span class="code-stat">Last commit <strong>{escape(cs.last_commit)}</strong></span>
    </div>
  </div>
</div>

<div class="filter-bar" id="filterBar">
  <button class="settings-back-btn" id="settingsBackBtn">&larr; Back to Board</button>
  <button class="filter-btn active" data-filter="all" data-group="all">All <span class="count">{count_total}</span></button>
  <span class="filter-divider"></span>
  <span class="filter-group" data-group-name="status">
    <button class="filter-btn" data-filter="proposed" data-group="status">Proposed <span class="count">{count_status_proposed}</span></button>
    <button class="filter-btn" data-filter="in-progress" data-group="status">In Progress <span class="count">{count_status_inprogress}</span></button>
    <button class="filter-btn" data-filter="for-review" data-group="status">For Review <span class="count">{count_status_forreview}</span></button>
  </span>
  <span class="filter-divider"></span>
  <span class="filter-group" data-group-name="type">
    <button class="filter-btn" data-filter="bug" data-group="type">Bug <span class="count">{count_type_bug}</span></button>
  </span>
  <span class="filter-divider"></span>
  <span class="filter-group" data-group-name="size">
    <button class="filter-btn" data-filter="S" data-group="size">S <span class="count">{count_size_s}</span></button>
    <button class="filter-btn" data-filter="M" data-group="size">M <span class="count">{count_size_m}</span></button>
    <button class="filter-btn" data-filter="L" data-group="size">L <span class="count">{count_size_l}</span></button>
  </span>
  <span class="filter-divider"></span>
  <!-- Kitchen filters (M2) — automation lens on the existing kanban -->
  <span class="filter-group" data-group-name="kitchen">
    <button class="filter-btn" data-filter="auto"        data-group="kitchen" title="Tickets with automation_mode=auto">Auto <span class="count">{count_auto}</span></button>
    <button class="filter-btn" data-filter="held"        data-group="kitchen" title="Paused with reason">Held <span class="count">{count_held}</span></button>
    <button class="filter-btn" data-filter="eligible"    data-group="kitchen" title="Auto AND all eligibility gates clear">Eligible <span class="count">{count_eligible}</span></button>
    <button class="filter-btn" data-filter="needs_input" data-group="kitchen" title="Run paused awaiting human input">Needs Input <span class="count">{count_needs_input}</span></button>
    <button class="filter-btn" data-filter="failed"      data-group="kitchen" title="Last run failed or stalled">Failed <span class="count">{count_failed}</span></button>
  </span>
  <span class="filter-divider"></span>
  <button class="filter-btn" id="draftsToggleBtn" data-filter="draft" data-group="draft">Drafts</button>
  <button class="filter-btn" id="seekBtn" data-testid="seek-btn" title="Scan project files for ticket-like items">Seek</button>
  <input type="text" class="search-input" id="searchInput" placeholder="Search items...">
  <button class="settings-toggle" id="journeysBtn" data-testid="journeys-btn" title="Journeys" onclick="window.__goJourneys()">{_icon_journeys}</button>
  <button class="settings-toggle" id="settingsToggleBtn" data-testid="settings-toggle" title="Settings">{_icon_settings}</button>
  <button class="new-ticket-btn" id="newTicketBtn" data-testid="new-ticket-btn">+ New</button>
  <div class="new-ticket-panel" id="newTicketPanel" style="display:none">
    <div class="new-ticket-quick">
      <input type="text" id="newTicketTitle" data-testid="new-ticket-title" placeholder="What needs to be done?" class="new-ticket-input" />
      <select id="newTicketSection" data-testid="new-ticket-section" class="new-ticket-select">
        <option value="ideas">Idea</option>
        <option value="backlog">Backlog</option>
        <option value="wip">WIP</option>
        <option value="bugs">Bug</option>
      </select>
      <button id="newTicketSubmit" data-testid="new-ticket-submit" class="new-ticket-submit">Create</button>
    </div>
  </div>
</div>

<!-- Settings drawer -->
<div id="settings-drawer" class="settings-drawer hidden">
  <div class="settings-drawer-header">
    <h2>Settings</h2>
    <button class="settings-drawer-close" id="settingsDrawerClose">{_icon_close}</button>
  </div>
  <div class="settings-drawer-body">
    <div class="settings-section">
      <div class="settings-section-title">Appearance</div>
      <div class="settings-row">
        <label>Theme</label>
        <div class="theme-toggle" id="themeToggle">
          <button class="theme-opt" data-theme="light" title="Light" aria-label="Light theme">&#9788;</button>
          <button class="theme-opt" data-theme="system" title="System" aria-label="System theme">&#9684;</button>
          <button class="theme-opt active" data-theme="dark" title="Dark" aria-label="Dark theme">&#9790;</button>
        </div>
      </div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">Feedbacks Integration</div>
      <div class="settings-row">
        <label>Enable</label>
        <label class="settings-toggle-switch">
          <input type="checkbox" id="settingsFeedbacksEnabled">
          <span class="settings-toggle-slider"></span>
        </label>
        <span class="settings-status-label" id="feedbacksStatusLabel"></span>
        <span class="settings-status-dot" id="feedbacksStatusDot" title="Feedbacks status"></span>
      </div>
      <div class="settings-row">
        <label>Path</label>
        <input type="text" id="settingsFeedbacksPath" placeholder="~/projects/feedbacks">
      </div>
      <div class="settings-row">
        <label>Auto-start recording</label>
        <label class="settings-toggle-switch">
          <input type="checkbox" id="settingsFeedbacksAutostart">
          <span class="settings-toggle-slider"></span>
        </label>
      </div>
      <div class="settings-hint" id="settingsAutostartHint">Skip the Start button when opening the recorder — capture begins immediately.</div>
      <div class="settings-row">
        <a class="settings-link" href="{FEEDBACKS_REPO_URL}" target="_blank" rel="noopener">GitHub</a>
        <button class="settings-install-btn" id="settingsFeedbacksInstall">Install</button>
      </div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">Managed Files</div>
      <div class="settings-hint">Files and directories created or managed by Ticket Takeaway in this project.</div>
      <div id="managedFilesList" class="managed-files-list"></div>
    </div>
  </div>
</div>

<div class="empty-state" id="emptyState" data-testid="empty-state" style="display:none;">
  <div class="empty-state-icon">&#9744;</div>
  <h2 class="empty-state-title">No tickets yet</h2>
  <p class="empty-state-desc">Create your first ticket or scan your project for existing work items.</p>
  <div class="empty-state-actions">
    <button class="empty-state-btn primary" id="emptyStateCreate" data-testid="empty-state-create">+ Create First Ticket</button>
    <button class="empty-state-btn secondary" id="emptyStateSeek" data-testid="empty-state-seek">Seek &mdash; scan project files</button>
  </div>
</div>

<div class="kanban" id="kanban" data-testid="board-root">

  <!-- Ideas -->
  <div class="column" data-col="ideas" id="col-ideas" data-testid="column-ideas">
    <div class="column-header" data-prompt="/spec">
      <div class="column-dot" style="background: var(--status-idea)"></div>
      <span class="column-name">Ideas</span>
      <span class="column-count">{count_ideas}</span>
    </div>
    <div class="column-body">
{ideas_cards}
    </div>
  </div>

  <!-- Backlog -->
  <div class="column" data-col="backlog" id="col-backlog" data-testid="column-backlog">
    <div class="column-header" data-prompt="Help spec the next backlog items — which are ready to move to WIP?">
      <div class="column-dot" style="background: var(--status-backlog)"></div>
      <span class="column-name">Backlog</span>
      <span class="column-count">{count_backlog}</span>
    </div>
    <div class="column-body">
{backlog_cards}
    </div>
  </div>

  <!-- WIP -->
  <div class="column" data-col="wip" id="col-wip" data-testid="column-wip">
    <div class="column-header" data-prompt="Show me current WIP status and any blockers">
      <div class="column-dot" style="background: var(--status-wip)"></div>
      <span class="column-name">WIP</span>
      <span class="column-count">{count_wip}</span>
    </div>
    <div class="column-body">
{wip_cards}
    </div>
  </div>

  <!-- For Review -->
  <div class="column" data-col="review" id="col-review" data-testid="column-review">
    <div class="column-header" data-prompt="/review">
      <div class="column-dot" style="background: var(--status-review)"></div>
      <span class="column-name">For Review</span>
      <span class="column-count">{count_review}</span>
    </div>
    <div class="column-body">
{review_cards}
    </div>
  </div>


</div>

<!-- Full-page settings -->
<div class="settings-page" id="settings-page">
  <h2>Settings</h2>

  <div class="sp-section">
    <h3>Appearance</h3>
    <div id="spThemeToggle"></div>
  </div>

  <div class="sp-section">
    <h3>Feedbacks Integration</h3>
    <p style="font-size:12px;color:var(--text-tertiary);">Managed via the settings drawer.</p>
  </div>

  <div class="sp-section">
    <h3>Managed Files</h3>
    <div id="spManagedFiles" style="font-size:12px;color:var(--text-tertiary);">Loading...</div>
  </div>

  <div class="sp-section">
    <h3>Agents</h3>
    <div id="spAgentList"></div>
    <button class="sp-btn" id="spAgentAddBtn" style="margin-top:8px;font-size:11px;padding:5px 14px;border-radius:6px;border:1px solid var(--border-default);background:var(--bg-card);color:var(--text-secondary);cursor:pointer;">+ Add Agent</button>
    <div id="spAgentForm" class="sp-agent-form" style="display:none;margin-top:12px;border:1px solid var(--border-default);border-radius:8px;padding:14px;background:var(--bg-primary);">
      <input type="hidden" id="spAgentId" />
      <label for="spAgentNameInput">Name</label>
      <input type="text" id="spAgentNameInput" placeholder="e.g. reviewer" />
      <label for="spAgentModelInput">Model</label>
      <input type="text" id="spAgentModelInput" placeholder="e.g. claude-sonnet-4-20250514" />
      <label for="spAgentRoleInput">Role / System Prompt</label>
      <textarea id="spAgentRoleInput" placeholder="What this agent does..."></textarea>
      <label for="spAgentTempInput">Temperature</label>
      <input type="number" id="spAgentTempInput" min="0" max="2" step="0.1" value="0.3" style="width:80px;" />
      <div class="sp-btn-row">
        <button class="sp-btn primary" id="spAgentSaveBtn">Save</button>
        <button class="sp-btn" id="spAgentCancelBtn">Cancel</button>
      </div>
    </div>
  </div>

  <div class="sp-section">
    <h3>Workflows</h3>
    <div id="spWfList"></div>
    <button class="sp-btn" id="spWfAddBtn" style="margin-top:8px;font-size:11px;padding:5px 14px;border-radius:6px;border:1px solid var(--border-default);background:var(--bg-card);color:var(--text-secondary);cursor:pointer;">+ Add Workflow</button>
    <div id="spWfForm" style="display:none;margin-top:12px;border:1px solid var(--border-default);border-radius:8px;padding:14px;background:var(--bg-primary);">
      <input type="hidden" id="spWfId" />
      <label style="display:block;font-size:11px;font-weight:600;color:var(--text-secondary);margin-bottom:3px;">Name</label>
      <input type="text" id="spWfNameInput" placeholder="e.g. review-and-merge" style="width:100%;padding:6px 8px;font-size:12px;border:1px solid var(--border-default);border-radius:6px;background:var(--bg-primary);color:var(--text-primary);" />
      <label style="display:block;font-size:11px;font-weight:600;color:var(--text-secondary);margin-bottom:3px;margin-top:10px;">Steps</label>
      <div id="spStepList"></div>
      <button class="sp-btn" id="spStepAddBtn" style="margin-top:6px;font-size:10px;padding:4px 10px;">+ Add Step</button>
      <div class="sp-btn-row" style="display:flex;gap:6px;margin-top:12px;">
        <button class="sp-btn primary" id="spWfSaveBtn">Save Workflow</button>
        <button class="sp-btn" id="spWfCancelBtn">Cancel</button>
      </div>
    </div>
  </div>
</div>

<!-- Bug Backlog section -->
<div class="bottom-section" id="bugSection">
  <div class="bottom-section-header" data-prompt="Check for outstanding bugs related to current WIP tickets and come up with a plan to fix one or more as it makes sense">
    <span class="toggle-arrow">&#9654;</span>
    <div class="section-dot" style="background: var(--priority-high)"></div>
    <span class="bottom-section-title">Bug Backlog</span>
    <span class="bottom-section-count">{count_bugs}</span>
  </div>
  <div class="bottom-section-body">
{bugs_cards}
  </div>
</div>

<!-- Icebox section -->
<div class="bottom-section" id="iceboxSection">
  <div class="bottom-section-header" data-prompt="Review icebox items — any worth reviving?">
    <span class="toggle-arrow">&#9654;</span>
    <div class="section-dot" style="background: var(--status-icebox)"></div>
    <span class="bottom-section-title">Icebox</span>
    <span class="bottom-section-count">{count_icebox}</span>
  </div>
  <div class="bottom-section-body">
{icebox_cards}
  </div>
</div>

<!-- Done section -->
<div class="bottom-section" id="doneSection">
  <div class="bottom-section-header" data-prompt="Show completed features summary">
    <span class="toggle-arrow">&#9654;</span>
    <div class="section-dot" style="background: var(--status-done)"></div>
    <span class="bottom-section-title">Done</span>
    <span class="bottom-section-count">{count_done}</span>
  </div>
  <div class="bottom-section-body">
{done_cards}
  </div>
</div>

<!-- Won't Do section -->
<div class="bottom-section" id="wontdoSection">
  <div class="bottom-section-header" data-prompt="Review won't-do decisions — any worth reconsidering?">
    <span class="toggle-arrow">&#9654;</span>
    <div class="section-dot" style="background: var(--status-wontdo)"></div>
    <span class="bottom-section-title">Won't Do</span>
    <span class="bottom-section-count">{count_wontdo}</span>
  </div>
  <div class="bottom-section-body">
{wontdo_cards}
  </div>
</div>

<script>
(function() {{
  // Sparkline
  var sparkData = {sparkline_json};
  var maxVal = Math.max.apply(null, sparkData) || 1;
  var sparkWrap = document.querySelector('.sparkline-wrap');
  sparkData.forEach(function(v) {{
    var bar = document.createElement('div');
    bar.className = 'spark-bar';
    bar.style.height = Math.max(1, (v / maxVal) * 18) + 'px';
    sparkWrap.appendChild(bar);
  }});

  // (Moved-card highlighting now handled by live-update diffing below)

  // Auto-scroll to filter bar
  setTimeout(function() {{
    document.getElementById('filterBar').scrollIntoView({{ behavior: 'smooth' }});
  }}, 100);

  // Multi-select filter buttons
  var filterBtns = document.querySelectorAll('.filter-btn');
  var allBtn = document.querySelector('.filter-btn[data-filter="all"]');
  var searchInput = document.getElementById('searchInput');

  function applyFilters() {{
    var activeByGroup = {{}};
    filterBtns.forEach(function(btn) {{
      if (btn.classList.contains('active') && btn.dataset.group !== 'all') {{
        var g = btn.dataset.group;
        if (!activeByGroup[g]) activeByGroup[g] = [];
        activeByGroup[g].push(btn.dataset.filter);
      }}
    }});
    var groups = Object.keys(activeByGroup);
    var noFilters = groups.length === 0;
    if (noFilters) {{ allBtn.classList.add('active'); }} else {{ allBtn.classList.remove('active'); }}

    var allCards = document.querySelectorAll('.card');
    allCards.forEach(function(card) {{
      if (noFilters) {{ card.style.display = ''; }}
      else {{
        var show = true;
        groups.forEach(function(g) {{
          var vals = activeByGroup[g];
          var match = false;
          if (g === 'status') {{ match = vals.indexOf(card.dataset.status) !== -1; }}
          else if (g === 'type') {{ match = card.dataset.isBug === 'true'; }}
          else if (g === 'size') {{ match = vals.indexOf(card.dataset.complexity) !== -1; }}
          else if (g === 'kitchen') {{
            // Multi-select within group is OR. A card matches if ANY chip applies.
            for (var i = 0; i < vals.length; i++) {{
              var v = vals[i];
              if (v === 'auto'        && card.dataset.automationMode === 'auto')   {{ match = true; break; }}
              if (v === 'held'        && card.dataset.automationMode === 'held')   {{ match = true; break; }}
              if (v === 'eligible'    && card.dataset.eligible === 'true')         {{ match = true; break; }}
              if (v === 'needs_input' && card.dataset.runStatus === 'needs_input') {{ match = true; break; }}
              if (v === 'failed'      && (card.dataset.runStatus === 'failed' || card.dataset.runStatus === 'stalled')) {{ match = true; break; }}
            }}
          }}
          if (!match) show = false;
        }});
        card.style.display = show ? '' : 'none';
      }}
    }});
    // Compose search on top of filters
    var q = (searchInput.value || '').toLowerCase().trim();
    if (q) {{
      allCards.forEach(function(card) {{
        if (card.style.display === 'none') return;
        var title = (card.dataset.title || '').toLowerCase();
        var id = (card.dataset.itemId || '').toLowerCase();
        var desc = (card.dataset.desc || '').toLowerCase();
        if (title.indexOf(q) === -1 && id.indexOf(q) === -1 && desc.indexOf(q) === -1) {{
          card.style.display = 'none';
        }}
      }});
    }}
  }}

  filterBtns.forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      if (btn.dataset.group === 'all') {{
        filterBtns.forEach(function(b) {{ b.classList.remove('active'); }});
        allBtn.classList.add('active');
      }} else {{
        btn.classList.toggle('active');
      }}
      applyFilters();
    }});
  }});

  // Search
  searchInput.addEventListener('input', function() {{ applyFilters(); }});

  // Column/section header double-click — copy prompt to clipboard
  document.querySelectorAll('.column-header[data-prompt], .bottom-section-header[data-prompt]').forEach(function(header) {{
    header.style.cursor = 'pointer';
    header.addEventListener('dblclick', function(e) {{
      e.stopPropagation();
      if (this._clickTimer) clearTimeout(this._clickTimer); // cancel section toggle
      var prompt = this.dataset.prompt;
      if (prompt) {{
        navigator.clipboard.writeText(prompt).then(function() {{
          showAppToast('Copied!', 'success', 1200);
        }});
      }}
    }});
  }});

  // Bottom section headers — single click toggles, double click copies prompt
  document.querySelectorAll('.bottom-section-header').forEach(function(header) {{
    header.addEventListener('click', function(e) {{
      if (e.detail === 1) {{
        var self = this;
        this._clickTimer = setTimeout(function() {{
          self.parentElement.classList.toggle('expanded');
        }}, 200);
      }}
    }});
  }});

  // Single click to expand, double click to copy — all cards everywhere
  document.querySelectorAll('.card').forEach(function(el) {{
    el._bound = true;
    el.addEventListener('click', function(e) {{
      e.stopPropagation(); // prevent bubbling to bottom-section-header
      if (e.detail === 1) {{
        var self = this;
        this._clickTimer = setTimeout(function() {{ self.classList.toggle('expanded'); }}, 200);
      }}
    }});
    el.addEventListener('dblclick', function(e) {{
      e.stopPropagation();
      clearTimeout(this._clickTimer);
      var id = this.dataset.itemId;
      var title = this.dataset.title;
      var col = this.dataset.section;
      var text;
      if (col === 'ideas') {{
        text = '/spec ' + id;
      }} else if (col === 'backlog') {{
        text = 'I want to spec out ' + id + ': ' + title + ' — write the description and acceptance criteria';
      }} else if (col === 'review') {{
        text = '/review ' + id;
      }} else if (col === 'bugs') {{
        text = 'We need to come up with a plan to fix this bug ' + id + ': ' + title;
      }} else {{
        text = 'I want to work on ' + id + ': ' + title;
      }}
      navigator.clipboard.writeText(text).then(function() {{
        showAppToast('Copied!', 'success', 1200);
      }});
    }});
  }});

  // Children toggle — collapse/expand child groups
  document.querySelectorAll('.children-toggle').forEach(function(toggle) {{
    toggle._bound = true;
    toggle.addEventListener('click', function(e) {{
      e.stopPropagation();
      var parentId = this.dataset.parent;
      var group = document.querySelector('.child-group[data-parent="' + parentId + '"]');
      if (group) {{
        group.classList.toggle('collapsed');
        this.classList.toggle('collapsed');
      }}
    }});
  }});

  // Live in-place update — fetch, diff, patch (no full reload)
  (function() {{
    var currentTs = document.querySelector('meta[name="gen-ts"]').content;
    var currentSchema = (document.querySelector('meta[name="schema-version"]') || {{}}).content || '0';
    var url = location.href;

    function getCardMap(root) {{
      var map = {{}};
      root.querySelectorAll('[data-item-id]').forEach(function(el) {{ if (!el.closest('.child-group')) map[el.dataset.itemId] = el; }});
      return map;
    }}

    function findContainerSel(el, root) {{
      var p = el;
      while (p && p !== root) {{
        if (p.classList && (p.classList.contains('column-body') || p.classList.contains('bottom-section-body'))) {{
          var sec = p.parentNode;
          if (sec && sec.id) return '#' + sec.id + ' > .' + (p.classList.contains('column-body') ? 'column-body' : 'bottom-section-body');
          break;
        }}
        p = p.parentNode;
      }}
      return null;
    }}

    function patchCards(newDoc) {{
      var oldMap = getCardMap(document);
      var newMap = getCardMap(newDoc);
      var firstMoved = null;

      Object.keys(oldMap).forEach(function(id) {{
        if (!newMap[id]) {{
          var el = oldMap[id];
          el.classList.add('card-exit');
          setTimeout(function() {{ if (el.parentNode) el.remove(); }}, 300);
        }}
      }});

      Object.keys(newMap).forEach(function(id) {{
        var oldEl = oldMap[id], newEl = newMap[id];
        if (!oldEl) return;
        // Skip cards being edited — don't overwrite in-progress edits
        if (oldEl.dataset.editing === 'true') return;
        var oldCol = oldEl.dataset.section, newCol = newEl.dataset.section;
        var wasExpanded = oldEl.classList.contains('expanded');

        if (oldCol !== newCol) {{
          var sel = findContainerSel(newEl, newDoc);
          if (sel) {{
            var target = document.querySelector(sel);
            if (target) {{
              oldEl.dataset.section = newCol;
              oldEl.dataset.title = newEl.dataset.title || '';
              oldEl.dataset.desc = newEl.dataset.desc || '';
              oldEl.className = newEl.className;
              if (wasExpanded) oldEl.classList.add('expanded');
              while (oldEl.firstChild) oldEl.removeChild(oldEl.firstChild);
              Array.from(newEl.childNodes).forEach(function(n) {{ oldEl.appendChild(n.cloneNode(true)); }});
              target.appendChild(oldEl);
              oldEl.classList.add('just-moved');
              oldEl._bound = false;
              if (!firstMoved) firstMoved = oldEl;
            }}
          }}
        }} else if (oldEl.textContent !== newEl.textContent || oldEl.className !== newEl.className) {{
          oldEl.dataset.title = newEl.dataset.title || '';
          oldEl.dataset.desc = newEl.dataset.desc || '';
          oldEl.className = newEl.className;
          if (wasExpanded) oldEl.classList.add('expanded');
          while (oldEl.firstChild) oldEl.removeChild(oldEl.firstChild);
          Array.from(newEl.childNodes).forEach(function(n) {{ oldEl.appendChild(n.cloneNode(true)); }});
          oldEl.classList.add('content-changed');
          oldEl._bound = false;
          setTimeout(function() {{ oldEl.classList.remove('content-changed'); }}, 800);
        }}
      }});

      Object.keys(newMap).forEach(function(id) {{
        if (oldMap[id]) return;
        var newEl = newMap[id];
        var sel = findContainerSel(newEl, newDoc);
        if (sel) {{
          var target = document.querySelector(sel);
          if (target) {{
            var clone = newEl.cloneNode(true);
            clone.classList.add('card-enter');
            target.appendChild(clone);
            if (!firstMoved) firstMoved = clone;
          }}
        }}
      }});

      return firstMoved;
    }}

    function patchCounters(newDoc) {{
      ['.header-stat', '.column-count', '.filter-btn .count', '.bottom-section-count'].forEach(function(sel) {{
        var oldEls = document.querySelectorAll(sel);
        var newEls = newDoc.querySelectorAll(sel);
        oldEls.forEach(function(el, i) {{
          if (newEls[i] && el.textContent !== newEls[i].textContent) el.textContent = newEls[i].textContent;
        }});
      }});
      var oldFill = document.querySelector('.progress-fill'), newFill = newDoc.querySelector('.progress-fill');
      if (oldFill && newFill) oldFill.style.width = newFill.style.width;
      var oldPct = document.querySelector('.progress-pct'), newPct = newDoc.querySelector('.progress-pct');
      if (oldPct && newPct && oldPct.textContent !== newPct.textContent) oldPct.textContent = newPct.textContent;
      var oldDate = document.querySelector('.header-date'), newDate = newDoc.querySelector('.header-date');
      if (oldDate && newDate && oldDate.textContent !== newDate.textContent) oldDate.textContent = newDate.textContent;
    }}

    setInterval(function() {{
      if (document.body.classList.contains('settings-open')) return;
      fetch(url).then(function(r) {{ return r.text(); }}).then(function(html) {{
        var tsMatch = html.match(/<meta name="gen-ts" content="(\\d+)">/);
        if (!tsMatch || tsMatch[1] === currentTs) return;
        var svMatch = html.match(/<meta name="schema-version" content="(\\d+)">/);
        if ((svMatch ? svMatch[1] : '0') !== currentSchema) {{ location.reload(); return; }}
        currentTs = tsMatch[1];
        var newDoc = new DOMParser().parseFromString(html, 'text/html');

        var scrollY = window.scrollY;
        var searchVal = document.getElementById('searchInput').value;
        var activeFilters = [];
        document.querySelectorAll('.filter-btn.active').forEach(function(b) {{
          activeFilters.push(b.dataset.group + ':' + b.dataset.filter);
        }});
        var expandedIds = [];
        document.querySelectorAll('.bottom-section.expanded').forEach(function(s) {{ expandedIds.push(s.id); }});

        var firstChanged = patchCards(newDoc);
        patchCounters(newDoc);

        window.scrollTo(0, scrollY);
        document.getElementById('searchInput').value = searchVal;
        if (searchVal) document.getElementById('searchInput').dispatchEvent(new Event('input'));
        filterBtns.forEach(function(b) {{ b.classList.remove('active'); }});
        activeFilters.forEach(function(key) {{
          var parts = key.split(':');
          var btn = document.querySelector('.filter-btn[data-group="' + parts[0] + '"][data-filter="' + parts[1] + '"]');
          if (btn) btn.classList.add('active');
        }});
        applyFilters();
        expandedIds.forEach(function(sid) {{ var sec = document.getElementById(sid); if (sec && !sec.classList.contains('expanded')) sec.classList.add('expanded'); }});
        rebindCardListeners();
        if (firstChanged) setTimeout(function() {{ firstChanged.scrollIntoView({{ behavior: 'smooth', block: 'center' }}); }}, 100);
      }}).catch(function() {{}});
    }}, 2000);

    function rebindCardListeners() {{
      document.querySelectorAll('.card').forEach(function(el) {{
        if (el._bound) return;
        el._bound = true;
        el.addEventListener('click', function(e) {{
          e.stopPropagation();
          if (e.detail === 1) {{ var self = this; this._clickTimer = setTimeout(function() {{ self.classList.toggle('expanded'); }}, 200); }}
        }});
        el.addEventListener('dblclick', function(e) {{
          e.stopPropagation(); clearTimeout(this._clickTimer);
          var id = this.dataset.itemId, title = this.dataset.title, col = this.dataset.section, text;
          if (col === 'ideas') text = '/spec ' + id;
          else if (col === 'backlog') text = 'I want to spec out ' + id + ': ' + title + ' — write the description and acceptance criteria';
          else if (col === 'review') text = '/review ' + id;
          else if (col === 'bugs') text = 'We need to come up with a plan to fix this bug ' + id + ': ' + title;
          else text = 'I want to work on ' + id + ': ' + title;
          navigator.clipboard.writeText(text).then(function() {{
            showAppToast('Copied!', 'success', 1200);
          }});
        }});
      }});
      // Rebind children toggles
      document.querySelectorAll('.children-toggle').forEach(function(toggle) {{
        if (toggle._bound) return;
        toggle._bound = true;
        toggle.addEventListener('click', function(e) {{
          e.stopPropagation();
          var parentId = this.dataset.parent;
          var group = document.querySelector('.child-group[data-parent="' + parentId + '"]');
          if (group) {{
            group.classList.toggle('collapsed');
            this.classList.toggle('collapsed');
          }}
        }});
      }});
    }}
    rebindCardListeners();

    // --- Quick-edit support (only active when served via serve.py) ---
    var editApiMeta = document.querySelector('meta[name="edit-api"]');
    var EDIT_API = editApiMeta ? editApiMeta.content : null;
    if (EDIT_API) document.body.classList.add('edit-enabled');

    function apiPut(ticketId, body) {{
      if (!EDIT_API) return Promise.reject('No API');
      return fetch(EDIT_API + '/tickets/' + ticketId, {{
        method: 'PUT',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(body)
      }}).then(function(r) {{ return r.json(); }});
    }}

    function apiMove(ticketId, section) {{
      if (!EDIT_API) return Promise.reject('No API');
      return fetch(EDIT_API + '/tickets/' + ticketId + '/move', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ section: section }})
      }}).then(function(r) {{ return r.json(); }});
    }}

    var GATED_SECTIONS = {{ 'Ideas': 1, 'Backlog': 1, 'WIP': 1, 'For Review': 1, 'Done': 1 }};

    function apiGateCheck(ticketId, section) {{
      if (!EDIT_API) return Promise.reject('No API');
      return fetch(EDIT_API + '/tickets/' + ticketId + '/gate-check', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ section: section }})
      }}).then(function(r) {{ return r.json(); }});
    }}

    function setCardGateChecking(card, checking) {{
      if (checking) {{
        card.classList.add('gate-checking');
      }} else {{
        card.classList.remove('gate-checking');
      }}
    }}

    function startGateCheck(ticketId, targetSection) {{
      var card = document.querySelector('[data-item-id="' + ticketId + '"]');
      if (card) setCardGateChecking(card, true);
      // Open overlay INSTANTLY — don't wait for AI
      if (window.openDetailOverlay) {{
        window.openDetailOverlay(ticketId, null);
      }}
      // Check cache first
      var cacheKey = ticketId + ':gate:' + targetSection;
      if (_assessCache[cacheKey]) {{
        if (card) setCardGateChecking(card, false);
        var cached = _assessCache[cacheKey];
        if (window.populateAssessment) window.populateAssessment(cached);
        if (window.showGateBanner) window.showGateBanner(cached, targetSection);
        var gateHash = '#gate/' + ticketId + '/' + encodeURIComponent(targetSection);
        if (window.location.hash !== gateHash) history.replaceState({{ gate: true, ticketId: ticketId, section: targetSection }}, '', gateHash);
        return;
      }}
      // Cache miss — show loading and run AI in background
      setTimeout(function() {{
        if (window.showGateBannerLoading) window.showGateBannerLoading(targetSection);
      }}, 50);
      apiGateCheck(ticketId, targetSection).then(function(data) {{
        _assessCache[cacheKey] = data;  // cache the result
        if (card) setCardGateChecking(card, false);
        // Find first needs-work category to focus on
        var cats = data.categories || {{}};
        var catRMap = {{ D:'description', C:'criteria', T:'tests', R:'reviewed', S:'smoke' }};
        var focusTab = 'description';
        ['D','C','T','R','S'].forEach(function(k) {{
          if (cats[k] && cats[k].status === 'needs-work' && focusTab === 'description') {{
            focusTab = catRMap[k];
          }}
        }});
        // Populate assessment results into already-open overlay
        if (window.populateAssessment) window.populateAssessment(data);
        if (window.showGateBanner) window.showGateBanner(data, targetSection);
        // Set URL hash for gate state
        var gateHash = '#gate/' + ticketId + '/' + encodeURIComponent(targetSection);
        if (window.location.hash !== gateHash) {{
          history.replaceState({{ gate: true, ticketId: ticketId, section: targetSection }}, '', gateHash);
        }}
      }}).catch(function() {{
        if (card) setCardGateChecking(card, false);
        // Hide loading banner on failure
        var banner = document.getElementById('detail-gate-banner');
        if (banner) banner.classList.add('hidden');
        showAppToast('Gate check failed', 'error');
      }});
    }}

    // --- Unified toast system ---
    var _toastTimer = null;
    var _toastPriority = 0; // 0=none, 1=low (success/info/copy), 2=high (error/undo)
    var _toastUndoFn = null;

    function showAppToast(message, type, duration, undoFn) {{
      type = type || 'success';
      var priority = (type === 'error' || type === 'undo') ? 2 : 1;
      // Don't displace higher-priority toast
      if (_toastPriority > priority) return;

      var el = document.getElementById('app-toast');
      var msgEl = document.getElementById('app-toast-msg');
      if (!el || !msgEl) return;

      clearTimeout(_toastTimer);
      _toastPriority = priority;
      _toastUndoFn = undoFn || null;

      // Reset classes
      el.className = 'visible';
      if (type === 'error') el.classList.add('toast-error');
      else if (type === 'undo') el.classList.add('toast-undo');

      // Build content safely using DOM methods
      while (msgEl.firstChild) msgEl.removeChild(msgEl.firstChild);
      msgEl.appendChild(document.createTextNode(message));
      if (type === 'undo' && undoFn) {{
        var undoBtn = document.createElement('button');
        undoBtn.className = 'toast-undo-btn';
        undoBtn.textContent = 'Undo';
        undoBtn.addEventListener('click', function() {{
          if (_toastUndoFn) {{ _toastUndoFn(); _toastUndoFn = null; }}
          el.className = '';
          clearTimeout(_toastTimer);
          _toastPriority = 0;
        }});
        msgEl.appendChild(undoBtn);
      }}

      duration = duration || (type === 'undo' ? 5000 : type === 'error' ? 4000 : 2500);
      _toastTimer = setTimeout(function() {{
        el.className = '';
        _toastPriority = 0;
        _toastUndoFn = null;
      }}, duration);
    }}

    // Backwards-compatible wrappers
    function showToast(el, text) {{ showAppToast(text || 'Saved!', 'success'); }}
    function showUndoToast(text) {{ showAppToast(text, 'success'); }}

    // --- Inline confirm pattern ---
    var _armedConfirm = null;
    var _armedTimer = null;

    function inlineConfirm(btn, opts) {{
      // opts: {{ onConfirm: fn, confirmLabel: str }}
      if (_armedConfirm && _armedConfirm !== btn && _armedConfirm._disarm) {{
        _armedConfirm._disarm();
      }}
      var origHTML = btn.textContent;
      _armedConfirm = btn;

      var wrapper = document.createElement('span');
      wrapper.appendChild(document.createTextNode((opts.confirmLabel || 'Sure?') + ' '));
      var yesBtn = document.createElement('span');
      yesBtn.textContent = 'Yes';
      yesBtn.style.cssText = 'text-decoration:underline;cursor:pointer';
      var noBtn = document.createElement('span');
      noBtn.textContent = 'Cancel';
      noBtn.style.cssText = 'text-decoration:underline;cursor:pointer;margin-left:6px';
      wrapper.appendChild(yesBtn);
      wrapper.appendChild(document.createTextNode(' / '));
      wrapper.appendChild(noBtn);

      while (btn.firstChild) btn.removeChild(btn.firstChild);
      btn.appendChild(wrapper);

      function disarm() {{
        while (btn.firstChild) btn.removeChild(btn.firstChild);
        btn.textContent = origHTML;
        clearTimeout(_armedTimer);
        _armedConfirm = null;
        btn._disarm = null;
      }}
      btn._disarm = disarm;

      yesBtn.addEventListener('click', function(e) {{
        e.stopPropagation();
        disarm();
        opts.onConfirm();
      }});
      noBtn.addEventListener('click', function(e) {{
        e.stopPropagation();
        disarm();
      }});
      _armedTimer = setTimeout(disarm, 3000);
    }}

    window.inlineConfirm = inlineConfirm;

    // --- Confirm modal (for destructive actions with no undo) ---
    function showConfirmModal(title, msg, confirmText, onConfirm) {{
      var modal = document.getElementById('confirm-modal');
      var titleEl = document.getElementById('confirm-modal-title');
      var msgEl = document.getElementById('confirm-modal-msg');
      var cancelBtn = document.getElementById('confirm-modal-cancel');
      var okBtn = document.getElementById('confirm-modal-ok');
      if (!modal) return;
      titleEl.textContent = title;
      msgEl.textContent = msg;
      okBtn.textContent = confirmText || 'Delete';
      modal.style.display = 'flex';

      function close() {{
        modal.style.display = 'none';
        cancelBtn.removeEventListener('click', close);
        okBtn.removeEventListener('click', handleOk);
        modal.removeEventListener('click', handleBackdrop);
      }}
      function handleOk() {{ close(); onConfirm(); }}
      function handleBackdrop(e) {{ if (e.target === modal) close(); }}

      cancelBtn.addEventListener('click', close);
      okBtn.addEventListener('click', handleOk);
      modal.addEventListener('click', handleBackdrop);
    }}

    window.showConfirmModal = showConfirmModal;

    // --- Undo/Redo system (Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y) ---
    var undoStack = [];
    var redoStack = [];
    var MAX_UNDO = 50;

    function pushUndo(ticketId, description, revertFn, redoFn) {{
      if (!EDIT_API) return;
      undoStack.push({{ ticketId: ticketId, description: description, revertFn: revertFn, redoFn: redoFn }});
      if (undoStack.length > MAX_UNDO) undoStack.shift();
      redoStack = []; // new edit clears redo history
      showAppToast(description + '  (Ctrl+Z to undo)', 'undo', 5000, function() {{ performUndo(); }});
    }}

    function performUndo() {{
      if (!undoStack.length) return;
      var state = undoStack.pop();
      state.revertFn().then(function() {{
        redoStack.push(state);
        showAppToast('Undone: ' + state.description, 'success');
      }}).catch(function() {{
        showAppToast('Undo failed', 'error');
      }});
    }}

    function performRedo() {{
      if (!redoStack.length) return;
      var state = redoStack.pop();
      if (state.redoFn) {{
        state.redoFn().then(function() {{
          undoStack.push(state);
          showAppToast('Redone: ' + state.description, 'success');
        }}).catch(function() {{
          showAppToast('Redo failed', 'error');
        }});
      }}
    }}

    // Ctrl+Z = undo, Ctrl+Shift+Z or Ctrl+Y = redo
    if (EDIT_API) {{
      document.addEventListener('keydown', function(e) {{
        var tag = document.activeElement && document.activeElement.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || (document.activeElement && document.activeElement.contentEditable === 'true')) return;
        if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) {{
          if (!undoStack.length) return;
          e.preventDefault();
          performUndo();
        }} else if ((e.ctrlKey || e.metaKey) && (e.key === 'y' || (e.key === 'z' && e.shiftKey))) {{
          if (!redoStack.length) return;
          e.preventDefault();
          performRedo();
        }}
      }});
    }}

    // Priority dot click — cycle high > medium > low > high
    if (EDIT_API) {{
      document.addEventListener('click', function(e) {{
        var dot = e.target.closest('.priority-dot');
        if (!dot) return;
        var card = dot.closest('.card');
        if (!card || !card.dataset.itemId) return;
        e.stopPropagation();
        e.preventDefault();
        clearTimeout(card._clickTimer);
        var cycle = ['high', 'medium', 'low'];
        var current = dot.classList.contains('high') ? 'high' : dot.classList.contains('medium') ? 'medium' : 'low';
        var next = cycle[(cycle.indexOf(current) + 1) % 3];
        dot.className = 'priority-dot ' + next;
        pushUndo(card.dataset.itemId, card.dataset.itemId + ' priority \u2192 ' + next, function() {{
          dot.className = 'priority-dot ' + current;
          return apiPut(card.dataset.itemId, {{ priority: current }});
        }}, function() {{
          dot.className = 'priority-dot ' + next;
          return apiPut(card.dataset.itemId, {{ priority: next }});
        }});
        apiPut(card.dataset.itemId, {{ priority: next }});
      }}, true);

      // Status badge click — show dropdown
      document.addEventListener('click', function(e) {{
        var badge = e.target.closest('.status-badge');
        if (!badge) return;
        var card = badge.closest('.card');
        if (!card || !card.dataset.itemId) return;
        // (child cards are now full cards — no special handling needed)
        e.stopPropagation();
        e.preventDefault();
        clearTimeout(card._clickTimer);
        // Remove any existing dropdown
        var existing = document.querySelector('.status-dropdown');
        if (existing) existing.remove();
        var oldStatus = badge.textContent.trim();
        // Create dropdown
        var statuses = {json.dumps(STATUSES)};
        var dd = document.createElement('div');
        dd.className = 'status-dropdown';
        dd.style.cssText = 'position:absolute;z-index:100;background:var(--bg-card);border:1px solid var(--border-main);border-radius:6px;padding:4px 0;min-width:130px;box-shadow:0 4px 12px rgba(0,0,0,.4);';
        statuses.forEach(function(s) {{
          var opt = document.createElement('div');
          opt.className = 'status-dropdown-opt';
          opt.textContent = s;
          opt.style.cssText = 'padding:3px 10px;font-size:11px;cursor:pointer;color:var(--text-secondary);';
          opt.addEventListener('mouseenter', function() {{ this.style.background = 'var(--bg-hover)'; }});
          opt.addEventListener('mouseleave', function() {{ this.style.background = ''; }});
          opt.addEventListener('click', function(ev) {{
            ev.stopPropagation();
            dd.remove();
            badge.className = 'status-badge ' + s;
            badge.textContent = s;
            pushUndo(card.dataset.itemId, card.dataset.itemId + ' status \u2192 ' + s, function() {{
              badge.className = 'status-badge ' + oldStatus;
              badge.textContent = oldStatus;
              return apiPut(card.dataset.itemId, {{ status: oldStatus }});
            }}, function() {{
              badge.className = 'status-badge ' + s;
              badge.textContent = s;
              return apiPut(card.dataset.itemId, {{ status: s }});
            }});
            apiPut(card.dataset.itemId, {{ status: s }});
          }});
          dd.appendChild(opt);
        }});
        badge.style.position = 'relative';
        badge.parentElement.style.position = 'relative';
        badge.parentElement.appendChild(dd);
        // Close on outside click
        setTimeout(function() {{
          document.addEventListener('click', function closer() {{
            dd.remove();
            document.removeEventListener('click', closer);
          }}, {{ once: true }});
        }}, 0);
      }}, true);

      // Acceptance criteria — checkbox toggle, text click-to-edit, add/remove
      document.addEventListener('click', function(e) {{
        var card;
        // Handle add criterion button
        var addBtn = e.target.closest('.add-criterion-btn');
        if (addBtn) {{
          card = addBtn.closest('.card');
          if (!card || !card.dataset.itemId) return;
          e.stopPropagation(); e.preventDefault();
          var input = document.createElement('input');
          input.type = 'text';
          input.placeholder = 'New criterion...';
          input.style.cssText = 'font-size:11px;padding:2px 4px;border:1px solid var(--border-default);background:var(--bg-page);color:var(--text-primary);border-radius:3px;width:100%;outline:none;margin-bottom:4px;';
          addBtn.parentElement.insertBefore(input, addBtn);
          input.focus();
          card.dataset.editing = 'true';
          function addSave() {{
            var text = input.value.trim();
            card.dataset.editing = '';
            if (text) {{
              apiPut(card.dataset.itemId, {{ add_criteria: text }}).then(function() {{ showToast(card, 'Added'); }});
            }}
            if (input.parentNode) input.remove();
          }}
          input.addEventListener('blur', function() {{ setTimeout(addSave, 100); }});
          input.addEventListener('keydown', function(ev) {{
            if (ev.key === 'Enter') input.blur();
            if (ev.key === 'Escape') {{ card.dataset.editing = ''; input.remove(); }}
          }});
          return;
        }}
        // Handle remove criterion button
        var removeBtn = e.target.closest('.remove-criterion-btn');
        if (removeBtn) {{
          card = removeBtn.closest('.card');
          if (!card || !card.dataset.itemId) return;
          e.stopPropagation(); e.preventDefault();
          var criteriaContainer = removeBtn.closest('.card-criteria');
          var allCriteria = criteriaContainer.querySelectorAll('.criterion');
          var criterion = removeBtn.closest('.criterion');
          var idx = Array.prototype.indexOf.call(allCriteria, criterion);
          if (idx >= 0) {{
            criterion.style.opacity = '0.3';
            apiPut(card.dataset.itemId, {{ remove_criterion: idx }}).then(function() {{
              showToast(card, 'Removed');
            }});
          }}
          return;
        }}
        // Handle criterion click
        var criterion = e.target.closest('.criterion');
        if (!criterion) return;
        card = criterion.closest('.card');
        if (!card || !card.dataset.itemId) return;
        e.stopPropagation(); e.preventDefault();
        clearTimeout(card._clickTimer);
        var criteriaContainer = criterion.closest('.card-criteria');
        if (!criteriaContainer) return;
        var allCriteria = criteriaContainer.querySelectorAll('.criterion');
        var idx = Array.prototype.indexOf.call(allCriteria, criterion);
        if (idx < 0) return;
        // Detect if click was on the checkbox marker (first 2 chars) or the text
        var clickX = e.clientX - criterion.getBoundingClientRect().left;
        if (clickX < 20) {{
          // Checkbox toggle
          var isChecked = criterion.classList.contains('checked');
          criterion.classList.toggle('checked');
          var newMarker = isChecked ? '\u2610 ' : '\u2611 ';
          criterion.textContent = newMarker + criterion.textContent.substring(2);
          pushUndo(card.dataset.itemId, card.dataset.itemId + ' criterion ' + (isChecked ? 'unchecked' : 'checked'), function() {{
            criterion.classList.toggle('checked');
            var revertMarker = !isChecked ? '\u2610 ' : '\u2611 ';
            criterion.textContent = revertMarker + criterion.textContent.substring(2);
            return apiPut(card.dataset.itemId, {{ toggle_criterion: idx }});
          }}, function() {{
            criterion.classList.toggle('checked');
            var redoMarker = isChecked ? '\u2610 ' : '\u2611 ';
            criterion.textContent = redoMarker + criterion.textContent.substring(2);
            return apiPut(card.dataset.itemId, {{ toggle_criterion: idx }});
          }});
          apiPut(card.dataset.itemId, {{ toggle_criterion: idx }});
        }} else {{
          // Text click-to-edit
          var origText = criterion.textContent.substring(2).trim();
          var marker = criterion.textContent.substring(0, 2);
          var input = document.createElement('input');
          input.type = 'text';
          input.value = origText;
          input.style.cssText = 'font-size:11px;padding:1px 4px;border:1px solid var(--border-default);background:var(--bg-page);color:var(--text-primary);border-radius:3px;flex:1;outline:none;';
          card.dataset.editing = 'true';
          criterion.textContent = marker;
          criterion.appendChild(input);
          var removeBtn2 = document.createElement('span');
          removeBtn2.className = 'remove-criterion-btn';
          removeBtn2.textContent = '\u00d7';
          removeBtn2.style.cssText = 'cursor:pointer;color:var(--text-tertiary);margin-left:4px;font-size:14px;';
          criterion.appendChild(removeBtn2);
          input.focus();
          function textSave() {{
            var val = input.value.trim();
            card.dataset.editing = '';
            criterion.textContent = marker + (val || origText);
            if (val && val !== origText) {{
              apiPut(card.dataset.itemId, {{ criterion_index: idx, criterion_text: val }}).then(function() {{
                showToast(card, 'Saved');
              }});
            }}
          }}
          input.addEventListener('blur', function() {{ setTimeout(textSave, 100); }});
          input.addEventListener('keydown', function(ev) {{
            if (ev.key === 'Enter') input.blur();
            if (ev.key === 'Escape') {{ criterion.textContent = marker + origText; card.dataset.editing = ''; }}
          }});
        }}
      }}, true);

      // --- Complexity badge click — show S/M/L/XL select ---
      document.addEventListener('click', function(e) {{
        var badge = e.target.closest('.complexity-badge');
        if (!badge) return;
        var card = badge.closest('.card');
        if (!card || !card.dataset.itemId) return;
        if (e.target.closest('.linked-child-card')) return;
        e.stopPropagation();
        e.preventDefault();
        clearTimeout(card._clickTimer);
        var existing = document.querySelector('.complexity-dropdown');
        if (existing) existing.remove();
        var oldComplexity = badge.textContent.trim();
        var sizes = ['S', 'M', 'L', 'XL'];
        var dd = document.createElement('div');
        dd.className = 'complexity-dropdown';
        dd.style.cssText = 'position:absolute;z-index:100;background:var(--bg-card);border:1px solid var(--border-main);border-radius:6px;padding:4px 0;min-width:60px;box-shadow:0 4px 12px rgba(0,0,0,.4);';
        sizes.forEach(function(sz) {{
          var opt = document.createElement('div');
          opt.textContent = sz;
          opt.style.cssText = 'padding:3px 10px;font-size:11px;cursor:pointer;color:var(--text-secondary);text-align:center;';
          opt.addEventListener('mouseenter', function() {{ this.style.background = 'var(--bg-hover)'; }});
          opt.addEventListener('mouseleave', function() {{ this.style.background = ''; }});
          opt.addEventListener('click', function(ev) {{
            ev.stopPropagation();
            dd.remove();
            badge.textContent = sz;
            card.dataset.complexity = sz;
            pushUndo(card.dataset.itemId, card.dataset.itemId + ' complexity \u2192 ' + sz, function() {{
              badge.textContent = oldComplexity;
              card.dataset.complexity = oldComplexity;
              return apiPut(card.dataset.itemId, {{ complexity: oldComplexity }});
            }}, function() {{
              badge.textContent = sz;
              card.dataset.complexity = sz;
              return apiPut(card.dataset.itemId, {{ complexity: sz }});
            }});
            apiPut(card.dataset.itemId, {{ complexity: sz }});
          }});
          dd.appendChild(opt);
        }});
        badge.parentElement.style.position = 'relative';
        badge.parentElement.appendChild(dd);
        setTimeout(function() {{
          document.addEventListener('click', function closer() {{
            dd.remove();
            document.removeEventListener('click', closer);
          }}, {{ once: true }});
        }}, 0);
      }}, true);

      // --- Click-to-edit for parent link ---
      document.addEventListener('click', function(e) {{
        var parentEl = e.target.closest('.card-parent-link');
        if (!parentEl) return;
        var card = parentEl.closest('.card');
        if (!card || !card.dataset.itemId) return;
        e.stopPropagation();
        e.preventDefault();
        clearTimeout(card._clickTimer);
        if (parentEl.querySelector('input')) return;
        card.dataset.editing = 'true';
        var currentVal = parentEl.classList.contains('empty') ? '' : (parentEl.textContent.replace(/^\u21b3\\s*/, '').trim());
        var input = document.createElement('input');
        input.type = 'text';
        input.value = currentVal;
        input.placeholder = 'Parent ticket ID...';
        input.style.cssText = 'font-size:10px;padding:2px 4px;border:1px solid var(--border-default);background:var(--bg-page);color:var(--text-primary);border-radius:3px;width:80px;outline:none;';
        parentEl.textContent = '';
        parentEl.appendChild(input);
        input.focus();
        showAutocomplete(input, function(selId) {{ input.value = selId; input.blur(); }});
        function save() {{
          var val = input.value.trim();
          parentEl.textContent = val ? '\u21b3 ' + val : '+ parent';
          parentEl.classList.toggle('empty', !val);
          card.dataset.editing = '';
          apiPut(card.dataset.itemId, {{ parent: val || null }}).then(function() {{
            showToast(card, val ? 'parent: ' + val : 'parent cleared');
          }});
        }}
        input.addEventListener('blur', function() {{ setTimeout(save, 150); }});
        input.addEventListener('keydown', function(ev) {{
          if (ev.key === 'Enter') input.blur();
          if (ev.key === 'Escape') {{ parentEl.textContent = currentVal ? '\u21b3 ' + currentVal : '+ parent'; parentEl.classList.toggle('empty', !currentVal); card.dataset.editing = ''; }};
        }});
      }}, true);

      // --- Autocomplete utility ---
      function showAutocomplete(input, onSelect) {{
        var allIds = [];
        document.querySelectorAll('[data-item-id]').forEach(function(el) {{
          var id = el.dataset.itemId;
          var title = el.dataset.title || '';
          if (allIds.findIndex(function(x) {{ return x.id === id; }}) === -1) allIds.push({{id: id, title: title}});
        }});
        var dd = null;
        function render(filter) {{
          if (dd) dd.remove();
          var matches = allIds.filter(function(x) {{ return x.id.toLowerCase().indexOf(filter) >= 0 || x.title.toLowerCase().indexOf(filter) >= 0; }}).slice(0, 8);
          if (!matches.length || !filter) return;
          dd = document.createElement('div');
          dd.className = 'autocomplete-dropdown';
          dd.style.cssText = 'position:absolute;z-index:200;background:var(--bg-card);border:1px solid var(--border-default);border-radius:6px;max-height:150px;overflow-y:auto;box-shadow:0 4px 12px rgba(0,0,0,.4);min-width:120px;';
          matches.forEach(function(m) {{
            var opt = document.createElement('div');
            opt.className = 'autocomplete-opt';
            opt.style.cssText = 'padding:4px 10px;font-size:11px;cursor:pointer;color:var(--text-secondary);';
            opt.textContent = m.id + (m.title ? ' — ' + m.title.substring(0, 30) : '');
            opt.addEventListener('mousedown', function(ev) {{ ev.preventDefault(); onSelect(m.id); if (dd) dd.remove(); dd = null; }});
            opt.addEventListener('mouseenter', function() {{ this.style.background = 'var(--bg-hover)'; }});
            opt.addEventListener('mouseleave', function() {{ this.style.background = ''; }});
            dd.appendChild(opt);
          }});
          input.parentElement.style.position = 'relative';
          input.parentElement.appendChild(dd);
        }}
        input.addEventListener('input', function() {{ render(input.value.trim().toLowerCase()); }});
        input.addEventListener('blur', function() {{ setTimeout(function() {{ if (dd) dd.remove(); dd = null; }}, 200); }});
      }}

      // --- Drag-to-move ---
      var dragId = null;
      document.addEventListener('dragstart', function(e) {{
        var card = e.target.closest('.card');
        if (!card || !card.dataset.itemId) return;
        dragId = card.dataset.itemId;
        card.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', dragId);
      }});
      document.addEventListener('dragend', function(e) {{
        var card = e.target.closest('.card');
        if (card) card.classList.remove('dragging');
        document.querySelectorAll('.drag-over').forEach(function(el) {{ el.classList.remove('drag-over'); }});
        dragId = null;
      }});
      document.querySelectorAll('.column, .bottom-section').forEach(function(zone) {{
        zone.addEventListener('dragover', function(e) {{
          e.preventDefault();
          e.dataTransfer.dropEffect = 'move';
          zone.classList.add('drag-over');
        }});
        zone.addEventListener('dragleave', function(e) {{
          if (!zone.contains(e.relatedTarget)) zone.classList.remove('drag-over');
        }});
        zone.addEventListener('drop', function(e) {{
          e.preventDefault();
          zone.classList.remove('drag-over');
          var id = e.dataTransfer.getData('text/plain');
          if (!id) return;
          var section = null;
          if (zone.dataset && zone.dataset.col) {{
            // Kanban column
            var colMap = {{ ideas: 'Ideas', backlog: 'Backlog', wip: 'WIP', review: 'For Review' }};
            section = colMap[zone.dataset.col];
          }} else if (zone.id) {{
            // Bottom section
            var secMap = {{ bugSection: 'Bugs', iceboxSection: 'Icebox', doneSection: 'Done', wontdoSection: "Won't Do" }};
            section = secMap[zone.id];
          }}
          if (section) {{
            if (GATED_SECTIONS[section]) {{
              startGateCheck(id, section);
            }} else {{
              apiMove(id, section).then(function() {{
                showToast(document.querySelector('[data-item-id="' + id + '"]'), 'Moved!');
              }});
            }}
          }}
        }});
      }});
      // Make cards draggable
      document.querySelectorAll('.card').forEach(function(c) {{ c.setAttribute('draggable', 'true'); }});

      // --- Card-on-card drop (set parent) ---
      document.addEventListener('dragover', function(e) {{
        if (!dragId) return;
        var card = e.target.closest('.card');
        if (!card || !card.dataset.itemId || card.dataset.itemId === dragId) return;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = 'link';
        card.classList.add('drag-target');
      }}, true);
      document.addEventListener('dragleave', function(e) {{
        var card = e.target.closest('.card');
        if (card) card.classList.remove('drag-target');
      }});
      document.addEventListener('drop', function(e) {{
        var card = e.target.closest('.card');
        if (!card || !card.dataset.itemId || !dragId) return;
        var targetId = card.dataset.itemId;
        var childId = dragId;
        if (targetId === childId) return;
        // Don't allow circular — check if target is already a child of dragged
        var targetParent = card.closest('[data-item-id="' + childId + '"]');
        if (targetParent) return;
        e.preventDefault();
        e.stopPropagation();
        card.classList.remove('drag-target');
        apiPut(childId, {{ parent: targetId }}).then(function() {{
          showToast(card, childId + ' \u2192 child');
        }});
      }}, true);

      // --- Click-to-edit for text fields (title, description) ---
      document.addEventListener('click', function(e) {{
        var titleEl = e.target.closest('.card-title');
        var descEl = e.target.closest('.card-desc');
        var target = titleEl || descEl;
        if (!target) return;
        var card = target.closest('.card');
        if (!card || !card.dataset.itemId) return;
        // Title editable on collapsed cards; desc only when expanded
        if (descEl && !card.classList.contains('expanded')) return;
        e.stopPropagation();
        e.preventDefault();
        clearTimeout(card._clickTimer);
        if (target.contentEditable === 'true') return;
        card.dataset.editing = 'true';
        target.contentEditable = 'true';
        target.focus();
        var range = document.createRange();
        range.selectNodeContents(target);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        var origValue = target.textContent.trim();
        function save() {{
          target.contentEditable = 'false';
          card.dataset.editing = '';
          var field = titleEl ? 'title' : 'description';
          var value = target.textContent.trim();
          if (value === origValue) {{
            target.removeEventListener('blur', save);
            target.removeEventListener('keydown', keyHandler);
            return;
          }}
          if (field === 'title') card.dataset.title = value;
          if (field === 'description') card.dataset.desc = value;
          var body = {{}};
          body[field] = value;
          pushUndo(card.dataset.itemId, card.dataset.itemId + ' ' + field + ' updated', function() {{
            target.textContent = origValue;
            if (field === 'title') card.dataset.title = origValue;
            if (field === 'description') card.dataset.desc = origValue;
            var revertBody = {{}};
            revertBody[field] = origValue;
            return apiPut(card.dataset.itemId, revertBody);
          }}, function() {{
            target.textContent = value;
            if (field === 'title') card.dataset.title = value;
            if (field === 'description') card.dataset.desc = value;
            var redoBody = {{}};
            redoBody[field] = value;
            return apiPut(card.dataset.itemId, redoBody);
          }});
          apiPut(card.dataset.itemId, body).then(function() {{
            showToast(card, 'Saved');
          }});
          target.removeEventListener('blur', save);
          target.removeEventListener('keydown', keyHandler);
        }}
        function keyHandler(ev) {{
          if (ev.key === 'Enter' && !ev.shiftKey) {{ ev.preventDefault(); target.blur(); }}
          if (ev.key === 'Escape') {{ target.textContent = origValue; target.blur(); }}
        }}
        target.addEventListener('blur', save);
        target.addEventListener('keydown', keyHandler);
      }}, true);

      // --- Readiness dot click → opens detail overlay (handled in overlay script below) ---

      // --- Workflow action buttons ---
      document.addEventListener('click', function(e) {{
        var btn = e.target.closest('.action-btn');
        if (!btn) return;
        var card = btn.closest('.card');
        if (!card || !card.dataset.itemId) return;
        e.stopPropagation();
        e.preventDefault();
        clearTimeout(card._clickTimer);
        var action = btn.dataset.action;
        var id = card.dataset.itemId;
        if (action === 'move') {{
          var section = btn.dataset.section;
          if (GATED_SECTIONS[section]) {{
            startGateCheck(id, section);
          }} else {{
            apiMove(id, section).then(function() {{ showToast(card, 'Moved!'); }});
          }}
        }} else if (action === 'accept') {{
          startGateCheck(id, 'Done');
        }}
      }}, true);

      // --- New ticket panel ---
      var newBtn = document.getElementById('newTicketBtn');
      var newPanel = document.getElementById('newTicketPanel');
      var newTitle = document.getElementById('newTicketTitle');
      var newSection = document.getElementById('newTicketSection');
      var newSubmit = document.getElementById('newTicketSubmit');

      if (newBtn) {{
        newBtn.addEventListener('click', function() {{
          var open = newPanel.style.display !== 'none';
          newPanel.style.display = open ? 'none' : 'block';
          if (!open) setTimeout(function() {{ newTitle.focus(); }}, 50);
        }});
      }}

      function submitNewTicket() {{
        var title = newTitle.value.trim();
        if (!title) return;
        newSubmit.disabled = true;
        fetch(EDIT_API + '/tickets', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ title: title, section: newSection.value }})
        }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
          newTitle.value = '';
          newSubmit.disabled = false;
          newTitle.focus();
        }}).catch(function() {{ newSubmit.disabled = false; }});
      }}

      if (newSubmit) newSubmit.addEventListener('click', submitNewTicket);
      if (newTitle) newTitle.addEventListener('keydown', function(e) {{
        if (e.key === 'Enter') {{ e.preventDefault(); submitNewTicket(); }}
      }});
    }}

    // Open button — works in both server mode and file:// mode
    document.addEventListener('click', function(e) {{
      var btn = e.target.closest('.card-open-btn');
      if (!btn) return;
      var card = btn.closest('.card');
      if (!card || !card.dataset.itemId) return;
      e.stopPropagation();
      e.preventDefault();
      clearTimeout(card._clickTimer);
      if (window.openDetailOverlay) window.openDetailOverlay(card.dataset.itemId, null);
    }}, true);

    // Expose for overlay gate-check integration and testability
    window.showToast = showToast;
    window.showAppToast = showAppToast;
    window.startGateCheck = startGateCheck;
  }})();
}})();
</script>

<!-- Ticket detail screen -->
<div id="ticket-detail-overlay" class="detail-overlay hidden" role="dialog" aria-modal="true" data-testid="detail-overlay">
  <div class="detail-backdrop"></div>
  <div class="detail-panel">
    <div class="detail-header">
      <span class="detail-id"></span>
      <span class="detail-title" contenteditable="false" title="Click to rename" data-testid="detail-title"></span>
      <span class="detail-path"></span>
      <div class="detail-dctrs-strip">
        {_dctrs_icons}
      </div>
      <button class="detail-record-btn" id="detail-record-btn" style="display:none" title="Record feedback session">{_svg_icon("mic", 14)} Record</button>
      <button class="detail-close" aria-label="Close ticket detail" data-testid="detail-close">{_icon_close}</button>
    </div>
    <div class="detail-meta-strip">
      <span class="meta-chip meta-chip--priority" title="Click to change priority"><span class="chip-dot"></span><span class="chip-text"></span></span>
      <span class="meta-chip meta-chip--status" title="Click to change status" data-testid="detail-status"><span class="chip-text"></span></span>
      <span class="meta-chip meta-chip--complexity" title="Click to change complexity"><span class="chip-text"></span></span>
      <span class="meta-chip meta-chip--parent"><span class="chip-label">Parent:</span> <span class="chip-value">None</span></span>
      <span class="meta-chip meta-chip--section"><span class="chip-text"></span></span>
      <!-- Kitchen automation toggle (M1a) — Manual / Auto / Held + hold reason -->
      <span class="meta-chip meta-chip--automation" title="Automation mode" data-testid="detail-automation">
        <span class="chip-label">Auto:</span> <span class="chip-value">Manual</span>
      </span>
    </div>
    <div class="detail-body">
      <!-- Live run panel (M3) — hidden when no runs exist -->
      <div class="detail-section detail-runs hidden" id="section-runs" data-testid="section-runs">
        <div class="detail-section-header">
          <h3>Runs <span id="runs-count" style="font-weight:400;opacity:0.6;font-size:11px;"></span></h3>
          <button class="run-now-btn" id="run-now-btn" data-testid="run-now-btn" style="display:none">{_svg_icon("play", 11)} Run now</button>
        </div>
        <!-- Latest run card -->
        <div id="run-latest-card" class="run-card" style="display:none">
          <div class="run-card-top">
            <span class="run-pill" id="run-latest-pill"></span>
            <span class="run-summary" id="run-latest-summary"></span>
          </div>
          <div class="run-meta">
            <span id="run-latest-time"></span>
            <span id="run-latest-duration"></span>
            <a id="run-latest-ws" class="run-ws-link" href="#" target="_blank" rel="noopener noreferrer">—</a>
          </div>
          <!-- needs_input inline response panel -->
          <div class="run-ni-panel hidden" id="run-ni-panel" data-testid="run-ni-panel">
            <div class="run-ni-prompt" id="run-ni-prompt"></div>
            <textarea class="run-ni-textarea" id="run-ni-textarea" data-testid="run-ni-textarea" placeholder="Type your response…" rows="3"></textarea>
            <div class="run-ni-actions">
              <button class="run-ni-send" id="run-ni-send" data-testid="run-ni-send" disabled>{_svg_icon("send", 11)} Send response</button>
              <button class="run-ni-cancel" id="run-ni-cancel" data-testid="run-ni-cancel">Cancel</button>
            </div>
          </div>
          <!-- Action buttons -->
          <div class="run-actions" id="run-latest-actions">
            <button class="run-action-btn danger" id="run-stop-btn" style="display:none" data-testid="run-stop">{_svg_icon("square", 11)} Stop</button>
            <button class="run-action-btn" id="run-retry-btn" style="display:none" data-testid="run-retry">{_svg_icon("rotate-ccw", 11)} Retry</button>
            <button class="run-action-btn" id="run-retry-fresh-btn" style="display:none" data-testid="run-retry-fresh">{_svg_icon("rotate-ccw", 11)} Retry fresh</button>
            <button class="run-action-btn" id="run-file-gap-btn" style="display:none" data-testid="run-file-gap" title="File a draft ticket from this gap and link to the journey">{_svg_icon("plus", 11)} File gap ticket</button>
            <button class="run-action-btn danger" id="run-discard-btn" style="display:none" data-testid="run-discard">{_svg_icon("trash-2", 11)} Discard</button>
          </div>
          <!-- Inline discard confirm -->
          <div class="run-discard-confirm hidden" id="run-discard-confirm">
            <span>Discard this run?</span>
            <button class="danger" id="run-discard-yes" data-testid="run-discard-yes">Yes, discard</button>
            <button id="run-discard-no" data-testid="run-discard-no">Cancel</button>
          </div>
        </div>
        <!-- Recent runs collapsed list (max 5 prior entries) -->
        <div id="run-history-wrapper" style="margin-top:4px;">
          <button class="run-history-toggle" id="run-history-toggle" data-testid="run-history-toggle" style="display:none">Show recent runs</button>
          <div class="run-history-list" id="run-history-list"></div>
        </div>
      </div>

      <!-- Gate banner (shown during column moves) -->
      <div class="detail-gate-banner hidden" id="detail-gate-banner">
        <div class="detail-gate-verdict">
          <span class="gate-verdict-badge" id="gate-banner-badge"></span>
          <span class="detail-gate-summary" id="gate-banner-summary"></span>
        </div>
        <div class="detail-gate-actions">
          <button class="detail-gate-confirm" id="gate-banner-confirm"></button>
          <button class="detail-gate-cancel" id="gate-banner-cancel">Keep here</button>
        </div>
      </div>

      <!-- Description -->
      <div class="detail-section" data-section="description" id="section-description">
        <div class="detail-section-header">
          <h3><span class="section-flag" data-cat="D">D</span> Description</h3>
          <button class="section-assess-btn" data-cat="D">Assess</button>
        </div>
        <div class="detail-assess-loading hidden" data-cat-loading="D">Assessing description...</div>
        <div class="detail-assessment hidden" data-cat-result="D"></div>
        <textarea class="detail-editor desc-editor" data-field="description" data-testid="detail-description" placeholder="No description yet. Click to write one."></textarea>
      </div>

      <!-- Acceptance Criteria -->
      <div class="detail-section" data-section="criteria" id="section-criteria">
        <div class="detail-section-header">
          <h3><span class="section-flag" data-cat="C">C</span> Acceptance Criteria</h3>
          <button class="section-assess-btn" data-cat="C">Assess</button>
        </div>
        <div class="detail-assess-loading hidden" data-cat-loading="C">Assessing criteria...</div>
        <div class="detail-assessment hidden" data-cat-result="C"></div>
        <ul class="detail-criteria-list"></ul>
        <input type="text" class="criteria-add-input" placeholder="+ Add criterion and press Enter">
      </div>

      <!-- Smoke -->
      <div class="detail-section" data-section="smoke" id="section-smoke">
        <div class="detail-section-header">
          <h3><span class="section-flag" data-cat="S">S</span> Smoke</h3>
          <button class="section-assess-btn" data-cat="S">Assess</button>
        </div>
        <div class="detail-assess-loading hidden" data-cat-loading="S">Assessing smoke tests...</div>
        <div class="detail-assessment hidden" data-cat-result="S"></div>
        <ul class="detail-criteria-list" data-list-field="smoke"></ul>
        <input type="text" class="criteria-add-input" data-list-add="smoke" placeholder="+ Add smoke test and press Enter">
      </div>

      <!-- Tests -->
      <div class="detail-section" data-section="tests" id="section-tests">
        <div class="detail-section-header">
          <h3><span class="section-flag" data-cat="T">T</span> Tests</h3>
          <button class="section-assess-btn" data-cat="T">Assess</button>
        </div>
        <div class="detail-assess-loading hidden" data-cat-loading="T">Assessing tests...</div>
        <div class="detail-assessment hidden" data-cat-result="T"></div>
        <ul class="detail-criteria-list" data-list-field="tests"></ul>
        <input type="text" class="criteria-add-input" data-list-add="tests" placeholder="+ Add test item and press Enter">
        <!-- Kitchen no-test-required bypass (M1a) -->
        <div class="ntr-block" data-testid="ntr-block">
          <label class="ntr-checkbox-row">
            <input type="checkbox" id="ntr-checkbox" data-testid="ntr-checkbox">
            <span>No tests required</span>
          </label>
          <textarea id="ntr-note" data-testid="ntr-note" class="ntr-note hidden"
                    placeholder="Why no tests? (required — e.g. 'pure docs change', 'config-only')"
                    rows="2"></textarea>
        </div>
      </div>

      <!-- Learnings -->
      <div class="detail-section" data-section="reviewed" id="section-reviewed">
        <div class="detail-section-header">
          <h3><span class="section-flag" data-cat="R">L</span> Learnings / Sync</h3>
          <button class="section-assess-btn" data-cat="R">Assess</button>
        </div>
        <div class="detail-assess-loading hidden" data-cat-loading="R">Assessing review...</div>
        <div class="detail-assessment hidden" data-cat-result="R"></div>
        <textarea class="detail-editor" data-field="reviewed" placeholder="Learnings, sync notes, and decisions captured along the way..."></textarea>
      </div>

      <!-- Attachments -->
      <div class="detail-section" id="section-attachments">
        <div class="detail-section-header">
          <h3>Attachments</h3>
          <div class="attachments-actions">
            <button class="link-session-btn" id="link-session-btn" style="display:none">+ Link</button>
          </div>
        </div>
        <div id="attachments-list" class="attachments-list"></div>
      </div>

      <!-- Workflows -->
      <div class="detail-section" id="section-workflow">
        <div class="detail-section-header">
          <h3>Workflows</h3>
          <div class="workflow-actions">
            <select id="workflow-select" class="workflow-select">
              <option value="">Select workflow...</option>
            </select>
            <button id="workflow-run-btn" class="workflow-run-btn" disabled>Run</button>
          </div>
        </div>
        <div id="workflow-runs-list" class="workflow-runs-list"></div>
      </div>

      <!-- Kitchen activity history (M1b) — newest first; collapsed by default -->
      <div class="detail-section" id="section-history">
        <div class="detail-section-header">
          <h3>History</h3>
          <button class="section-assess-btn" id="history-toggle" data-testid="history-toggle">Show</button>
        </div>
        <div id="history-list" class="history-list hidden"></div>
      </div>

    </div>
  </div>
</div>

<script>
(function() {{
  var editApiMeta = document.querySelector('meta[name="edit-api"]');
  var EDIT_API = editApiMeta ? editApiMeta.content : null;
  if (!EDIT_API) return;

  var overlay = document.getElementById('ticket-detail-overlay');
  if (!overlay) return;
  var idEl = overlay.querySelector('.detail-id');
  var titleEl = overlay.querySelector('.detail-title');
  var currentTicketId = null;
  var currentData = null;
  var _hasAssessmentData = false;
  var _gateContext = null;
  var _editingField = null;
  var _assessCache = {{}};  // keyed by ticketId:gate:section or ticketId:cat:D/C/T/R/S

  var FLAG_NAMES = {{ description:'Description', criteria:'Acceptance Criteria', tests:'Tests', reviewed:'Learnings', smoke:'Smoke Tests' }};
  var CAT_MAP = {{ description:'D', criteria:'C', tests:'T', reviewed:'R', smoke:'S' }};
  var CAT_RMAP = {{ D:'description', C:'criteria', T:'tests', R:'reviewed', S:'smoke', L:'reviewed' }};
  var TAB_COMPAT = {{ properties: null, description: 'D', criteria: 'C', tests: 'T', reviewed: 'R', smoke: 'S' }};
  var PRIORITY_CYCLE = ['high', 'medium', 'low'];
  var COMPLEXITY_CYCLE = ['S', 'M', 'L', 'XL'];
  var STATUS_OPTIONS = {json.dumps(STATUSES)};

  var gateBanner = document.getElementById('detail-gate-banner');
  var gateBadge = document.getElementById('gate-banner-badge');
  var gateSummary = document.getElementById('gate-banner-summary');
  var gateConfirm = document.getElementById('gate-banner-confirm');
  var gateCancel = document.getElementById('gate-banner-cancel');

  function toast(msg) {{ showAppToast(msg, 'success'); }}

  /* --- Auto-save helper --- */
  function autosaveField(field, value) {{
    if (!currentTicketId) return Promise.resolve();
    var body = {{}}; body[field] = value;
    return fetch(EDIT_API+'/tickets/'+currentTicketId, {{method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}})
      .then(function(r){{return r.json();}})
      .then(function(u) {{
        if(u) {{ currentData = u; idEl.textContent = u.id; titleEl.textContent = u.title; }}
        return u;
      }});
  }}

  /* --- DCTRS dots in header --- */
  function refreshDCTRS(data) {{
    if (!data) return;
    var fl = data.readiness_flags || {{}};
    var dots = overlay.querySelectorAll('.detail-dctrs-strip .readiness-dot');
    dots.forEach(function(d) {{
      var flag = d.dataset.flag;
      var ok = flag === 'description' ? !!(data.description) : flag === 'criteria' ? (data.acceptance_criteria || []).length > 0 : !!(fl[flag]);
      d.classList.toggle('filled', ok);
    }});
    // Also update section-header flag indicators
    overlay.querySelectorAll('.section-flag').forEach(function(sf) {{
      var cat = sf.dataset.cat;
      var sec = CAT_RMAP[cat];
      if (!sec) return;
      var ok2 = sec === 'description' ? !!(data.description) : sec === 'criteria' ? (data.acceptance_criteria || []).length > 0 : !!(fl[sec]);
      sf.classList.toggle('filled', ok2);
    }});
    // Update assess button labels
    overlay.querySelectorAll('.section-assess-btn').forEach(function(btn) {{
      var cat = btn.dataset.cat;
      var sec = CAT_RMAP[cat];
      if (!sec) return;
      var hasContent = sec === 'description' ? !!(data.description) : sec === 'criteria' ? (data.acceptance_criteria || []).length > 0 : !!(fl[sec]);
      btn.textContent = hasContent ? 'Re-assess' : 'Assess';
    }});
  }}

  function scrollToSection(flag) {{
    var sectionId = 'section-' + flag;
    var el = document.getElementById(sectionId);
    if (el) {{
      var body = overlay.querySelector('.detail-body');
      if (body) {{
        var offset = el.offsetTop - body.offsetTop;
        body.scrollTo({{ top: offset, behavior: 'smooth' }});
      }}
    }}
  }}

  /* --- Meta chips --- */
  function populateMetaChips(data) {{
    // Priority
    var prioChip = overlay.querySelector('.meta-chip--priority');
    var prioDot = prioChip.querySelector('.chip-dot');
    var prioText = prioChip.querySelector('.chip-text');
    prioDot.className = 'chip-dot ' + (data.priority || 'medium');
    prioText.textContent = (data.priority || 'medium').charAt(0).toUpperCase() + (data.priority || 'medium').slice(1);

    // Status
    var statusText = overlay.querySelector('.meta-chip--status .chip-text');
    statusText.textContent = (data.status || 'proposed').replace(/-/g, ' ').replace(/\\b\\w/g, function(c){{ return c.toUpperCase(); }});

    // Complexity
    var compText = overlay.querySelector('.meta-chip--complexity .chip-text');
    compText.textContent = data.complexity || 'M';

    // Parent
    var parentChip = overlay.querySelector('.meta-chip--parent');
    var parentVal = parentChip.querySelector('.chip-value');
    parentVal.textContent = data.parent || 'None';

    // Column
    var colText = overlay.querySelector('.meta-chip--section .chip-text');
    colText.textContent = (data.section || '').replace(/^\\w/, function(c){{ return c.toUpperCase(); }});

    // Kitchen automation chip (M1a)
    var autoChip = overlay.querySelector('.meta-chip--automation');
    if (autoChip) {{
      var mode = data.automation_mode || 'manual';
      autoChip.setAttribute('data-mode', mode);
      var label = mode.charAt(0).toUpperCase() + mode.slice(1);
      if (mode === 'held' && data.hold_reason) {{
        label += ' — ' + data.hold_reason;
        autoChip.title = 'Held: ' + data.hold_reason;
      }} else {{
        autoChip.title = 'Automation: ' + label;
      }}
      autoChip.querySelector('.chip-value').textContent = label;
    }}

    // No-tests-required block (M1a)
    var ntrCb = overlay.querySelector('#ntr-checkbox');
    var ntrNote = overlay.querySelector('#ntr-note');
    if (ntrCb && ntrNote) {{
      ntrCb.checked = !!data.no_test_required;
      ntrNote.value = data.no_test_required_note || '';
      ntrNote.classList.toggle('hidden', !ntrCb.checked);
    }}
  }}

  // Priority cycling
  overlay.querySelector('.meta-chip--priority').addEventListener('click', function() {{
    if (!currentData) return;
    var idx = PRIORITY_CYCLE.indexOf(currentData.priority || 'medium');
    var next = PRIORITY_CYCLE[(idx + 1) % PRIORITY_CYCLE.length];
    autosaveField('priority', next).then(function() {{ populateMetaChips(currentData); toast('Priority updated'); }});
  }});

  // Complexity cycling
  overlay.querySelector('.meta-chip--complexity').addEventListener('click', function() {{
    if (!currentData) return;
    var idx = COMPLEXITY_CYCLE.indexOf(currentData.complexity || 'M');
    var next = COMPLEXITY_CYCLE[(idx + 1) % COMPLEXITY_CYCLE.length];
    autosaveField('complexity', next).then(function() {{ populateMetaChips(currentData); toast('Complexity updated'); }});
  }});

  // Status dropdown
  var _statusDropdown = null;
  function closeStatusDropdown() {{ if (_statusDropdown) {{ _statusDropdown.parentNode.removeChild(_statusDropdown); _statusDropdown = null; }} }}
  overlay.querySelector('.meta-chip--status').addEventListener('click', function(e) {{
    e.stopPropagation();
    if (_statusDropdown) {{ closeStatusDropdown(); return; }}
    var chip = this;
    var rect = chip.getBoundingClientRect();
    var dd = document.createElement('div');
    dd.className = 'meta-status-dropdown';
    dd.style.position = 'fixed';
    dd.style.top = (rect.bottom + 4) + 'px';
    dd.style.left = rect.left + 'px';
    STATUS_OPTIONS.forEach(function(opt) {{
      var btn = document.createElement('button');
      btn.className = 'meta-status-opt' + (opt === (currentData && currentData.status) ? ' active' : '');
      btn.textContent = opt.replace(/-/g, ' ').replace(/\\b\\w/g, function(c){{ return c.toUpperCase(); }});
      btn.addEventListener('click', function(ev) {{
        ev.stopPropagation();
        closeStatusDropdown();
        autosaveField('status', opt).then(function() {{ populateMetaChips(currentData); toast('Status updated'); }});
      }});
      dd.appendChild(btn);
    }});
    document.body.appendChild(dd);
    _statusDropdown = dd;
  }});
  document.addEventListener('click', function() {{ closeStatusDropdown(); }});

  /* Kitchen automation chip + picker (M1a) */
  var _autoPicker = null;
  function closeAutoPicker() {{
    if (_autoPicker) {{ _autoPicker.parentNode.removeChild(_autoPicker); _autoPicker = null; }}
  }}
  function postAutomation(mode, holdReason) {{
    if (!currentData || !currentData.id) return Promise.reject(new Error('no ticket'));
    var apiBase = (document.querySelector('meta[name="edit-api"]') || {{}}).content || '';
    var url = (apiBase ? apiBase : '') + '/api/tickets/' + encodeURIComponent(currentData.id) + '/automation';
    var body = {{ mode: mode }};
    if (mode === 'held') body.hold_reason = holdReason || '';
    return fetch(url, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }}).then(function(r) {{
      return r.json().then(function(j) {{
        if (!r.ok) throw new Error(j.error || 'failed');
        return j;
      }});
    }});
  }}
  var autoChipEl = overlay.querySelector('.meta-chip--automation');
  if (autoChipEl) {{
    autoChipEl.addEventListener('click', function(e) {{
      e.stopPropagation();
      if (_autoPicker) {{ closeAutoPicker(); return; }}
      var rect = this.getBoundingClientRect();
      var current = (currentData && currentData.automation_mode) || 'manual';
      var currentReason = (currentData && currentData.hold_reason) || '';
      var pkr = document.createElement('div');
      pkr.className = 'automation-picker';
      pkr.style.position = 'fixed';
      pkr.style.top = (rect.bottom + 4) + 'px';
      pkr.style.left = rect.left + 'px';
      pkr.innerHTML =
        '<div class="automation-picker-row">' +
          ['manual','auto','held'].map(function(m) {{
            return '<button class="automation-picker-opt' + (m===current?' active':'') + '" data-mode="' + m + '">' + m.charAt(0).toUpperCase()+m.slice(1) + '</button>';
          }}).join('') +
        '</div>' +
        '<textarea class="automation-picker-reason" placeholder="Reason (required for Held)"' + (current==='held'?'':' style="display:none"') + '>' + (currentReason ? currentReason.replace(/[&<>"]/g, function(c){{return ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}})[c];}}) : '') + '</textarea>' +
        '<div class="automation-picker-error"></div>' +
        '<div class="automation-picker-actions">' +
          '<button data-act="cancel">Cancel</button>' +
          '<button class="primary" data-act="save">Save</button>' +
        '</div>';
      document.body.appendChild(pkr);
      _autoPicker = pkr;
      var selectedMode = current;
      var reasonEl = pkr.querySelector('.automation-picker-reason');
      var errEl = pkr.querySelector('.automation-picker-error');
      pkr.querySelectorAll('.automation-picker-opt').forEach(function(btn) {{
        btn.addEventListener('click', function(ev) {{
          ev.stopPropagation();
          selectedMode = btn.getAttribute('data-mode');
          pkr.querySelectorAll('.automation-picker-opt').forEach(function(b) {{ b.classList.toggle('active', b===btn); }});
          reasonEl.style.display = selectedMode === 'held' ? '' : 'none';
          errEl.textContent = '';
        }});
      }});
      pkr.querySelector('[data-act="cancel"]').addEventListener('click', function(ev) {{ ev.stopPropagation(); closeAutoPicker(); }});
      pkr.querySelector('[data-act="save"]').addEventListener('click', function(ev) {{
        ev.stopPropagation();
        var reason = (reasonEl.value || '').trim();
        if (selectedMode === 'held' && !reason) {{
          errEl.textContent = 'Reason required when holding.';
          reasonEl.focus();
          return;
        }}
        postAutomation(selectedMode, reason).then(function(updated) {{
          currentData = updated;
          populateMetaChips(currentData);
          closeAutoPicker();
          toast('Automation: ' + selectedMode);
        }}).catch(function(err) {{
          errEl.textContent = err.message || 'failed';
        }});
      }});
      pkr.addEventListener('click', function(ev) {{ ev.stopPropagation(); }});
    }});
  }}
  document.addEventListener('click', function() {{ closeAutoPicker(); }});

  /* Kitchen no-tests-required (M1a) */
  function postNoTestRequired(enabled, note) {{
    if (!currentData || !currentData.id) return Promise.reject(new Error('no ticket'));
    var apiBase = (document.querySelector('meta[name="edit-api"]') || {{}}).content || '';
    var url = (apiBase ? apiBase : '') + '/api/tickets/' + encodeURIComponent(currentData.id) + '/no-test-required';
    return fetch(url, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ enabled: enabled, note: note || '' }}),
    }}).then(function(r) {{
      return r.json().then(function(j) {{
        if (!r.ok) throw new Error(j.error || 'failed');
        return j;
      }});
    }});
  }}
  var ntrCb = overlay.querySelector('#ntr-checkbox');
  var ntrNoteEl = overlay.querySelector('#ntr-note');
  if (ntrCb && ntrNoteEl) {{
    ntrCb.addEventListener('change', function() {{
      ntrNoteEl.classList.toggle('hidden', !ntrCb.checked);
      if (ntrCb.checked) {{
        ntrNoteEl.focus();
      }} else {{
        // Disabled — clear server-side immediately.
        postNoTestRequired(false, '').then(function(updated) {{
          currentData = updated;
          ntrNoteEl.value = '';
          toast('Tests required again');
        }}).catch(function(err) {{ toast('Failed: ' + (err.message || err)); }});
      }}
    }});
    ntrNoteEl.addEventListener('blur', function() {{
      if (!ntrCb.checked) return;
      var note = (ntrNoteEl.value || '').trim();
      if (!note) {{
        // Re-prompt — empty note when checked is invalid.
        toast('Note is required when no_test_required is on');
        ntrNoteEl.focus();
        return;
      }}
      postNoTestRequired(true, note).then(function(updated) {{
        currentData = updated;
        toast('Saved no-tests rationale');
      }}).catch(function(err) {{ toast('Failed: ' + (err.message || err)); }});
    }});
  }}

  /* Kitchen activity history (M1b) */
  function escapeHtmlForHistory(s) {{
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"]/g, function(c) {{
      return ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}})[c];
    }});
  }}
  function renderHistorySummary(ev) {{
    var p = ev.payload || {{}};
    var k = ev.event_kind;
    function diff(before, after) {{
      var b = escapeHtmlForHistory(typeof before === 'object' ? JSON.stringify(before) : (before === '' || before === null ? '∅' : before));
      var a = escapeHtmlForHistory(typeof after === 'object'  ? JSON.stringify(after)  : (after  === '' || after  === null ? '∅' : after));
      return '<span class="h-old">' + b + '</span> → <span class="h-new">' + a + '</span>';
    }}
    if (k === 'section_change')      return diff(p.before, p.after);
    if (k === 'status_change')        return diff(p.before, p.after);
    if (k === 'mode_changed')         return 'mode: ' + diff(p.before, p.after);
    if (k === 'hold_set')             return 'held: ' + escapeHtmlForHistory(p.reason || '');
    if (k === 'hold_cleared')         return 'unheld → ' + escapeHtmlForHistory(p.after || '');
    if (k === 'criteria_check')       return 'criterion #' + p.criterion_id + ': ' + diff(p.before, p.after);
    if (k === 'criteria_added')       return '+ ' + escapeHtmlForHistory(p.text || '');
    if (k === 'criteria_removed')     return '− ' + escapeHtmlForHistory(p.text || '');
    if (k === 'criteria_changed')     return diff(p.before, p.after);
    if (k === 'field_changed')        return p.field + ': ' + diff(p.before, p.after);
    if (k === 'dependency_changed')   return diff(p.before, p.after);
    if (k === 'readiness_changed')    return p.flag + ': ' + diff(
      (p.before && p.before.present) ? (p.before.content || '✓') : '∅',
      (p.after  && p.after.present ) ? (p.after.content  || '✓') : '∅'
    );
    if (k === 'attachment_added')     return '+ ' + escapeHtmlForHistory((p.kind || '') + ':' + (p.label || ''));
    if (k === 'attachment_removed')   return '− ' + escapeHtmlForHistory((p.kind || '') + ':' + (p.label || ''));
    if (k === 'ticket_created')       return 'created in ' + escapeHtmlForHistory(p.section || '');
    if (k === 'ticket_deleted')       return 'deleted';
    if (k === 'run_started')          return 'run #' + p.run_id + ' (' + (p.runner_kind || '') + ')';
    if (k === 'run_succeeded')        return '✓ run #' + p.run_id + (p.summary ? ' — ' + escapeHtmlForHistory(p.summary) : '');
    if (k === 'run_failed')           return '✗ run #' + p.run_id + ' — ' + escapeHtmlForHistory(p.error_message || p.error_class || '');
    if (k === 'run_cancelled')        return '⏹ run #' + p.run_id;
    if (k === 'needs_input')          return '? ' + escapeHtmlForHistory(p.prompt || '');
    if (k === 'input_provided')       return escapeHtmlForHistory(p.response_excerpt || '');
    return escapeHtmlForHistory(JSON.stringify(p));
  }}
  function timeAgo(iso) {{
    if (!iso) return '';
    var t = Date.parse(iso); if (isNaN(t)) return iso;
    var diff = (Date.now() - t) / 1000;
    if (diff < 60) return Math.round(diff) + 's ago';
    if (diff < 3600) return Math.round(diff / 60) + 'm ago';
    if (diff < 86400) return Math.round(diff / 3600) + 'h ago';
    return Math.round(diff / 86400) + 'd ago';
  }}
  function loadHistory(ticketId) {{
    var listEl = overlay.querySelector('#history-list');
    if (!listEl) return;
    listEl.innerHTML = '<div class="history-empty">Loading…</div>';
    var apiBase = (document.querySelector('meta[name="edit-api"]') || {{}}).content || '';
    var url = (apiBase || '') + '/api/tickets/' + encodeURIComponent(ticketId) + '/history';
    fetch(url).then(function(r) {{ return r.json(); }}).then(function(data) {{
      var events = (data && data.events) || [];
      if (!events.length) {{ listEl.innerHTML = '<div class="history-empty">No history yet.</div>'; return; }}
      listEl.innerHTML = events.map(function(ev) {{
        var discardCls = ev.discarded_run_id ? ' discarded' : '';
        return '<div class="history-row' + discardCls + '">' +
          '<span class="h-time" title="' + escapeHtmlForHistory(ev.occurred_at) + '">' + escapeHtmlForHistory(timeAgo(ev.occurred_at)) + '</span>' +
          '<span class="h-actor ' + escapeHtmlForHistory(ev.actor_type) + '">' + escapeHtmlForHistory(ev.actor_type) + '</span>' +
          '<span class="h-summary"><span class="h-kind">' + escapeHtmlForHistory(ev.event_kind) + '</span> ' + renderHistorySummary(ev) + '</span>' +
          '<span></span>' +
        '</div>';
      }}).join('');
    }}).catch(function(err) {{
      listEl.innerHTML = '<div class="history-empty">Failed: ' + escapeHtmlForHistory(err.message || err) + '</div>';
    }});
  }}
  /* =====================================================================
     Live run panel (M3)
     ===================================================================== */
  var _runsSection    = overlay.querySelector('#section-runs');
  var _runNowBtn      = overlay.querySelector('#run-now-btn');
  var _runLatestCard  = overlay.querySelector('#run-latest-card');
  var _runLatestPill  = overlay.querySelector('#run-latest-pill');
  var _runLatestSumm  = overlay.querySelector('#run-latest-summary');
  var _runLatestTime  = overlay.querySelector('#run-latest-time');
  var _runLatestDur   = overlay.querySelector('#run-latest-duration');
  var _runLatestWs    = overlay.querySelector('#run-latest-ws');
  var _runStopBtn     = overlay.querySelector('#run-stop-btn');
  var _runRetryBtn    = overlay.querySelector('#run-retry-btn');
  var _runRetryFresh  = overlay.querySelector('#run-retry-fresh-btn');
  var _runDiscardBtn  = overlay.querySelector('#run-discard-btn');
  var _runDiscardConf = overlay.querySelector('#run-discard-confirm');
  var _runDiscardYes  = overlay.querySelector('#run-discard-yes');
  var _runDiscardNo   = overlay.querySelector('#run-discard-no');
  var _runFileGapBtn  = overlay.querySelector('#run-file-gap-btn');
  var _runsCount      = overlay.querySelector('#runs-count');
  var _runHistToggle  = overlay.querySelector('#run-history-toggle');
  var _runHistList    = overlay.querySelector('#run-history-list');
  var _runNiPanel     = overlay.querySelector('#run-ni-panel');
  var _runNiPrompt    = overlay.querySelector('#run-ni-prompt');
  var _runNiTextarea  = overlay.querySelector('#run-ni-textarea');
  var _runNiSend      = overlay.querySelector('#run-ni-send');
  var _runNiCancel    = overlay.querySelector('#run-ni-cancel');

  var _currentRunId   = null;   // ID of the latest run being displayed
  var _runsPollTimer  = null;   // setInterval handle for active run polling

  var ACTIVE_RUN_STATUSES = {{'queued':1,'preparing':1,'running':1,'needs_input':1}};
  var TERMINAL_RUN_STATUSES = {{'succeeded':1,'failed':1,'stalled':1,'cancelled':1}};

  function _esc(s) {{ return escapeHtmlForHistory(s == null ? '' : String(s)); }}

  function _runPillClass(status) {{
    var safe = (status || 'unknown').replace(/[^a-z_]/g, '');
    return 'run-pill run-pill-' + safe;
  }}

  function _runPillLabel(status) {{
    var labels = {{
      queued: 'queued', preparing: 'preparing', running: 'running',
      needs_input: 'needs input', succeeded: 'succeeded',
      failed: 'failed', stalled: 'stalled', cancelled: 'cancelled'
    }};
    return labels[status] || (status || '—');
  }}

  function _fmtDuration(ms) {{
    if (!ms) return '';
    if (ms < 1000) return ms + 'ms';
    if (ms < 60000) return Math.round(ms / 1000) + 's';
    return Math.round(ms / 60000) + 'm ' + Math.round((ms % 60000) / 1000) + 's';
  }}

  function renderRuns(data) {{
    var runs = (data && data.runs) || [];
    if (!runs.length) {{
      _runsSection.classList.add('hidden');
      _stopRunsPolling();
      return;
    }}
    _runsSection.classList.remove('hidden');
    var latest = runs[0];
    _currentRunId = latest.id;
    var isActive = !!ACTIVE_RUN_STATUSES[latest.status];
    var isTerminal = !!TERMINAL_RUN_STATUSES[latest.status];

    // Count badge
    if (_runsCount) _runsCount.textContent = '(' + runs.length + ')';

    // Run-now button — show when eligible and no active run
    var eligible = currentData && currentData.automation_eligible;
    if (_runNowBtn) {{
      _runNowBtn.style.display = (eligible && !isActive) ? 'inline-flex' : 'none';
    }}

    // Latest card
    _runLatestCard.style.display = 'block';
    _runLatestPill.className = _runPillClass(latest.status);
    _runLatestPill.textContent = _runPillLabel(latest.status);

    var summaryText = latest.summary || (latest.error_message ? latest.error_message : '—');
    _runLatestSumm.textContent = summaryText;
    _runLatestSumm.title = summaryText;

    _runLatestTime.textContent = latest.started_at ? timeAgo(latest.started_at) : '—';
    _runLatestDur.textContent = latest.duration_ms ? '(' + _fmtDuration(latest.duration_ms) + ')' : (isActive ? '(running…)' : '');

    if (latest.workspace_path) {{
      _runLatestWs.href = 'file://' + _esc(latest.workspace_path);
      _runLatestWs.textContent = latest.workspace_path;
      _runLatestWs.title = latest.workspace_path;
    }} else {{
      _runLatestWs.href = '#';
      _runLatestWs.textContent = '—';
      _runLatestWs.removeAttribute('title');
    }}

    // Action buttons
    _runStopBtn.style.display    = isActive    ? 'inline-flex' : 'none';
    _runRetryBtn.style.display   = isTerminal  ? 'inline-flex' : 'none';
    _runRetryFresh.style.display = isTerminal  ? 'inline-flex' : 'none';
    _runDiscardBtn.style.display = isTerminal  ? 'inline-flex' : 'none';

    // Kitchen M4: "File gap ticket" — only on red scenario runs (failed/stalled).
    var canFileGap = (latest.runner_kind === 'scenario' &&
                      (latest.status === 'failed' || latest.status === 'stalled'));
    if (_runFileGapBtn) {{
      _runFileGapBtn.style.display = canFileGap ? 'inline-flex' : 'none';
      _runFileGapBtn.dataset.runId = latest.id;
    }}

    // Store run id on buttons for handlers
    [_runStopBtn, _runRetryBtn, _runRetryFresh, _runDiscardBtn].forEach(function(b) {{
      if (b) b.dataset.runId = latest.id;
    }});

    // needs_input panel
    if (latest.status === 'needs_input' && _runNiPanel) {{
      _runNiPanel.classList.remove('hidden');
      if (_runNiPrompt) _runNiPrompt.textContent = latest.needs_input_prompt || 'The agent is waiting for your input.';
      if (_runNiTextarea) {{ _runNiTextarea.value = ''; _runNiTextarea.dataset.runId = latest.id; }}
      if (_runNiSend) {{ _runNiSend.disabled = true; _runNiSend.dataset.runId = latest.id; }}
      if (_runNiCancel) _runNiCancel.dataset.runId = latest.id;
    }} else {{
      if (_runNiPanel) _runNiPanel.classList.add('hidden');
    }}

    // Recent history (skip latest = runs[0])
    var older = runs.slice(1, 6);
    if (_runHistToggle) _runHistToggle.style.display = older.length ? 'inline' : 'none';
    if (_runHistList) {{
      _runHistList.innerHTML = older.map(function(r) {{
        return '<div class="run-history-row">' +
          '<span class="' + _esc(_runPillClass(r.status)) + '">' + _esc(_runPillLabel(r.status)) + '</span>' +
          '<span>' + _esc(r.summary || r.error_message || '—') + '</span>' +
          '<span class="rh-time">' + _esc(r.started_at ? timeAgo(r.started_at) : '—') + '</span>' +
          '</div>';
      }}).join('');
    }}

    // Manage polling
    if (isActive) {{
      _startRunsPolling(currentTicketId);
    }} else {{
      _stopRunsPolling();
    }}
  }}

  function loadRuns(ticketId) {{
    if (!ticketId) return;
    var url = EDIT_API + '/runs?ticket=' + encodeURIComponent(ticketId);
    fetch(url).then(function(r) {{ return r.json(); }}).then(function(data) {{
      renderRuns(data);
    }}).catch(function() {{
      // Silently fail — runs API may not be deployed yet
    }});
  }}

  function _startRunsPolling(ticketId) {{
    if (_runsPollTimer) return;  // already polling
    _runsPollTimer = setInterval(function() {{
      if (overlay.classList.contains('hidden')) {{ _stopRunsPolling(); return; }}
      loadRuns(ticketId);
    }}, 2000);
  }}

  function _stopRunsPolling() {{
    if (_runsPollTimer) {{ clearInterval(_runsPollTimer); _runsPollTimer = null; }}
  }}

  function postRunNow(ticketId) {{
    if (!ticketId) return;
    var btn = _runNowBtn;
    if (btn) btn.disabled = true;
    var url = EDIT_API + '/tickets/' + encodeURIComponent(ticketId) + '/run-now';
    fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}'}})
      .then(function(r) {{
        if (r.status === 409) {{ showAppToast('A run is already active for this ticket.', 'error'); if(btn) btn.disabled=false; return null; }}
        if (r.status === 422) {{ return r.json().then(function(d) {{ showAppToast('Not eligible: ' + ((d.reasons||[]).join('; ')||'unknown reason'), 'error'); if(btn) btn.disabled=false; return null; }}); }}
        if (!r.ok) {{ showAppToast('Run failed to start.', 'error'); if(btn) btn.disabled=false; return null; }}
        return r.json();
      }})
      .then(function(run) {{
        if (!run) return;
        showAppToast('Run started', 'success');
        loadRuns(currentTicketId);
      }})
      .catch(function() {{ showAppToast('Network error starting run.', 'error'); if(btn) btn.disabled=false; }});
  }}

  function postRunAction(runId, action) {{
    if (!runId) return;
    var url = EDIT_API + '/runs/' + encodeURIComponent(runId) + '/' + action;
    return fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}'}})
      .then(function(r) {{ return r.json(); }})
      .then(function(run) {{
        showAppToast(action.charAt(0).toUpperCase() + action.slice(1).replace(/-/g,' ') + ' sent', 'success');
        loadRuns(currentTicketId);
        return run;
      }})
      .catch(function() {{ showAppToast('Network error.', 'error'); }});
  }}

  function postNeedsInputResponse(runId, text) {{
    if (!runId || !text) return;
    var url = EDIT_API + '/runs/' + encodeURIComponent(runId) + '/respond';
    if (_runNiSend) _runNiSend.disabled = true;
    fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{response: text}})}})
      .then(function(r) {{ return r.json(); }})
      .then(function(run) {{
        showAppToast('Response sent', 'success');
        if (_runNiTextarea) _runNiTextarea.value = '';
        loadRuns(currentTicketId);
      }})
      .catch(function() {{ showAppToast('Network error sending response.', 'error'); if(_runNiSend) _runNiSend.disabled=false; }});
  }}

  // Run-now button in detail header
  if (_runNowBtn) {{
    _runNowBtn.addEventListener('click', function() {{
      postRunNow(currentTicketId);
    }});
  }}

  // Stop button
  if (_runStopBtn) {{
    _runStopBtn.addEventListener('click', function() {{
      postRunAction(this.dataset.runId, 'stop');
    }});
  }}

  // Retry button
  if (_runRetryBtn) {{
    _runRetryBtn.addEventListener('click', function() {{
      postRunAction(this.dataset.runId, 'retry');
    }});
  }}

  // Retry fresh button
  if (_runRetryFresh) {{
    _runRetryFresh.addEventListener('click', function() {{
      postRunAction(this.dataset.runId, 'retry-fresh');
    }});
  }}

  // Discard button — show inline confirm
  if (_runDiscardBtn) {{
    _runDiscardBtn.addEventListener('click', function() {{
      _runDiscardConf.classList.remove('hidden');
      _runDiscardBtn.style.display = 'none';
    }});
  }}
  if (_runDiscardYes) {{
    _runDiscardYes.addEventListener('click', function() {{
      _runDiscardConf.classList.add('hidden');
      if (_runDiscardBtn) _runDiscardBtn.style.display = 'none';
      postRunAction(_currentRunId, 'discard');
    }});
  }}
  if (_runDiscardNo) {{
    _runDiscardNo.addEventListener('click', function() {{
      _runDiscardConf.classList.add('hidden');
      if (_runDiscardBtn && TERMINAL_RUN_STATUSES[_currentRunId]) _runDiscardBtn.style.display = 'inline-flex';
      else if (_runDiscardBtn) _runDiscardBtn.style.display = 'inline-flex';
    }});
  }}

  /* Kitchen M4: file gap ticket from a red scenario run. */
  if (_runFileGapBtn) {{
    _runFileGapBtn.addEventListener('click', function() {{
      var runId = this.dataset.runId;
      if (!runId) return;
      _runFileGapBtn.disabled = true;
      var url = EDIT_API + '/runs/' + encodeURIComponent(runId) + '/file-gap-ticket';
      fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}'}})
        .then(function(r) {{ return r.json().then(function(j) {{ return [r.ok, j]; }}); }})
        .then(function(arr) {{
          var ok = arr[0], j = arr[1];
          _runFileGapBtn.disabled = false;
          if (!ok) {{
            if (typeof showAppToast === 'function') showAppToast('File gap failed: ' + (j.error || 'unknown'), 'error');
            return;
          }}
          var ticket = j.ticket || {{}};
          var msg = 'Gap ticket filed: ' + (ticket.id || '?');
          if (typeof showAppToast === 'function') showAppToast(msg, 'success');
        }})
        .catch(function(err) {{
          _runFileGapBtn.disabled = false;
          if (typeof showAppToast === 'function') showAppToast('File gap failed: ' + (err.message || err), 'error');
        }});
    }});
  }}

  // needs_input textarea — enable send when non-empty
  if (_runNiTextarea) {{
    _runNiTextarea.addEventListener('input', function() {{
      if (_runNiSend) _runNiSend.disabled = !this.value.trim();
    }});
    _runNiTextarea.addEventListener('keydown', function(e) {{
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {{
        e.preventDefault();
        if (_runNiSend && !_runNiSend.disabled) _runNiSend.click();
      }}
    }});
  }}
  if (_runNiSend) {{
    _runNiSend.addEventListener('click', function() {{
      var tid = this.dataset.runId;
      var txt = _runNiTextarea ? _runNiTextarea.value.trim() : '';
      if (tid && txt) postNeedsInputResponse(tid, txt);
    }});
  }}
  if (_runNiCancel) {{
    _runNiCancel.addEventListener('click', function() {{
      // Cancel = stop the run
      postRunAction(this.dataset.runId || _currentRunId, 'stop');
    }});
  }}

  // Recent runs toggle
  if (_runHistToggle) {{
    _runHistToggle.addEventListener('click', function() {{
      var isVisible = _runHistList.classList.toggle('visible');
      this.textContent = isVisible ? 'Hide recent runs' : 'Show recent runs';
    }});
  }}

  var historyToggleBtn = overlay.querySelector('#history-toggle');
  if (historyToggleBtn) {{
    historyToggleBtn.addEventListener('click', function() {{
      var listEl = overlay.querySelector('#history-list');
      var isHidden = listEl.classList.toggle('hidden');
      historyToggleBtn.textContent = isHidden ? 'Show' : 'Hide';
      if (!isHidden && currentData && currentData.id) {{
        loadHistory(currentData.id);
      }}
    }});
  }}

  // Parent chip — click to edit inline
  overlay.querySelector('.meta-chip--parent').addEventListener('click', function() {{
    var chip = this;
    var valEl = chip.querySelector('.chip-value');
    if (chip.querySelector('input')) return; // already editing
    var current = (currentData && currentData.parent) || '';
    var inp = document.createElement('input');
    inp.value = current;
    inp.placeholder = 'e.g. B-01';
    valEl.style.display = 'none';
    chip.appendChild(inp);
    inp.focus();
    function finish() {{
      var newVal = inp.value.trim();
      if (chip.contains(inp)) chip.removeChild(inp);
      valEl.style.display = '';
      if (newVal !== current) {{
        autosaveField('parent', newVal).then(function() {{ populateMetaChips(currentData); toast('Parent updated'); }});
      }}
    }}
    inp.addEventListener('blur', finish);
    inp.addEventListener('keydown', function(e) {{ if(e.key==='Enter') inp.blur(); if(e.key==='Escape'){{ inp.value=current; inp.blur(); }} }});
  }});

  /* --- Title inline editing --- */
  titleEl.addEventListener('click', function() {{
    if (titleEl.contentEditable === 'true') return;
    titleEl.contentEditable = 'true';
    titleEl.focus();
    // Select all text
    var range = document.createRange();
    range.selectNodeContents(titleEl);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }});
  titleEl.addEventListener('blur', function() {{
    titleEl.contentEditable = 'false';
    var newTitle = titleEl.textContent.trim();
    if (currentData && newTitle && newTitle !== currentData.title) {{
      autosaveField('title', newTitle).then(function() {{ toast('Title updated'); }});
    }}
  }});
  titleEl.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') {{ e.preventDefault(); titleEl.blur(); }}
    if (e.key === 'Escape') {{ titleEl.textContent = currentData ? currentData.title : ''; titleEl.blur(); }}
  }});

  function clearAssessments() {{
    overlay.querySelectorAll('.detail-assessment').forEach(function(el) {{
      el.classList.add('hidden');
      el.className = 'detail-assessment hidden';
      while (el.firstChild) el.removeChild(el.firstChild);
    }});
    overlay.querySelectorAll('.detail-assess-loading').forEach(function(el) {{
      el.classList.add('hidden');
    }});
    _hasAssessmentData = false;
  }}

  function renderCategoryAssessment(cat, result) {{
    var el = overlay.querySelector('[data-cat-result="'+cat+'"]');
    if (!el) return;
    var status = result.status || 'needs-work';
    el.className = 'detail-assessment ' + status;
    while (el.firstChild) el.removeChild(el.firstChild);

    var header = document.createElement('div');
    header.className = 'assessment-header';
    var badge = document.createElement('span');
    badge.className = 'assessment-status ' + status;
    badge.textContent = status.replace(/-/g, ' ');
    header.appendChild(badge);
    var dismiss = document.createElement('button');
    dismiss.className = 'assessment-dismiss';
    dismiss.textContent = '\\u00d7';
    dismiss.addEventListener('click', function() {{ el.classList.add('hidden'); }});
    header.appendChild(dismiss);
    el.appendChild(header);

    if (result.current_summary) {{
      var sum = document.createElement('div');
      sum.className = 'assessment-summary';
      sum.textContent = result.current_summary;
      el.appendChild(sum);
    }}

    if (result.suggestion) {{
      var sug = document.createElement('div');
      sug.className = 'assessment-suggestion';
      sug.textContent = result.suggestion;
      el.appendChild(sug);
    }}

    // Contextual action buttons — copy workflow prompt to clipboard
    var actionDefs = {{
      D: {{ icon: '\U0001F4C4', label: 'Write Description',
            prompt: function(t) {{ return 'Write a detailed description for ' + t.id + ': "' + t.title + '". Include problem statement, proposed solution, scope, and constraints.'; }} }},
      C: {{ icon: '\\u2611', label: 'Add Criteria',
            prompt: function(t) {{ return 'Write acceptance criteria for ' + t.id + ': "' + t.title + '". Use Given/When/Then format.\\n\\nDescription:\\n' + (t.description || '(empty)'); }} }},
      T: {{ icon: '\U0001F52C', label: 'Run Tests',
            prompt: function(t) {{ return 'Write test definitions for ' + t.id + ': "' + t.title + '".\\n\\nCriteria:\\n' + (t.criteria_text || '(none)'); }} }},
      R: {{ icon: '\U0001F441', label: 'Start Learnings',
            prompt: function(t) {{ return 'Perform a code review for ' + t.id + ': "' + t.title + '". Check correctness, edge cases, and document decisions.\\n\\nDescription:\\n' + (t.description || '(empty)'); }} }},
      S: {{ icon: '\U0001F4A8', label: 'Run Smoke',
            prompt: function(t) {{ return 'Create a smoke test checklist for ' + t.id + ': "' + t.title + '". List manual verification steps to confirm the feature works end-to-end.\\n\\nCriteria:\\n' + (t.criteria_text || '(none)'); }} }}
    }};
    var actionDef = actionDefs[cat];
    if (actionDef && currentData) {{
      var actionRow = document.createElement('div');
      actionRow.className = 'assessment-action-row';
      var actionBtn = document.createElement('button');
      actionBtn.className = 'assessment-action-btn';
      actionBtn.textContent = actionDef.icon + ' ' + actionDef.label;
      actionBtn.addEventListener('click', function() {{
        var prompt = actionDef.prompt(currentData);
        navigator.clipboard.writeText(prompt).then(function() {{
          toast('Prompt copied \\u2014 paste into Claude');
          actionBtn.textContent = actionDef.icon + ' Copied \\u2714';
          setTimeout(function() {{ actionBtn.textContent = actionDef.icon + ' ' + actionDef.label; }}, 2000);
        }});
      }});
      actionRow.appendChild(actionBtn);
      el.appendChild(actionRow);
    }}

    if (result.content) {{
      var applyBtn = document.createElement('button');
      applyBtn.className = 'assessment-apply-btn';
      applyBtn.textContent = 'Apply Generated Content';
      applyBtn.addEventListener('click', function() {{
        var section = CAT_RMAP[cat];
        var editor = overlay.querySelector('[data-field="'+section+'"]');
        if (editor) {{
          editor.value = result.content;
          toast('Content applied \\u2014 click outside to save');
        }}
        applyBtn.textContent = 'Applied \\u2714';
        applyBtn.style.pointerEvents = 'none';
      }});
      el.appendChild(applyBtn);
    }}

    if (cat === 'C' && result.add_criteria && result.add_criteria.length > 0) {{
      var list = document.createElement('ul');
      list.className = 'assessment-add-criteria';
      result.add_criteria.forEach(function(criterion) {{
        var li = document.createElement('li');
        var span = document.createElement('span');
        span.textContent = criterion;
        var addBtn = document.createElement('button');
        addBtn.textContent = '+ Add';
        addBtn.addEventListener('click', function() {{
          fetch(EDIT_API+'/tickets/'+currentTicketId, {{method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{add_criteria:criterion}})}})
            .then(function(r){{return r.json();}})
            .then(function(u) {{
              if (u) {{ currentData = u; populateCriteria(u); }}
              addBtn.textContent = 'Added \\u2714';
              addBtn.className = 'added';
            }});
        }});
        li.appendChild(span);
        li.appendChild(addBtn);
        list.appendChild(li);
      }});
      el.appendChild(list);
    }}

    el.classList.remove('hidden');
    _hasAssessmentData = true;
  }}

  // Field name mapping from cat key to data-field attribute on textarea
  var CAT_FIELD_MAP = {{ D:'description', C:'criteria', T:'tests', R:'reviewed', S:'smoke', L:'reviewed' }};

  function _getFieldContent(cat) {{
    if (cat === 'C') {{
      var items = [];
      overlay.querySelectorAll('.detail-criteria-list .criteria-item').forEach(function(li) {{
        var cb = li.querySelector('input[type=checkbox]');
        var txt = li.querySelector('.criteria-text');
        if (txt) items.push((cb && cb.checked ? '[x] ' : '[ ] ') + txt.textContent.trim());
      }});
      return items.join('\\n');
    }}
    var field = CAT_FIELD_MAP[cat];
    var ta = overlay.querySelector('[data-field="' + field + '"]');
    return ta ? ta.value : '';
  }}

  function _findLine(lines, text) {{
    for (var i = 0; i < lines.length; i++) {{
      if (lines[i] === text) return i;
    }}
    return -1;
  }}

  function _applyDiffHunks(origLines, hunks, states) {{
    var lines = origLines.slice();
    hunks.forEach(function(hunk, i) {{
      if (states[i] !== 'accepted') return;
      if (hunk.type === 'modify') {{
        var pos = _findLine(lines, hunk.original);
        if (pos !== -1) lines[pos] = hunk.suggested;
      }} else if (hunk.type === 'remove') {{
        var pos = _findLine(lines, hunk.original);
        if (pos !== -1) lines.splice(pos, 1);
      }} else if (hunk.type === 'add') {{
        lines.push(hunk.suggested);
      }}
    }});
    return lines;
  }}

  function renderDiffUI(container, data, cat) {{
    var existing = container.querySelector('.diff-panel');
    if (existing) existing.parentNode.removeChild(existing);

    var hunks = data.hunks || [];
    var original = data.original || '';

    if (!hunks.length) {{
      // Build "no changes" notice using safe DOM methods
      var noChange = document.createElement('div');
      noChange.className = 'detail-assessment ok';
      noChange.style.marginBottom = '12px';
      var ncHeader = document.createElement('div');
      ncHeader.className = 'assessment-header';
      var ncBadge = document.createElement('span');
      ncBadge.className = 'assessment-status ok';
      ncBadge.textContent = 'no changes';
      var ncDismiss = document.createElement('button');
      ncDismiss.className = 'assessment-dismiss';
      ncDismiss.textContent = '\\u00d7';
      ncDismiss.addEventListener('click', function() {{ noChange.classList.add('hidden'); }});
      ncHeader.appendChild(ncBadge);
      ncHeader.appendChild(ncDismiss);
      var ncSummary = document.createElement('div');
      ncSummary.className = 'assessment-summary';
      ncSummary.textContent = 'Content looks good \\u2014 no improvements suggested.';
      noChange.appendChild(ncHeader);
      noChange.appendChild(ncSummary);
      container.insertBefore(noChange, container.firstChild);
      return;
    }}

    var panel = document.createElement('div');
    panel.className = 'diff-panel';

    var header = document.createElement('div');
    header.className = 'diff-header';
    var titleSpan = document.createElement('span');
    titleSpan.textContent = 'Suggested Changes (' + hunks.length + ')';
    var acceptAll = document.createElement('button');
    acceptAll.className = 'diff-accept-all';
    acceptAll.textContent = 'Accept All';
    var rejectAll = document.createElement('button');
    rejectAll.className = 'diff-reject-all';
    rejectAll.textContent = 'Reject All';
    header.appendChild(titleSpan);
    header.appendChild(acceptAll);
    header.appendChild(rejectAll);
    panel.appendChild(header);

    var states = hunks.map(function() {{ return 'pending'; }});
    var hunkContainer = document.createElement('div');
    hunkContainer.className = 'diff-hunks';

    var hunkEls = hunks.map(function(hunk, i) {{
      var row = document.createElement('div');
      row.className = 'diff-hunk';
      row.dataset.index = i;

      var linesEl = document.createElement('div');
      linesEl.className = 'diff-hunk-lines';
      if (hunk.type === 'remove' || hunk.type === 'modify') {{
        var oldEl = document.createElement('div');
        oldEl.className = 'diff-hunk-old';
        oldEl.textContent = '\\u2212 ' + (hunk.original || '');
        linesEl.appendChild(oldEl);
      }}
      if (hunk.type === 'add' || hunk.type === 'modify') {{
        var newEl = document.createElement('div');
        newEl.className = 'diff-hunk-new';
        newEl.contentEditable = 'true';
        newEl.spellcheck = false;
        newEl.textContent = '+ ' + (hunk.suggested || '');
        newEl.addEventListener('input', function() {{
          hunk.suggested = newEl.textContent.replace(/^\\+\\s?/, '');
        }});
        newEl.addEventListener('keydown', function(e) {{
          if (e.key === 'Enter') e.preventDefault();
        }});
        linesEl.appendChild(newEl);
      }}

      var actionsEl = document.createElement('div');
      actionsEl.className = 'diff-hunk-actions';
      var acceptBtn = document.createElement('button');
      acceptBtn.className = 'diff-accept';
      acceptBtn.title = 'Accept change';
      acceptBtn.textContent = '\\u2713';
      var rejectBtn = document.createElement('button');
      rejectBtn.className = 'diff-reject';
      rejectBtn.title = 'Reject change';
      rejectBtn.textContent = '\\u00d7';
      actionsEl.appendChild(acceptBtn);
      actionsEl.appendChild(rejectBtn);
      row.appendChild(linesEl);
      row.appendChild(actionsEl);
      hunkContainer.appendChild(row);

      ;(function(idx, rowEl) {{
        function setHunkState(newState) {{
          if (states[idx] === newState) {{
            states[idx] = 'pending';
            rowEl.classList.remove('accepted', 'rejected');
          }} else {{
            states[idx] = newState;
            rowEl.classList.remove('accepted', 'rejected');
            if (newState !== 'pending') rowEl.classList.add(newState);
          }}
          updateStatus();
        }}
        acceptBtn.addEventListener('click', function() {{ setHunkState('accepted'); }});
        rejectBtn.addEventListener('click', function() {{ setHunkState('rejected'); }});
      }})(i, row);

      return row;
    }});

    panel.appendChild(hunkContainer);

    var footer = document.createElement('div');
    footer.className = 'diff-footer';
    var statusEl = document.createElement('span');
    statusEl.className = 'diff-status';
    var applyBtn = document.createElement('button');
    applyBtn.className = 'diff-apply';
    applyBtn.textContent = 'Apply Selected';
    applyBtn.disabled = true;
    var discardBtn = document.createElement('button');
    discardBtn.className = 'diff-discard';
    discardBtn.textContent = 'Discard';
    footer.appendChild(statusEl);
    footer.appendChild(discardBtn);
    footer.appendChild(applyBtn);
    panel.appendChild(footer);

    function updateStatus() {{
      var accepted = states.filter(function(s) {{ return s === 'accepted'; }}).length;
      var rejected = states.filter(function(s) {{ return s === 'rejected'; }}).length;
      statusEl.textContent = accepted + ' accepted, ' + rejected + ' rejected, ' + (hunks.length - accepted - rejected) + ' pending';
      applyBtn.disabled = accepted === 0;
    }}
    updateStatus();

    acceptAll.addEventListener('click', function() {{
      for (var k = 0; k < states.length; k++) states[k] = 'accepted';
      hunkEls.forEach(function(el) {{ el.classList.remove('rejected'); el.classList.add('accepted'); }});
      updateStatus();
    }});
    rejectAll.addEventListener('click', function() {{
      for (var k = 0; k < states.length; k++) states[k] = 'rejected';
      hunkEls.forEach(function(el) {{ el.classList.remove('accepted'); el.classList.add('rejected'); }});
      updateStatus();
    }});

    applyBtn.addEventListener('click', function() {{
      var origLines = original.split('\\n');
      var resultLines = _applyDiffHunks(origLines, hunks, states);
      var merged = resultLines.join('\\n');
      var field = CAT_FIELD_MAP[cat];
      var ta = overlay.querySelector('[data-field="' + field + '"]');
      if (ta) {{
        ta.value = merged;
        toast('Content applied \\u2014 click outside to save');
      }}
      panel.classList.add('hidden');
    }});

    discardBtn.addEventListener('click', function() {{
      panel.classList.add('hidden');
    }});

    container.insertBefore(panel, container.firstChild);
  }}

  function runCategoryAssess(cat, action, onDone, forceRefresh) {{
    var catCacheKey = currentTicketId + ':cat:' + cat;
    var loading = overlay.querySelector('[data-cat-loading="'+cat+'"]');
    var resultEl = overlay.querySelector('[data-cat-result="'+cat+'"]');

    // Check cache (unless force refresh)
    if (!forceRefresh && _assessCache[catCacheKey]) {{
      var cached = _assessCache[catCacheKey];
      if (onDone) onDone();
      var sectionKey = CAT_RMAP[cat];
      var section = overlay.querySelector('[data-section="' + sectionKey + '"]');
      if (section) renderDiffUI(section, cached, cat);
      return;
    }}

    if (loading) {{ loading.classList.remove('hidden'); loading.textContent = 'Assessing ' + (FLAG_NAMES[CAT_RMAP[cat]] || cat) + '...'; }}
    if (resultEl) resultEl.classList.add('hidden');

    var content = _getFieldContent(cat);
    var fieldName = CAT_FIELD_MAP[cat];

    fetch(EDIT_API + '/tickets/' + currentTicketId + '/enrich', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ field: fieldName, content: content, action: action }})
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (loading) loading.classList.add('hidden');
      if (onDone) onDone();
      if (data.error) {{
        toast('Enrich error: ' + data.error);
        return;
      }}
      _assessCache[catCacheKey] = data;  // cache the result
      var sectionKey = CAT_RMAP[cat];
      var section = overlay.querySelector('[data-section="' + sectionKey + '"]');
      if (section) {{
        renderDiffUI(section, data, cat);
        section.classList.add('assess-complete');
        setTimeout(function() {{ section.classList.remove('assess-complete'); }}, 1500);
        section.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
      }}
    }})
    .catch(function() {{
      if (loading) loading.classList.add('hidden');
      if (onDone) onDone();
      toast('Enrich request failed');
    }});
  }}

  function showGateBannerLoading(targetSection) {{
    _gateContext = {{ targetSection: targetSection, ticketId: currentTicketId }};
    gateBadge.className = 'gate-verdict-badge loading';
    gateBadge.textContent = 'Checking...';
    gateSummary.textContent = 'AI is analyzing readiness for ' + targetSection;
    gateConfirm.textContent = 'Move to ' + targetSection;
    gateConfirm.disabled = true;
    gateBanner.classList.remove('hidden');
  }}
  window.showGateBannerLoading = showGateBannerLoading;

  function showGateBanner(data, targetSection) {{
    _gateContext = {{ targetSection: targetSection, ticketId: currentTicketId }};
    var verdict = data.verdict || 'needs-work';
    gateBadge.className = 'gate-verdict-badge ' + verdict;
    gateBadge.textContent = verdict.replace(/-/g, ' ');
    gateSummary.textContent = data.summary || '';
    gateConfirm.textContent = 'Move to ' + targetSection;
    gateConfirm.disabled = false;
    gateBanner.classList.remove('hidden');
  }}

  function hideGateBanner() {{
    gateBanner.classList.add('hidden');
    _gateContext = null;
  }}

  gateConfirm.addEventListener('click', function() {{
    if (!_gateContext) return;
    var tid = _gateContext.ticketId;
    var section = _gateContext.targetSection;
    if (section === 'Done') {{
      fetch(EDIT_API + '/tickets/' + tid + '/accept', {{
        method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: '{{}}'
      }}).then(function(r) {{ return r.json(); }}).then(function() {{
        hideGateBanner(); closeOverlay();
        var card = document.querySelector('[data-item-id="' + tid + '"]');
        if (card && window.showToast) window.showToast(card, 'Accepted!');
      }});
    }} else {{
      fetch(EDIT_API + '/tickets/' + tid + '/move', {{
        method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ section: section }})
      }}).then(function(r) {{ return r.json(); }}).then(function() {{
        hideGateBanner(); closeOverlay();
        var card = document.querySelector('[data-item-id="' + tid + '"]');
        if (card && window.showToast) window.showToast(card, 'Moved!');
      }});
    }}
  }});

  gateCancel.addEventListener('click', function() {{
    hideGateBanner();
    // Close overlay and clear hash on cancel (I-11)
    closeOverlay();
  }});

  function populateAssessment(data) {{
    var cats = data.categories || {{}};
    ['D', 'C', 'T', 'R', 'S'].forEach(function(key) {{
      if (cats[key]) renderCategoryAssessment(key, cats[key]);
    }});
    // Update section-flag indicators for needs-work
    overlay.querySelectorAll('.section-flag').forEach(function(sf) {{
      var cat = sf.dataset.cat;
      if (cat && cats[cat] && cats[cat].status === 'needs-work') {{
        sf.style.borderColor = '#eab308'; sf.style.color = '#eab308';
      }}
    }});
  }}

  function populateCriteria(data) {{
    var list = overlay.querySelector('.detail-criteria-list');
    while (list.firstChild) list.removeChild(list.firstChild);
    (data.acceptance_criteria || []).forEach(function(c, i) {{
      var li = document.createElement('li'); li.className = 'detail-criteria-item';
      var bullet = document.createElement('span'); bullet.className = 'criteria-bullet'; bullet.textContent = '\\u2022';
      var sp = document.createElement('span'); sp.className = 'criteria-text'; sp.textContent = c.text;
      // Click to edit criterion text inline
      sp.addEventListener('click', function() {{
        if (sp.contentEditable === 'true') return;
        sp.contentEditable = 'true'; sp.focus();
        var range = document.createRange(); range.selectNodeContents(sp);
        var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
      }});
      sp.addEventListener('blur', function() {{
        sp.contentEditable = 'false';
        var newText = sp.textContent.trim();
        if (newText && newText !== c.text) {{
          fetch(EDIT_API + '/tickets/' + data.id, {{ method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{criterion_index:i, criterion_text:newText}}) }})
            .then(function(r){{return r.json();}}).then(function(u){{ if(u) {{ currentData=u; toast('Criterion updated'); }} }});
        }}
      }});
      sp.addEventListener('keydown', function(e) {{ if(e.key==='Enter'){{ e.preventDefault(); sp.blur(); }} if(e.key==='Escape'){{ sp.textContent=c.text; sp.blur(); }} }});
      var del = document.createElement('button'); del.className = 'criteria-delete'; del.textContent = '\\u00d7';
      del.title = 'Remove criterion';
      del.addEventListener('click', function() {{
        fetch(EDIT_API + '/tickets/' + data.id, {{ method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{remove_criterion:i}}) }})
          .then(function(r){{return r.json();}}).then(function(u){{
            if(u) {{ currentData=u; populateCriteria(u); refreshDCTRS(u); toast('Criterion removed'); }}
          }});
      }});
      li.appendChild(bullet); li.appendChild(sp); li.appendChild(del); list.appendChild(li);
    }});
  }}

  /* --- List-style fields (Tests, Smoke) --- */
  function serializeListField(field) {{
    var ul = overlay.querySelector('[data-list-field="' + field + '"]');
    if (!ul) return '';
    var items = [];
    ul.querySelectorAll('.criteria-text').forEach(function(sp) {{
      var t = sp.textContent.trim();
      if (t) items.push(t);
    }});
    return items.join('\\n');
  }}

  function saveListField(field) {{
    var val = serializeListField(field);
    fetch(EDIT_API+'/tickets/'+currentTicketId+'/readiness/'+field, {{method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{content:val}})}})
      .then(function(r){{return r.json();}}).then(function(u){{ if(u) {{ currentData=u; refreshDCTRS(currentData); }} toast(FLAG_NAMES[field]+' saved'); }});
  }}

  function populateListField(field, rawText) {{
    var ul = overlay.querySelector('[data-list-field="' + field + '"]');
    if (!ul) return;
    ul.innerHTML = '';
    var lines = (rawText || '').split('\\n').filter(function(l){{ return l.trim(); }});
    lines.forEach(function(line) {{
      var li = document.createElement('li'); li.className = 'detail-criteria-item';
      var bullet = document.createElement('span'); bullet.className = 'criteria-bullet'; bullet.textContent = '\\u2022';
      var sp = document.createElement('span'); sp.className = 'criteria-text'; sp.textContent = line.trim();
      sp.addEventListener('click', function() {{ sp.contentEditable = 'true'; sp.focus(); }});
      sp.addEventListener('blur', function() {{ sp.contentEditable = 'false'; saveListField(field); }});
      sp.addEventListener('keydown', function(e) {{
        if (e.key === 'Enter') {{ e.preventDefault(); sp.blur(); }}
        if (e.key === 'Escape') {{ e.preventDefault(); e.stopPropagation(); sp.textContent = line.trim(); sp.blur(); }}
      }});
      var del = document.createElement('button'); del.className = 'criteria-delete'; del.textContent = '\\u00d7';
      del.addEventListener('click', function() {{ li.remove(); saveListField(field); toast('Item removed'); }});
      li.appendChild(bullet); li.appendChild(sp); li.appendChild(del); ul.appendChild(li);
    }});
  }}

  // Wire up list-field add inputs (tests, smoke)
  overlay.querySelectorAll('[data-list-add]').forEach(function(input) {{
    var field = input.dataset.listAdd;
    input.addEventListener('keydown', function(e) {{
      if (e.key === 'Escape') {{ e.preventDefault(); e.stopPropagation(); input.value = ''; input.blur(); return; }}
      if (e.key !== 'Enter') return;
      e.preventDefault();
      var text = input.value.trim();
      if (!text) return;
      input.value = '';
      // Add to list and save
      var ul = overlay.querySelector('[data-list-field="' + field + '"]');
      if (ul) {{
        var current = serializeListField(field);
        var newVal = current ? current + '\\n' + text : text;
        fetch(EDIT_API+'/tickets/'+currentTicketId+'/readiness/'+field, {{method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{content:newVal}})}})
          .then(function(r){{return r.json();}}).then(function(u){{
            if(u) {{ currentData=u; var fl=u.readiness_content||{{}}; populateListField(field, fl[field]||''); refreshDCTRS(u); }}
            toast('Item added');
          }});
      }}
    }});
  }});

  /* --- Inline auto-save for textarea editors --- */
  function setupInlineEditors() {{
    overlay.querySelectorAll('.detail-editor').forEach(function(ed) {{
      var field = ed.dataset.field;
      ed._origValue = ed.value;
      ed.addEventListener('focus', function() {{ _editingField = field; ed._origValue = ed.value; }});
      ed.addEventListener('keydown', function(e) {{
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {{ e.preventDefault(); ed.blur(); }}
        if (e.key === 'Escape') {{ e.preventDefault(); e.stopPropagation(); ed.value = ed._origValue || ''; ed.blur(); }}
      }});
      ed.addEventListener('blur', function() {{
        _editingField = null;
        var val = ed.value;
        if (val === ed._origValue) return;
        ed._origValue = val;
        if (field === 'description') {{
          autosaveField('description', val).then(function() {{ toast('Description saved'); refreshDCTRS(currentData); }});
        }} else {{
          // readiness flag: tests, reviewed, smoke
          fetch(EDIT_API+'/tickets/'+currentTicketId+'/readiness/'+field, {{method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{content:val}})}})
            .then(function(r){{return r.json();}}).then(function(u){{ if(u)currentData=u; toast(FLAG_NAMES[field]+' saved'); refreshDCTRS(currentData); }});
        }}
      }});
    }});
  }}
  setupInlineEditors();

  /* --- Criteria add input (Enter to commit) --- */
  var criteriaInput = overlay.querySelector('.criteria-add-input');
  if (criteriaInput) {{
    criteriaInput.addEventListener('keydown', function(e) {{
      if (e.key === 'Escape') {{ e.preventDefault(); e.stopPropagation(); criteriaInput.value = ''; criteriaInput.blur(); return; }}
      if (e.key !== 'Enter') return;
      e.preventDefault();
      var text = criteriaInput.value.trim();
      if (!text || !currentTicketId) return;
      criteriaInput.value = '';
      fetch(EDIT_API+'/tickets/'+currentTicketId, {{method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{add_criteria:text}})}})
        .then(function(r){{return r.json();}})
        .then(function(u) {{
          if (u) {{ currentData = u; populateCriteria(u); refreshDCTRS(u); }}
          toast('Criterion added');
        }});
    }});
  }}

  function populate(data) {{
    // Invalidate AI cache for this ticket (data may have changed)
    Object.keys(_assessCache).forEach(function(k) {{ if (k.startsWith(data.id + ':')) delete _assessCache[k]; }});
    currentData = data;
    idEl.textContent = data.id;
    titleEl.textContent = data.title;
    titleEl.contentEditable = 'false';
    var pathEl = overlay.querySelector('.detail-path');
    if (pathEl) pathEl.textContent = 'docs/features/' + data.id + '/';
    overlay.querySelector('[data-field="description"]').value = data.description || '';
    populateCriteria(data);
    var fl = data.readiness_content || data.readiness_flags || {{}};
    // Tests and Smoke are list-style fields
    populateListField('tests', fl['tests'] || '');
    populateListField('smoke', fl['smoke'] || '');
    // Reviewed (Learnings) stays as textarea
    var reviewEd = overlay.querySelector('[data-field="reviewed"]');
    if(reviewEd) {{ reviewEd.value = fl['reviewed'] || ''; reviewEd._origValue = reviewEd.value; }}
    // Description orig value
    var descEd = overlay.querySelector('[data-field="description"]');
    if(descEd) descEd._origValue = descEd.value;
    populateMetaChips(data);
    refreshDCTRS(data);
  }}

  function openOverlay(tid, section) {{
    currentTicketId = tid;
    if (!_hasAssessmentData) clearAssessments();
    hideGateBanner();
    overlay.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    // Resolve section — could be a flag letter or old tab name
    var scrollFlag = null;
    if (section) {{
      if (TAB_COMPAT[section] !== undefined) scrollFlag = TAB_COMPAT[section]; // old tab name
      else if (CAT_RMAP[section]) scrollFlag = section; // flag letter like 'D'
      else scrollFlag = CAT_MAP[section] || null; // section name like 'description'
    }}
    fetch(EDIT_API+'/tickets/'+tid).then(function(r){{return r.json();}}).then(function(d){{
      populate(d);
      if (scrollFlag) {{ setTimeout(function() {{ scrollToSection(CAT_RMAP[scrollFlag]); }}, 50); }}
      else {{ var body = overlay.querySelector('.detail-body'); if(body) body.scrollTop = 0; }}
      // Set ticket hash (I-11)
      if (!window.location.hash || window.location.hash.indexOf('#gate/') !== 0) {{
        var ticketHash = '#ticket/' + tid + (scrollFlag ? '/' + scrollFlag : '');
        if (window.location.hash !== ticketHash) {{
          history.pushState({{ ticket: true, id: tid, flag: scrollFlag }}, '', ticketHash);
        }}
      }}
      // Load live run panel (M3)
      loadRuns(tid);
    }});
  }}

  function closeOverlay() {{
    closeStatusDropdown();
    _stopRunsPolling();
    overlay.classList.add('hidden');
    document.body.style.overflow = '';
    currentTicketId = null; currentData = null;
    _hasAssessmentData = false; _gateContext = null;
    clearAssessments(); hideGateBanner();
    if (window.location.hash && (window.location.hash.indexOf('#gate/') === 0 || window.location.hash.indexOf('#ticket/') === 0)) {{
      history.pushState({{ gate: false }}, '', window.location.pathname + window.location.search);
    }}
  }}

  overlay.querySelector('.detail-backdrop').addEventListener('click', closeOverlay);
  overlay.querySelector('.detail-close').addEventListener('click', closeOverlay);
  overlay.querySelector('.detail-path').addEventListener('click', function(e) {{
    e.stopPropagation();
    navigator.clipboard.writeText(this.textContent).then(function() {{ toast('Path copied'); }});
  }});
  document.addEventListener('keydown', function(e) {{
    if (e.key !== 'Escape' || overlay.classList.contains('hidden')) return;
    var active = document.activeElement;
    if (active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT' || active.contentEditable === 'true')) return;
    closeOverlay();
  }});

  // DCTRS dots in header — scroll to section
  overlay.querySelectorAll('.detail-dctrs-strip .readiness-dot').forEach(function(dot) {{
    dot.addEventListener('click', function(e) {{
      e.stopPropagation();
      scrollToSection(dot.dataset.flag);
    }});
  }});

  // Assess buttons (single button per section)
  overlay.querySelectorAll('.section-assess-btn').forEach(function(btn) {{
    btn.addEventListener('click', function(e) {{
      if(!currentData || !currentTicketId) return;
      var cat = btn.dataset.cat;
      if (!cat) return;
      var sec = CAT_RMAP[cat];
      var fl = currentData.readiness_flags || {{}};
      var hasContent = sec === 'description' ? !!(currentData.description) : sec === 'criteria' ? (currentData.acceptance_criteria || []).length > 0 : !!(fl[sec]);
      var action = hasContent ? 'review' : 'create';
      // Shift+click copies prompt to clipboard as fallback
      if (e.shiftKey) {{
        var t = currentData;
        var prompts = {{
          D: {{ create: 'Write a detailed description for ' + t.id + ': "' + t.title + '". Include problem statement, proposed solution, scope, and constraints.',
                review: 'Review the description for ' + t.id + ': "' + t.title + '".\\n\\nDescription:\\n' + (t.description || '(empty)') }},
          C: {{ create: 'Write acceptance criteria for ' + t.id + ': "' + t.title + '". Use Given/When/Then format.\\n\\nDescription:\\n' + (t.description || '(empty)'),
                review: 'Review acceptance criteria for ' + t.id + ': "' + t.title + '".\\n\\nCriteria:\\n' + (t.criteria_text || '(none)') }},
          T: {{ create: 'Write test definitions for ' + t.id + ': "' + t.title + '".\\n\\nCriteria:\\n' + (t.criteria_text || '(none)'),
                review: 'Review test definitions for ' + t.id + ': "' + t.title + '".' }},
          R: {{ create: 'Perform a review for ' + t.id + ': "' + t.title + '".\\n\\nDescription:\\n' + (t.description || '(empty)'),
                review: 'Review the review notes for ' + t.id + ': "' + t.title + '".' }},
          S: {{ create: 'Create a smoke test plan for ' + t.id + ': "' + t.title + '".\\n\\nCriteria:\\n' + (t.criteria_text || '(none)'),
                review: 'Review smoke test results for ' + t.id + ': "' + t.title + '".' }}
        }};
        var p = prompts[cat] && prompts[cat][action];
        if (p) navigator.clipboard.writeText(p).then(function(){{ toast('Prompt copied'); }});
        return;
      }}
      btn.textContent = 'Assessing...'; btn.classList.add('loading');
      var _origLabel = hasContent ? 'Re-assess' : 'Assess';
      var _restore = function() {{ btn.textContent = _origLabel; btn.classList.remove('loading'); }};
      runCategoryAssess(cat, action, _restore, true);  // force refresh — user explicitly clicked
    }});
  }});

  // Ctrl+S saves the focused textarea
  overlay.addEventListener('keydown', function(e) {{
    if((e.ctrlKey||e.metaKey) && e.key==='s') {{
      e.preventDefault();
      var focused = document.activeElement;
      if (focused && focused.classList && focused.classList.contains('detail-editor')) {{
        focused.blur(); // triggers auto-save
      }}
    }}
  }});

  // Readiness dot click on cards — open detail view scrolled to section
  document.addEventListener('click', function(e) {{
    var dot = e.target.closest('.readiness-dot[data-flag]');
    if(!dot) return;
    // Skip dots inside the overlay header strip
    if (dot.closest('.detail-dctrs-strip')) return;
    var card = dot.closest('.card') || dot.closest('.list-row');
    if(!card || !card.dataset.itemId) return;
    e.stopPropagation(); e.preventDefault();
    if(card._clickTimer) clearTimeout(card._clickTimer);
    openOverlay(card.dataset.itemId, dot.dataset.flag);
  }}, true);

  // Expose for gate-check integration
  window.DETAIL_OVERLAY_OPEN = function() {{ return currentTicketId; }};
  window.openDetailOverlay = openOverlay;
  window.populateAssessment = populateAssessment;
  window.showGateBanner = showGateBanner;
  window.closeDetailOverlay = closeOverlay;

  // --- URL hash routing (I-11) ---
  function _parseGateHash(hash) {{
    if (!hash || hash.indexOf('#gate/') !== 0) return null;
    var parts = hash.substring(6).split('/');
    if (parts.length < 2) return null;
    return {{ ticketId: parts[0], section: decodeURIComponent(parts.slice(1).join('/')) }};
  }}

  function _parseTicketHash(hash) {{
    if (!hash || hash.indexOf('#ticket/') !== 0) return null;
    var parts = hash.substring(8).split('/');
    if (parts.length < 1 || !parts[0]) return null;
    var rawFlag = parts[1] || '';
    // Backward compat: old tab names → flag letters
    var flag = TAB_COMPAT.hasOwnProperty(rawFlag) ? TAB_COMPAT[rawFlag] : rawFlag;
    return {{ ticketId: parts[0], flag: flag || null }};
  }}

  var _suppressPopstate = false;

  window.addEventListener('popstate', function() {{
    if (_suppressPopstate) {{ _suppressPopstate = false; return; }}
    var gateP = _parseGateHash(window.location.hash);
    if (gateP) {{
      if (!overlay.classList.contains('hidden') && currentTicketId === gateP.ticketId) return;
      if (window.startGateCheck) window.startGateCheck(gateP.ticketId, gateP.section);
      return;
    }}
    var ticketP = _parseTicketHash(window.location.hash);
    if (ticketP) {{
      if (!overlay.classList.contains('hidden') && currentTicketId === ticketP.ticketId) {{
        if (ticketP.flag) scrollToSection(CAT_RMAP[ticketP.flag]);
        return;
      }}
      openOverlay(ticketP.ticketId, ticketP.flag);
      return;
    }}
    if (!overlay.classList.contains('hidden')) {{
      overlay.classList.add('hidden');
      document.body.style.overflow = '';
      currentTicketId = null; currentData = null;
      _hasAssessmentData = false; _gateContext = null;
      clearAssessments(); hideGateBanner();
    }}
  }});

  (function() {{
    var gateP = _parseGateHash(window.location.hash);
    if (gateP && window.startGateCheck) {{
      setTimeout(function() {{ window.startGateCheck(gateP.ticketId, gateP.section); }}, 200);
      return;
    }}
    var ticketP = _parseTicketHash(window.location.hash);
    if (ticketP) {{
      setTimeout(function() {{ openOverlay(ticketP.ticketId, ticketP.flag); }}, 200);
    }}
  }})();
}})();
</script>

<script>
/* =========================================================
   Task 9: Draft filter toggle
   ========================================================= */
(function() {{
  var draftsBtn = document.getElementById('draftsToggleBtn');
  if (!draftsBtn) return;
  var showDrafts = localStorage.getItem('tt-show-drafts') === '1';
  if (showDrafts && draftsBtn) draftsBtn.classList.add('active');

  function applyDraftVisibility() {{
    var draftCards = document.querySelectorAll('.card.is-draft');
    draftCards.forEach(function(c) {{
      if (!showDrafts) {{
        c.style.display = 'none';
      }} else {{
        c.style.display = '';
      }}
    }});
  }}

  // Hide drafts by default on load
  applyDraftVisibility();

  draftsBtn.addEventListener('click', function() {{
    showDrafts = !showDrafts;
    localStorage.setItem('tt-show-drafts', showDrafts ? '1' : '0');
    draftsBtn.classList.toggle('active', showDrafts);
    applyDraftVisibility();
  }});
}})();
</script>

<script>
/* Seek button handler */
(function() {{
  var seekBtn = document.getElementById('seekBtn');
  var editApiMeta = document.querySelector('meta[name="edit-api"]');
  var EDIT_API = editApiMeta ? editApiMeta.content : null;
  if (!seekBtn || !EDIT_API) return;

  function doSeek(btn, origText) {{
    btn.disabled = true;
    btn.textContent = 'Seeking\u2026';
    fetch(EDIT_API + '/seek', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: '{{}}'
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(result) {{
      btn.disabled = false;
      btn.textContent = origText;
      if (result.created > 0) {{
        showAppToast(result.created + ' draft(s) created', 'success');
        localStorage.setItem('tt-show-drafts', '1');
        location.reload();
      }} else if (result.discovered > 0) {{
        showAppToast('All ' + result.discovered + ' items already tracked', 'success');
      }} else {{
        showAppToast('No ticket-like items found', 'success');
      }}
    }})
    .catch(function() {{
      btn.disabled = false;
      btn.textContent = origText;
      showAppToast('Seek failed', 'error');
    }});
  }}

  seekBtn.addEventListener('click', function() {{
    doSeek(seekBtn, seekBtn.textContent);
  }});
  window._doSeek = doSeek;
}})();
</script>

<script>
/* Empty state handler */
(function() {{
  var emptyState = document.getElementById('emptyState');
  var kanban = document.getElementById('kanban');
  if (!emptyState || !kanban) return;

  var realCards = kanban.querySelectorAll('.card:not(.is-draft)');
  if (realCards.length === 0) {{
    emptyState.style.display = 'flex';
    kanban.style.display = 'none';
  }}

  var createBtn = document.getElementById('emptyStateCreate');
  if (createBtn) {{
    createBtn.addEventListener('click', function() {{
      emptyState.style.display = 'none';
      kanban.style.display = '';
      var newBtn = document.getElementById('newTicketBtn');
      if (newBtn) newBtn.click();
    }});
  }}

  var seekCta = document.getElementById('emptyStateSeek');
  if (seekCta && window._doSeek) {{
    seekCta.addEventListener('click', function() {{
      window._doSeek(seekCta, seekCta.textContent);
    }});
  }}
}})();
</script>

<script>
/* =========================================================
   Task 9: Draft confirm/reject in detail overlay
   ========================================================= */
(function() {{
  var editApiMeta = document.querySelector('meta[name="edit-api"]');
  var EDIT_API = editApiMeta ? editApiMeta.content : null;
  if (!EDIT_API) return;

  // We hook into the overlay open event by watching when the overlay is made visible
  // and checking if current ticket is a draft — then we show confirm/reject buttons.

  var overlay = document.getElementById('ticket-detail-overlay');
  if (!overlay) return;

  var _draftBanner = null;

  function removeDraftBanner() {{
    if (_draftBanner && _draftBanner.parentNode) {{
      _draftBanner.parentNode.removeChild(_draftBanner);
    }}
    _draftBanner = null;
  }}

  function showDraftBanner(ticketId) {{
    removeDraftBanner();
    var body = overlay.querySelector('.detail-body');
    if (!body) return;

    var banner = document.createElement('div');
    banner.style.cssText = 'padding:10px 14px;margin-bottom:12px;border-radius:8px;background:rgba(234,179,8,0.08);border:1px solid rgba(234,179,8,0.3);display:flex;align-items:center;gap:10px;';

    var label = document.createElement('span');
    label.style.cssText = 'font-size:11px;font-weight:700;color:#eab308;text-transform:uppercase;letter-spacing:0.3px;flex-shrink:0;';
    label.textContent = 'DRAFT';

    var msg = document.createElement('span');
    msg.style.cssText = 'font-size:12px;color:var(--text-secondary);flex:1;';
    var cardEl = document.querySelector('[data-item-id="' + ticketId + '"]');
    var desc = (cardEl && cardEl.dataset.desc) || '';
    if (desc.startsWith('Source: ')) {{
      var srcType = desc.split(' ')[1];
      var labels = {{code_todo:'a code comment',md_task:'a markdown task',readme_todo:'a README item',changelog:'a changelog entry',github_issue:'a GitHub issue'}};
      msg.textContent = 'Auto-generated from ' + (labels[srcType] || 'project files') + '. Confirm to keep or reject to discard.';
    }} else {{
      msg.textContent = 'This ticket was auto-generated. Confirm to keep or reject to discard.';
    }}

    var confirmBtn = document.createElement('button');
    confirmBtn.style.cssText = 'font-size:11px;padding:4px 12px;border-radius:5px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-weight:600;font-family:var(--font-sans);';
    confirmBtn.textContent = 'Confirm';
    confirmBtn.addEventListener('click', function() {{
      fetch(EDIT_API + '/tickets/' + ticketId, {{
        method: 'PUT',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ draft: false }})
      }}).then(function() {{
        removeDraftBanner();
        // Remove is-draft class from the card
        var card = document.querySelector('[data-item-id="' + ticketId + '"]');
        if (card) {{ card.classList.remove('is-draft'); card.removeAttribute('data-draft'); card.style.display = ''; }}
      }}).catch(function() {{ showAppToast('Failed to confirm ticket', 'error'); }});
    }});

    var rejectBtn = document.createElement('button');
    rejectBtn.style.cssText = 'font-size:11px;padding:4px 10px;border-radius:5px;border:1px solid rgba(239,68,68,0.5);background:none;color:#ef4444;cursor:pointer;font-weight:600;font-family:var(--font-sans);';
    rejectBtn.textContent = 'Reject';
    rejectBtn.addEventListener('click', function() {{
      showConfirmModal('Delete Draft', 'Delete this draft ticket? This cannot be undone.', 'Delete', function() {{
        fetch(EDIT_API + '/tickets/' + ticketId, {{
          method: 'DELETE'
        }}).then(function() {{
          if (window.closeDetailOverlay) window.closeDetailOverlay();
          var card = document.querySelector('[data-item-id="' + ticketId + '"]');
          if (card) card.remove();
          showAppToast('Draft deleted', 'success');
        }}).catch(function() {{ showAppToast('Failed to reject ticket', 'error'); }});
      }});
    }});

    banner.appendChild(label);
    banner.appendChild(msg);
    banner.appendChild(confirmBtn);
    banner.appendChild(rejectBtn);
    _draftBanner = banner;

    // Insert at top of body (before the gate banner)
    body.insertBefore(banner, body.firstChild);
  }}

  // Hook into openDetailOverlay — wrap it
  var _origOpen = window.openDetailOverlay;
  window.openDetailOverlay = function(tid, section) {{
    removeDraftBanner();
    if (_origOpen) _origOpen(tid, section);
    // After data loads, check if draft
    setTimeout(function() {{
      var card = document.querySelector('[data-item-id="' + tid + '"]');
      if (card && card.dataset.draft === 'true') {{
        showDraftBanner(tid);
      }}
    }}, 300);
  }};

  // Also patch closeDetailOverlay to clean up banner
  var _origClose = window.closeDetailOverlay;
  window.closeDetailOverlay = function() {{
    removeDraftBanner();
    if (_origClose) _origClose();
  }};
}})();
</script>

<script>
/* =========================================================
   Task 10: Settings panel
   ========================================================= */
(function() {{
  var editApiMeta = document.querySelector('meta[name="edit-api"]');
  var EDIT_API = editApiMeta ? editApiMeta.content : null;

  // Theme toggle (works without edit-api)
  var themeToggle = document.getElementById('themeToggle');
  if (themeToggle) {{
    var currentTheme = localStorage.getItem('tt-theme') || 'system';
    themeToggle.querySelectorAll('.theme-opt').forEach(function(btn) {{
      if (btn.dataset.theme === currentTheme) btn.classList.add('active');
      else btn.classList.remove('active');
      btn.addEventListener('click', function() {{
        themeToggle.querySelectorAll('.theme-opt').forEach(function(b) {{ b.classList.remove('active'); }});
        btn.classList.add('active');
        var choice = btn.dataset.theme;
        localStorage.setItem('tt-theme', choice);
        var resolved = choice;
        if (choice === 'system') {{
          resolved = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
        }}
        document.documentElement.setAttribute('data-theme', resolved);
      }});
    }});
  }}

  if (!EDIT_API) return;

  /* Full-page settings toggle */
  var settingsPage = document.getElementById('settings-page');
  var backBtn = document.getElementById('settingsBackBtn');

  function openSettingsPage() {{
    document.body.classList.add('settings-open');
    if (typeof _spLoadAgents === 'function') _spLoadAgents();
    if (typeof _spLoadWorkflows === 'function') _spLoadWorkflows();
  }}
  function closeSettingsPage() {{
    document.body.classList.remove('settings-open');
  }}
  window.openSettingsPage = openSettingsPage;
  window.closeSettingsPage = closeSettingsPage;

  if (backBtn) backBtn.addEventListener('click', closeSettingsPage);

  var toggleBtn = document.getElementById('settingsToggleBtn');
  var drawer = document.getElementById('settings-drawer');
  var closeBtn = document.getElementById('settingsDrawerClose');
  if (!toggleBtn || !drawer) return;

  var enabledChk = document.getElementById('settingsFeedbacksEnabled');
  var pathInput = document.getElementById('settingsFeedbacksPath');
  var autostartChk = document.getElementById('settingsFeedbacksAutostart');
  var autostartHint = document.getElementById('settingsAutostartHint');
  var statusDot = document.getElementById('feedbacksStatusDot');
  var statusLabel = document.getElementById('feedbacksStatusLabel');
  var installBtn = document.getElementById('settingsFeedbacksInstall');

  function openDrawer() {{
    drawer.classList.remove('hidden');
    loadSettings().then(function() {{ checkFeedbacksStatus(); }});
    loadManagedFiles();
  }}

  function closeDrawer() {{
    drawer.classList.add('hidden');
  }}

  toggleBtn.addEventListener('click', function() {{
    if (settingsPage) {{
      if (document.body.classList.contains('settings-open')) {{
        closeSettingsPage();
      }} else {{
        openSettingsPage();
        openDrawer();
      }}
      return;
    }}
    if (drawer.classList.contains('hidden')) {{
      openDrawer();
    }} else {{
      closeDrawer();
    }}
  }});

  if (closeBtn) closeBtn.addEventListener('click', closeDrawer);

  // Close when clicking outside
  document.addEventListener('click', function(e) {{
    if (!drawer.classList.contains('hidden') &&
        !drawer.contains(e.target) &&
        e.target !== toggleBtn) {{
      closeDrawer();
    }}
  }});

  function loadSettings() {{
    return fetch(EDIT_API + '/settings')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (enabledChk) enabledChk.checked = (data['feedbacks.enabled'] === 'true' || data['feedbacks.enabled'] === 'True' || data['feedbacks.enabled'] === true);
        if (pathInput) pathInput.value = data['feedbacks.home'] || '';
        var autoVal = data['feedbacks.autostart'];
        if (autostartChk) autostartChk.checked = (autoVal === 'true' || autoVal === 'True' || autoVal === true);
      }})
      .catch(function() {{ /* settings endpoint may not exist yet */ }});
  }}

  function saveSettings(patch) {{
    return fetch(EDIT_API + '/settings', {{
      method: 'PUT',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(patch)
    }}).catch(function() {{ /* ignore save errors */ }});
  }}

  function checkFeedbacksStatus() {{
    if (!statusDot) return;
    fetch(EDIT_API + '/feedbacks/status')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        statusDot.className = 'settings-status-dot';
        var label = '';
        if (!data.installed) {{
          statusDot.classList.add('err');
          label = 'Not installed';
        }} else if (!data.enabled) {{
          label = '';
        }} else if (data.running) {{
          statusDot.classList.add('ok');
          label = 'Server running';
        }} else {{
          statusDot.classList.add('warn');
          label = 'Server not running';
        }}
        statusDot.title = label;
        if (statusLabel) statusLabel.textContent = label;
        // Enable toggle: disabled until installed
        if (enabledChk) {{
          enabledChk.disabled = !data.installed;
          if (!data.installed) enabledChk.checked = false;
        }}
        // Path input: gray out when disabled
        if (pathInput) {{
          pathInput.disabled = !data.enabled;
          pathInput.style.opacity = data.enabled ? '1' : '0.4';
        }}
        // Autostart toggle: disabled when feedbacks not enabled
        if (autostartChk) {{
          autostartChk.disabled = !data.enabled;
        }}
        if (autostartHint) {{
          autostartHint.style.opacity = data.enabled ? '1' : '0.4';
        }}
        // Install button: "Re-install" if already installed
        if (installBtn) {{
          installBtn.textContent = data.installed ? 'Re-install' : 'Install';
        }}
      }})
      .catch(function() {{
        statusDot.className = 'settings-status-dot err';
        statusDot.title = 'Could not check feedbacks status';
        if (enabledChk) enabledChk.disabled = true;
      }});
  }}

  function loadManagedFiles() {{
    var container = document.getElementById('managedFilesList');
    if (!container || !EDIT_API) return;
    fetch(EDIT_API + '/managed-files')
      .then(function(r) {{ return r.json(); }})
      .then(function(files) {{
        while (container.firstChild) container.removeChild(container.firstChild);
        files.forEach(function(f) {{
          var row = document.createElement('div');
          row.className = 'managed-file-row';
          var dot = document.createElement('span');
          dot.className = 'managed-file-dot ' + (f.exists ? 'exists' : 'missing');
          dot.title = f.exists ? 'Exists' : 'Not created yet';
          var pathEl = document.createElement('span');
          pathEl.className = 'managed-file-path';
          pathEl.textContent = f.path;
          var desc = document.createElement('span');
          desc.className = 'managed-file-desc';
          desc.textContent = f.description;
          row.appendChild(dot);
          row.appendChild(pathEl);
          if (f.gitignored) {{
            var badge = document.createElement('span');
            badge.className = 'managed-file-badge';
            badge.textContent = '.gitignored';
            row.appendChild(badge);
          }}
          row.appendChild(desc);
          container.appendChild(row);
        }});
      }})
      .catch(function() {{
        while (container.firstChild) container.removeChild(container.firstChild);
        var hint = document.createElement('div');
        hint.className = 'settings-hint';
        hint.textContent = 'Could not load managed files.';
        container.appendChild(hint);
      }});
  }}

  if (enabledChk) {{
    enabledChk.addEventListener('change', function() {{
      var enabling = enabledChk.checked;
      saveSettings({{ 'feedbacks.enabled': enabling ? 'true' : 'false' }})
        .then(function() {{
          if (enabling) {{
            // Start the feedbacks server if not already running
            statusDot.className = 'settings-status-dot warn';
            if (statusLabel) statusLabel.textContent = 'Starting\u2026';
            return fetch(EDIT_API + '/settings/feedbacks/start', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: '{{}}'
            }}).then(function() {{
              // Poll until server is up (max 15s)
              var attempts = 0;
              function pollReady() {{
                attempts++;
                return fetch(EDIT_API + '/feedbacks/status')
                  .then(function(r) {{ return r.json(); }})
                  .then(function(d) {{
                    if (d.running) return checkFeedbacksStatus();
                    if (attempts < 15) return new Promise(function(ok) {{ setTimeout(ok, 1000); }}).then(pollReady);
                    return checkFeedbacksStatus();
                  }});
              }}
              return pollReady();
            }}).catch(function() {{ checkFeedbacksStatus(); }});
          }} else {{
            return checkFeedbacksStatus();
          }}
        }});
    }});
  }}

  if (pathInput) {{
    pathInput.addEventListener('blur', function() {{
      saveSettings({{ 'feedbacks.home': pathInput.value }});
    }});
  }}

  if (autostartChk) {{
    autostartChk.addEventListener('change', function() {{
      saveSettings({{ 'feedbacks.autostart': autostartChk.checked ? 'true' : 'false' }});
    }});
  }}

  if (installBtn) {{
    installBtn.addEventListener('click', function() {{
      installBtn.disabled = true;
      installBtn.textContent = 'Installing...';
      fetch(EDIT_API + '/settings/feedbacks/install', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: '{{}}'
      }})
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        installBtn.textContent = data.ok ? 'Installed \u2714' : 'Failed';
        installBtn.disabled = false;
        if (data.install_dir && pathInput) pathInput.value = data.install_dir;
        checkFeedbacksStatus();
      }})
      .catch(function() {{
        installBtn.textContent = 'Failed';
        installBtn.disabled = false;
      }});
    }});
  }}
}})();
</script>

<script>
/* =========================================================
   Task 10.5: Workflow Agents & Workflows settings
   ========================================================= */
(function() {{
  var editApiMeta = document.querySelector('meta[name="edit-api"]');
  var EDIT_API = editApiMeta ? editApiMeta.content : null;
  if (!EDIT_API) return;

  var agentsList = document.getElementById('workflowAgentsList');
  var addAgentBtn = document.getElementById('wfAddAgentBtn');
  var agentForm = document.getElementById('wfAgentForm');
  var agentIdInput = document.getElementById('wfAgentId');
  var agentNameInput = document.getElementById('wfAgentName');
  var agentCmdInput = document.getElementById('wfAgentCmd');
  var agentArgsInput = document.getElementById('wfAgentArgs');
  var agentPromptInput = document.getElementById('wfAgentPrompt');
  var agentSaveBtn = document.getElementById('wfAgentSave');
  var agentCancelBtn = document.getElementById('wfAgentCancel');

  var workflowsList = document.getElementById('workflowsList');
  var addWorkflowBtn = document.getElementById('wfAddWorkflowBtn');
  var workflowForm = document.getElementById('wfWorkflowForm');
  var workflowIdInput = document.getElementById('wfWorkflowId');
  var workflowNameInput = document.getElementById('wfWorkflowName');
  var workflowDescInput = document.getElementById('wfWorkflowDesc');
  var workflowSaveBtn = document.getElementById('wfWorkflowSave');
  var workflowCancelBtn = document.getElementById('wfWorkflowCancel');

  var editingAgentId = null;
  var editingWorkflowId = null;

  function loadAgents() {{
    fetch(EDIT_API + '/workflow/agents')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        var agents = data.agents || data || [];
        while (agentsList.firstChild) agentsList.removeChild(agentsList.firstChild);
        agents.forEach(function(a) {{
          var row = document.createElement('div');
          row.className = 'wf-agent-row' + (a.source === 'project' ? ' readonly' : '');

          var nameSpan = document.createElement('span');
          nameSpan.className = 'wf-row-name';
          nameSpan.textContent = a.name || a.id;
          row.appendChild(nameSpan);

          var cmdSpan = document.createElement('span');
          cmdSpan.className = 'wf-row-cmd';
          cmdSpan.textContent = a.command || 'claude';
          row.appendChild(cmdSpan);

          var sourceSpan = document.createElement('span');
          sourceSpan.className = 'wf-row-source';
          sourceSpan.textContent = a.source || 'user';
          row.appendChild(sourceSpan);

          if (a.source !== 'project') {{
            var actions = document.createElement('div');
            actions.className = 'wf-row-actions';

            var editBtn = document.createElement('button');
            editBtn.textContent = 'Edit';
            editBtn.addEventListener('click', function() {{ editAgent(a); }});
            actions.appendChild(editBtn);

            var delBtn = document.createElement('button');
            delBtn.textContent = 'Del';
            delBtn.className = 'danger';
            delBtn.addEventListener('click', function() {{ deleteAgent(a.id); }});
            actions.appendChild(delBtn);

            row.appendChild(actions);
          }}

          agentsList.appendChild(row);
        }});
      }})
      .catch(function() {{}});
  }}

  function editAgent(a) {{
    editingAgentId = a.id;
    agentIdInput.value = a.id;
    agentIdInput.disabled = true;
    agentNameInput.value = a.name || '';
    agentCmdInput.value = a.cmd || 'claude';
    agentArgsInput.value = JSON.stringify(a.args || []);
    agentPromptInput.value = a.prompt || '';
    agentForm.classList.remove('hidden');
  }}

  function deleteAgent(id) {{
    fetch(EDIT_API + '/workflow/agents/' + encodeURIComponent(id), {{ method: 'DELETE' }})
      .then(function() {{ loadAgents(); }})
      .catch(function() {{}});
  }}

  if (addAgentBtn) {{
    addAgentBtn.addEventListener('click', function() {{
      editingAgentId = null;
      agentIdInput.value = '';
      agentIdInput.disabled = false;
      agentNameInput.value = '';
      agentCmdInput.value = 'claude';
      agentArgsInput.value = '[]';
      agentPromptInput.value = '';
      agentForm.classList.remove('hidden');
    }});
  }}

  if (agentCancelBtn) {{
    agentCancelBtn.addEventListener('click', function() {{
      agentForm.classList.add('hidden');
      editingAgentId = null;
    }});
  }}

  if (agentSaveBtn) {{
    agentSaveBtn.addEventListener('click', function() {{
      var payload = {{
        id: agentIdInput.value.trim(),
        name: agentNameInput.value.trim(),
        command: agentCmdInput.value.trim() || 'claude',
        args: agentArgsInput.value.trim() || '[]',
        system_prompt: agentPromptInput.value
      }};
      var method = editingAgentId ? 'PUT' : 'POST';
      var url = editingAgentId
        ? EDIT_API + '/workflow/agents/' + encodeURIComponent(editingAgentId)
        : EDIT_API + '/workflow/agents';
      fetch(url, {{
        method: method,
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
      }})
        .then(function(r) {{
          if (r.ok) {{
            agentForm.classList.add('hidden');
            editingAgentId = null;
            loadAgents();
          }} else {{
            r.json().then(function(d) {{ showAppToast(d.error || 'Failed to save', 'error'); }});
          }}
        }})
        .catch(function() {{ showAppToast('Failed to save agent', 'error'); }});
    }});
  }}

  function loadWorkflows() {{
    fetch(EDIT_API + '/workflow/workflows')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        var workflows = data.workflows || data || [];
        while (workflowsList.firstChild) workflowsList.removeChild(workflowsList.firstChild);
        workflows.forEach(function(wf) {{
          var row = document.createElement('div');
          row.className = 'wf-workflow-row';

          var nameSpan = document.createElement('span');
          nameSpan.className = 'wf-row-name';
          nameSpan.textContent = wf.name || wf.id;
          row.appendChild(nameSpan);

          var parsedSteps = [];
          try {{ parsedSteps = typeof wf.steps === 'string' ? JSON.parse(wf.steps) : (wf.steps || []); }} catch(e) {{}}

          var stepsSpan = document.createElement('span');
          stepsSpan.className = 'wf-row-steps';
          stepsSpan.textContent = parsedSteps.length + ' steps';
          row.appendChild(stepsSpan);

          if (parsedSteps.length > 0) {{
            var stepList = document.createElement('div');
            stepList.className = 'wf-step-list';
            parsedSteps.forEach(function(step, idx) {{
              var stepRow = document.createElement('div');
              stepRow.className = 'wf-step-row';

              var idxSpan = document.createElement('span');
              idxSpan.className = 'wf-step-idx';
              idxSpan.textContent = (idx + 1) + '.';
              stepRow.appendChild(idxSpan);

              var agentSpan = document.createElement('span');
              agentSpan.textContent = step.agent || step.agent_id || '?';
              stepRow.appendChild(agentSpan);

              if (idx === 0) {{
                var primarySpan = document.createElement('span');
                primarySpan.className = 'wf-step-primary';
                primarySpan.textContent = 'primary';
                stepRow.appendChild(primarySpan);
              }}

              stepList.appendChild(stepRow);
            }});
            row.appendChild(stepList);
          }}

          var actions = document.createElement('div');
          actions.className = 'wf-row-actions';

          var editBtn = document.createElement('button');
          editBtn.textContent = 'Edit';
          editBtn.addEventListener('click', function() {{ editWorkflow(wf); }});
          actions.appendChild(editBtn);

          var delBtn = document.createElement('button');
          delBtn.textContent = 'Del';
          delBtn.className = 'danger';
          delBtn.addEventListener('click', function() {{ deleteWorkflow(wf.id); }});
          actions.appendChild(delBtn);

          row.appendChild(actions);
          workflowsList.appendChild(row);
        }});
      }})
      .catch(function() {{}});
  }}

  function editWorkflow(wf) {{
    editingWorkflowId = wf.id;
    workflowIdInput.value = wf.id;
    workflowIdInput.disabled = true;
    workflowNameInput.value = wf.name || '';
    workflowDescInput.value = wf.description || '';
    workflowForm.classList.remove('hidden');
  }}

  function deleteWorkflow(id) {{
    fetch(EDIT_API + '/workflow/workflows/' + encodeURIComponent(id), {{ method: 'DELETE' }})
      .then(function() {{ loadWorkflows(); }})
      .catch(function() {{}});
  }}

  if (addWorkflowBtn) {{
    addWorkflowBtn.addEventListener('click', function() {{
      editingWorkflowId = null;
      workflowIdInput.value = '';
      workflowIdInput.disabled = false;
      workflowNameInput.value = '';
      workflowDescInput.value = '';
      workflowForm.classList.remove('hidden');
    }});
  }}

  if (workflowCancelBtn) {{
    workflowCancelBtn.addEventListener('click', function() {{
      workflowForm.classList.add('hidden');
      editingWorkflowId = null;
    }});
  }}

  if (workflowSaveBtn) {{
    workflowSaveBtn.addEventListener('click', function() {{
      var payload = {{
        id: workflowIdInput.value.trim(),
        name: workflowNameInput.value.trim(),
        description: workflowDescInput.value.trim()
      }};
      var method = editingWorkflowId ? 'PUT' : 'POST';
      var url = editingWorkflowId
        ? EDIT_API + '/workflow/workflows/' + encodeURIComponent(editingWorkflowId)
        : EDIT_API + '/workflow/workflows';
      fetch(url, {{
        method: method,
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
      }})
        .then(function(r) {{
          if (r.ok) {{
            workflowForm.classList.add('hidden');
            editingWorkflowId = null;
            loadWorkflows();
          }} else {{
            r.json().then(function(d) {{ showAppToast(d.error || 'Failed to save', 'error'); }});
          }}
        }})
        .catch(function() {{ showAppToast('Failed to save workflow', 'error'); }});
    }});
  }}

  window._wfLoadAgents = loadAgents;
  window._wfLoadWorkflows = loadWorkflows;

  // Load on settings open
  var settingsBtn = document.getElementById('settingsToggleBtn');
  if (settingsBtn) {{
    settingsBtn.addEventListener('click', function() {{
      setTimeout(function() {{
        loadAgents();
        loadWorkflows();
      }}, 100);
    }});
  }}
}})();
</script>

<script>
/* =========================================================
   Full-page settings: Agent CRUD (spAgentList)
   ========================================================= */
(function() {{
  var editApiMeta = document.querySelector('meta[name="edit-api"]');
  var EDIT_API = editApiMeta ? editApiMeta.content : null;
  if (!EDIT_API) return;

  var listEl = document.getElementById('spAgentList');
  var formEl = document.getElementById('spAgentForm');
  var addBtn = document.getElementById('spAgentAddBtn');
  var saveBtn = document.getElementById('spAgentSaveBtn');
  var cancelBtn = document.getElementById('spAgentCancelBtn');
  var idInput = document.getElementById('spAgentId');
  var nameInput = document.getElementById('spAgentNameInput');
  var modelInput = document.getElementById('spAgentModelInput');
  var roleInput = document.getElementById('spAgentRoleInput');
  var tempInput = document.getElementById('spAgentTempInput');
  if (!listEl) return;

  var editingId = null;

  function loadAgents() {{
    fetch(EDIT_API + '/workflow/agents')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        var agents = data.agents || data || [];
        while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
        if (agents.length === 0) {{
          var empty = document.createElement('div');
          empty.style.cssText = 'font-size:12px;color:var(--text-tertiary);padding:8px 0;';
          empty.textContent = 'No agents defined yet.';
          listEl.appendChild(empty);
          return;
        }}
        agents.forEach(function(a) {{
          var row = document.createElement('div');
          row.className = 'sp-agent-item';

          var name = document.createElement('span');
          name.className = 'sp-agent-name';
          name.textContent = a.name || a.id;
          row.appendChild(name);

          var model = document.createElement('span');
          model.className = 'sp-agent-model';
          model.textContent = a.model || a.command || '';
          row.appendChild(model);

          var editBtn = document.createElement('button');
          editBtn.className = 'sp-btn';
          editBtn.textContent = 'Edit';
          editBtn.style.cssText = 'font-size:10px;padding:3px 8px;';
          editBtn.addEventListener('click', function() {{
            editingId = a.id;
            if (idInput) idInput.value = a.id;
            if (nameInput) nameInput.value = a.name || '';
            if (modelInput) modelInput.value = a.model || a.command || '';
            if (roleInput) roleInput.value = a.system_prompt || a.prompt || '';
            if (tempInput) tempInput.value = a.temperature || 0.3;
            if (formEl) formEl.style.display = '';
          }});
          row.appendChild(editBtn);

          var delBtn = document.createElement('button');
          delBtn.className = 'sp-btn';
          delBtn.textContent = 'Del';
          delBtn.style.cssText = 'font-size:10px;padding:3px 8px;color:#ef4444;';
          delBtn.addEventListener('click', function() {{
            fetch(EDIT_API + '/workflow/agents/' + encodeURIComponent(a.id), {{ method: 'DELETE' }})
              .then(function() {{ loadAgents(); }});
          }});
          row.appendChild(delBtn);

          listEl.appendChild(row);
        }});
      }})
      .catch(function() {{}});
  }}

  if (addBtn) addBtn.addEventListener('click', function() {{
    editingId = null;
    if (idInput) idInput.value = '';
    if (nameInput) nameInput.value = '';
    if (modelInput) modelInput.value = '';
    if (roleInput) roleInput.value = '';
    if (tempInput) tempInput.value = '0.3';
    if (formEl) formEl.style.display = '';
  }});

  if (cancelBtn) cancelBtn.addEventListener('click', function() {{
    if (formEl) formEl.style.display = 'none';
    editingId = null;
  }});

  if (saveBtn) saveBtn.addEventListener('click', function() {{
    var payload = {{
      id: (idInput ? idInput.value.trim() : '') || (nameInput ? nameInput.value.trim().toLowerCase().replace(/\\s+/g, '-') : ''),
      name: nameInput ? nameInput.value.trim() : '',
      command: modelInput ? modelInput.value.trim() : 'claude',
      system_prompt: roleInput ? roleInput.value : '',
      temperature: tempInput ? parseFloat(tempInput.value) : 0.3
    }};
    var method = editingId ? 'PUT' : 'POST';
    var url = editingId
      ? EDIT_API + '/workflow/agents/' + encodeURIComponent(editingId)
      : EDIT_API + '/workflow/agents';
    fetch(url, {{
      method: method,
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload)
    }})
      .then(function(r) {{
        if (r.ok) {{
          if (formEl) formEl.style.display = 'none';
          editingId = null;
          loadAgents();
        }} else {{
          r.json().then(function(d) {{ showAppToast(d.error || 'Failed to save', 'error'); }});
        }}
      }})
      .catch(function() {{ showAppToast('Failed to save agent', 'error'); }});
  }});

  window._spLoadAgents = loadAgents;
}})();
</script>

<script>
/* =========================================================
   Full-page settings: Workflow step builder (spWfList)
   ========================================================= */
(function() {{
  var editApiMeta = document.querySelector('meta[name="edit-api"]');
  var EDIT_API = editApiMeta ? editApiMeta.content : null;
  if (!EDIT_API) return;

  var listEl = document.getElementById('spWfList');
  var formEl = document.getElementById('spWfForm');
  var addBtn = document.getElementById('spWfAddBtn');
  var saveBtn = document.getElementById('spWfSaveBtn');
  var cancelBtn = document.getElementById('spWfCancelBtn');
  var idInput = document.getElementById('spWfId');
  var nameInput = document.getElementById('spWfNameInput');
  var stepList = document.getElementById('spStepList');
  var stepAddBtn = document.getElementById('spStepAddBtn');
  if (!listEl) return;

  var editingId = null;
  var currentSteps = [];

  var _cachedAgents = [];
  function _fetchAgentsForSteps() {{
    return fetch(EDIT_API + '/workflow/agents')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        _cachedAgents = (data.agents || data || []).filter(function(a) {{ return a.source !== 'project'; }});
      }})
      .catch(function() {{}});
  }}

  function renderSteps() {{
    while (stepList.firstChild) stepList.removeChild(stepList.firstChild);
    currentSteps.forEach(function(step, idx) {{
      var row = document.createElement('div');
      row.style.cssText = 'display:flex;flex-direction:column;gap:4px;padding:8px 10px;background:var(--bg-surface);border:1px solid var(--border-default);border-radius:6px;margin-bottom:4px;';

      var header = document.createElement('div');
      header.style.cssText = 'display:flex;align-items:center;gap:8px;';

      var num = document.createElement('span');
      num.style.cssText = 'font-size:10px;font-weight:700;color:var(--text-tertiary);min-width:50px;';
      num.textContent = 'Step ' + (idx + 1) + (idx === 0 ? ' (Primary)' : '');
      header.appendChild(num);

      var sel = document.createElement('select');
      sel.style.cssText = 'font-size:11px;padding:3px 6px;border:1px solid var(--border-default);border-radius:4px;background:var(--bg-card);color:var(--text-primary);flex:1;';
      var emptyOpt = document.createElement('option');
      emptyOpt.value = '';
      emptyOpt.textContent = 'Select agent...';
      sel.appendChild(emptyOpt);
      _cachedAgents.forEach(function(a) {{
        var opt = document.createElement('option');
        opt.value = a.id;
        opt.textContent = a.name;
        if (a.id === step.agent_id) opt.selected = true;
        sel.appendChild(opt);
      }});
      sel.addEventListener('change', function() {{
        currentSteps[idx].agent_id = sel.value;
        currentSteps[idx].label = sel.value ? sel.options[sel.selectedIndex].textContent : '';
      }});
      header.appendChild(sel);

      var controls = document.createElement('div');
      controls.style.cssText = 'display:flex;gap:3px;';
      if (idx > 0) {{
        var upBtn = document.createElement('button');
        upBtn.textContent = '\u2191';
        upBtn.title = 'Move up';
        upBtn.style.cssText = 'font-size:10px;padding:1px 5px;border:1px solid var(--border-default);border-radius:3px;background:none;color:var(--text-tertiary);cursor:pointer;';
        upBtn.addEventListener('click', function(e) {{ e.stopPropagation(); var tmp = currentSteps[idx]; currentSteps[idx] = currentSteps[idx-1]; currentSteps[idx-1] = tmp; renderSteps(); }});
        controls.appendChild(upBtn);
      }}
      if (idx < currentSteps.length - 1) {{
        var downBtn = document.createElement('button');
        downBtn.textContent = '\u2193';
        downBtn.title = 'Move down';
        downBtn.style.cssText = 'font-size:10px;padding:1px 5px;border:1px solid var(--border-default);border-radius:3px;background:none;color:var(--text-tertiary);cursor:pointer;';
        downBtn.addEventListener('click', function(e) {{ e.stopPropagation(); var tmp = currentSteps[idx]; currentSteps[idx] = currentSteps[idx+1]; currentSteps[idx+1] = tmp; renderSteps(); }});
        controls.appendChild(downBtn);
      }}
      var delBtn = document.createElement('button');
      delBtn.textContent = '\u00d7';
      delBtn.title = 'Remove step';
      delBtn.style.cssText = 'font-size:10px;padding:1px 5px;border:1px solid var(--border-default);border-radius:3px;background:none;color:var(--text-tertiary);cursor:pointer;';
      delBtn.addEventListener('click', function(e) {{ e.stopPropagation(); currentSteps.splice(idx, 1); renderSteps(); }});
      controls.appendChild(delBtn);
      header.appendChild(controls);

      row.appendChild(header);

      var textarea = document.createElement('textarea');
      textarea.style.cssText = 'font-size:11px;padding:5px 8px;border:1px solid var(--border-default);border-radius:4px;background:var(--bg-card);color:var(--text-primary);font-family:var(--font-mono);resize:vertical;min-height:28px;';
      textarea.placeholder = 'Step instructions (optional)';
      textarea.rows = 2;
      textarea.value = step.prompt_modifier || step.prompt || '';
      textarea.addEventListener('input', function() {{ currentSteps[idx].prompt_modifier = textarea.value; }});
      row.appendChild(textarea);

      stepList.appendChild(row);
    }});
  }}

  function loadWorkflows() {{
    fetch(EDIT_API + '/workflow/workflows')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        var workflows = data.workflows || data || [];
        while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
        if (workflows.length === 0) {{
          var empty = document.createElement('div');
          empty.style.cssText = 'font-size:12px;color:var(--text-tertiary);padding:8px 0;';
          empty.textContent = 'No workflows defined yet.';
          listEl.appendChild(empty);
          return;
        }}
        workflows.forEach(function(wf) {{
          var row = document.createElement('div');
          row.className = 'sp-wf-item';

          var name = document.createElement('span');
          name.className = 'sp-wf-name';
          name.textContent = wf.name || wf.id;
          row.appendChild(name);

          var parsedSteps = [];
          try {{ parsedSteps = typeof wf.steps === 'string' ? JSON.parse(wf.steps) : (wf.steps || []); }} catch(e) {{}}

          var steps = document.createElement('span');
          steps.className = 'sp-wf-steps';
          steps.textContent = parsedSteps.length + ' steps';
          row.appendChild(steps);

          row.addEventListener('click', function() {{
            editingId = wf.id;
            if (idInput) idInput.value = wf.id;
            if (nameInput) nameInput.value = wf.name || '';
            currentSteps = parsedSteps.slice();
            _fetchAgentsForSteps().then(function() {{ renderSteps(); }});
            if (formEl) formEl.style.display = '';
          }});

          var delBtn = document.createElement('button');
          delBtn.className = 'sp-btn';
          delBtn.textContent = 'Del';
          delBtn.style.cssText = 'font-size:10px;padding:3px 8px;color:#ef4444;';
          delBtn.addEventListener('click', function(e) {{
            e.stopPropagation();
            fetch(EDIT_API + '/workflow/workflows/' + encodeURIComponent(wf.id), {{ method: 'DELETE' }})
              .then(function() {{ loadWorkflows(); }});
          }});
          row.appendChild(delBtn);

          listEl.appendChild(row);
        }});
      }})
      .catch(function() {{}});
  }}

  if (addBtn) addBtn.addEventListener('click', function() {{
    editingId = null;
    if (idInput) idInput.value = '';
    if (nameInput) nameInput.value = '';
    currentSteps = [{{ agent_id: '', prompt_modifier: '' }}];
    _fetchAgentsForSteps().then(function() {{ renderSteps(); }});
    if (formEl) formEl.style.display = '';
  }});

  if (cancelBtn) cancelBtn.addEventListener('click', function() {{
    if (formEl) formEl.style.display = 'none';
    editingId = null;
  }});

  if (stepAddBtn) stepAddBtn.addEventListener('click', function(e) {{
    e.stopPropagation();
    currentSteps.push({{ agent_id: '', prompt_modifier: '' }});
    renderSteps();
  }});

  if (saveBtn) saveBtn.addEventListener('click', function() {{
    var payload = {{
      id: (idInput ? idInput.value.trim() : '') || (nameInput ? nameInput.value.trim().toLowerCase().replace(/\\s+/g, '-') : ''),
      name: nameInput ? nameInput.value.trim() : '',
      steps: JSON.stringify(currentSteps)
    }};
    var method = editingId ? 'PUT' : 'POST';
    var url = editingId
      ? EDIT_API + '/workflow/workflows/' + encodeURIComponent(editingId)
      : EDIT_API + '/workflow/workflows';
    fetch(url, {{
      method: method,
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload)
    }})
      .then(function(r) {{
        if (r.ok) {{
          if (formEl) formEl.style.display = 'none';
          editingId = null;
          loadWorkflows();
        }} else {{
          r.json().then(function(d) {{ showAppToast(d.error || 'Failed to save', 'error'); }});
        }}
      }})
      .catch(function() {{ showAppToast('Failed to save workflow', 'error'); }});
  }});

  window._spLoadWorkflows = loadWorkflows;
}})();
</script>

<script>
/* =========================================================
   Task 11: Attachments + Record button
   ========================================================= */
(function() {{
  var editApiMeta = document.querySelector('meta[name="edit-api"]');
  var EDIT_API = editApiMeta ? editApiMeta.content : null;
  if (!EDIT_API) return;

  var attachmentsList = document.getElementById('attachments-list');
  var linkBtn = document.getElementById('link-session-btn');

  function loadAttachments(ticketId) {{
    if (!attachmentsList) return;
    // Preserve placeholder if present
    var placeholder = attachmentsList.querySelector('.attachment-placeholder');
    // Clear non-placeholder children
    Array.from(attachmentsList.children).forEach(function(ch) {{
      if (!ch.classList.contains('attachment-placeholder')) ch.remove();
    }});

    fetch(EDIT_API + '/tickets/' + ticketId + '/attachments')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        var items = data.attachments || data || [];
        if (!Array.isArray(items) || items.length === 0) {{
          if (placeholder) return; // Keep showing placeholder, don't show "empty"
          var empty = document.createElement('div');
          empty.className = 'attachments-empty';
          empty.textContent = 'No attachments yet.';
          attachmentsList.appendChild(empty);
          return;
        }}
        // Real attachments arrived — remove placeholder
        if (placeholder) placeholder.remove();
        items.forEach(function(att) {{
          var row = document.createElement('div');
          row.className = 'attachment-row';

          var thumb = document.createElement('img');
          thumb.className = 'attachment-thumb';
          thumb.alt = 'Session thumbnail';
          if (att.thumbnail_url) {{
            thumb.src = att.thumbnail_url;
          }} else {{
            thumb.style.cssText = 'background:var(--bg-hover);';
          }}
          row.appendChild(thumb);

          var info = document.createElement('div');
          info.className = 'attachment-info';

          var summary = document.createElement('div');
          summary.className = 'attachment-summary';
          summary.textContent = att.summary || att.name || 'Feedback session';
          info.appendChild(summary);

          var meta = document.createElement('div');
          meta.className = 'attachment-meta';
          var metaParts = [];
          if (att.created_at) metaParts.push(att.created_at.substring(0, 10));
          if (att.attachment_type) metaParts.push(att.attachment_type);
          meta.textContent = metaParts.join(' \u00b7 ');
          info.appendChild(meta);

          row.appendChild(info);

          var actions = document.createElement('div');
          actions.className = 'attachment-actions';

          if (att.player_url || att.path) {{
            var openBtn = document.createElement('button');
            openBtn.className = 'attachment-action-btn';
            openBtn.textContent = 'Play';
            openBtn.addEventListener('click', function(e) {{
              e.stopPropagation();
              window.open(att.player_url || att.path, '_blank');
            }});
            actions.appendChild(openBtn);
          }}

          var unlinkBtn = document.createElement('button');
          unlinkBtn.className = 'attachment-action-btn danger';
          unlinkBtn.textContent = 'Unlink';
          unlinkBtn.addEventListener('click', function(e) {{
            e.stopPropagation();
            var attId = att.id;
            var attPath = att.path || att.session_path || '';
            inlineConfirm(unlinkBtn, {{
              confirmLabel: 'Unlink?',
              onConfirm: function() {{
                fetch(EDIT_API + '/tickets/' + ticketId + '/attachments/' + attId, {{
                  method: 'DELETE'
                }}).then(function() {{
                  loadAttachments(ticketId);
                  showAppToast('Attachment unlinked', 'undo', 5000, function() {{
                    fetch(EDIT_API + '/tickets/' + ticketId + '/attachments', {{
                      method: 'POST',
                      headers: {{ 'Content-Type': 'application/json' }},
                      body: JSON.stringify({{ session_path: attPath }})
                    }}).then(function() {{ loadAttachments(ticketId); }})
                    .catch(function() {{ showAppToast('Undo failed', 'error'); }});
                  }});
                }})
                .catch(function() {{ showAppToast('Failed to unlink attachment', 'error'); }});
              }}
            }});
          }});
          actions.appendChild(unlinkBtn);

          row.appendChild(actions);

          // Click row to open player
          row.addEventListener('click', function() {{
            if (att.player_url || att.path) window.open(att.player_url || att.path, '_blank');
          }});

          attachmentsList.appendChild(row);
        }});
      }})
      .catch(function() {{
        if (!attachmentsList) return;
        var empty = document.createElement('div');
        empty.className = 'attachments-empty';
        empty.textContent = 'Could not load attachments.';
        attachmentsList.appendChild(empty);
      }});
  }}

  var detailRecordBtn = document.getElementById('detail-record-btn');

  function updateRecordButton(ticketId) {{
    // Check feedbacks status and update all record buttons
    fetch(EDIT_API + '/feedbacks/status')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        var enabled = data.installed && data.enabled;
        if (linkBtn) {{
          linkBtn.style.display = enabled ? 'inline-block' : 'none';
          linkBtn.dataset.ticketId = ticketId;
        }}
        // Detail header record button
        if (detailRecordBtn) {{
          detailRecordBtn.style.display = enabled ? 'inline-flex' : 'none';
          detailRecordBtn.dataset.ticketId = ticketId;
        }}
        // Card record buttons (all cards)
        document.querySelectorAll('.card-record-btn').forEach(function(btn) {{
          btn.style.display = enabled ? 'inline-block' : 'none';
        }});
      }})
      .catch(function() {{
        if (linkBtn) linkBtn.style.display = 'none';
        if (detailRecordBtn) detailRecordBtn.style.display = 'none';
        document.querySelectorAll('.card-record-btn').forEach(function(btn) {{
          btn.style.display = 'none';
        }});
      }});
  }}

  // Shared record handler — used by all record buttons
  function startRecording(tid) {{
    if (!tid) return;
    fetch(EDIT_API + '/tickets/' + tid + '/record', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: '{{}}'
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.url) {{
        var recordUrl = data.url;
        // Append autostart param if setting is enabled
        var _autoChk = document.getElementById('settingsFeedbacksAutostart');
        if (_autoChk && _autoChk.checked) {{
          recordUrl += (recordUrl.indexOf('?') >= 0 ? '&' : '?') + 'autostart=1';
        }}
        var popup = window.open(recordUrl, '_blank', 'width=550,height=420');

        // Add placeholder row to attachments list
        var placeholder = _createRecordingPlaceholder('Recording in progress\u2026');
        if (attachmentsList) {{
          var empty = attachmentsList.querySelector('.attachments-empty');
          if (empty) empty.remove();
          attachmentsList.insertBefore(placeholder, attachmentsList.firstChild);
        }}

        // Poll for popup close, then switch to "processing" state
        var pollId = setInterval(function() {{
          if (popup && !popup.closed) return;
          clearInterval(pollId);
          var label = placeholder.querySelector('.attachment-summary');
          if (label) label.textContent = 'Processing session\u2026';

          // Poll for real attachment to appear
          var attempts = 0;
          var attPollId = setInterval(function() {{
            attempts++;
            if (attempts > 20) {{
              clearInterval(attPollId);
              if (placeholder.parentNode) placeholder.remove();
              return;
            }}
            loadAttachments(tid);
            setTimeout(function() {{
              if (!placeholder.parentNode) clearInterval(attPollId);
            }}, 500);
          }}, 3000);
        }}, 500);
      }} else {{
        showAppToast(data.error || 'Failed to start recording', 'error');
      }}
    }})
    .catch(function() {{ showAppToast('Failed to start recording', 'error'); }});
  }}

  // Wire record buttons to shared handler
  if (detailRecordBtn) {{
    detailRecordBtn.addEventListener('click', function() {{
      startRecording(detailRecordBtn.dataset.ticketId);
    }});
  }}
  // Card action strip record buttons (delegated)
  document.addEventListener('click', function(e) {{
    var btn = e.target.closest('.card-record-btn[data-action="record"]');
    if (btn) {{
      e.stopPropagation();
      startRecording(btn.dataset.ticketId);
    }}
  }});

  // Per-card run-now buttons (delegated) — visible only when data-eligible="true" and no active run
  document.addEventListener('click', function(e) {{
    var btn = e.target.closest('.card-run-now-btn');
    if (!btn) return;
    e.stopPropagation();
    var tid = btn.dataset.ticketId;
    if (!tid) return;
    btn.disabled = true;
    var url = EDIT_API + '/tickets/' + encodeURIComponent(tid) + '/run-now';
    fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}'}})
      .then(function(r) {{
        if (r.status === 409) {{ showAppToast('A run is already active.', 'error'); btn.disabled=false; return null; }}
        if (r.status === 422) {{ return r.json().then(function(d) {{ showAppToast('Not eligible: ' + ((d.reasons||[]).join('; ')||''), 'error'); btn.disabled=false; return null; }}); }}
        if (!r.ok) {{ showAppToast('Run failed to start.', 'error'); btn.disabled=false; return null; }}
        return r.json();
      }})
      .then(function(run) {{
        if (!run) return;
        showAppToast('Run started for ' + _esc(tid), 'success');
        // Update card data attribute so button hides until next page refresh
        var card = btn.closest('.card');
        if (card) {{ card.dataset.runStatus = 'queued'; btn.style.display = 'none'; }}
      }})
      .catch(function() {{ showAppToast('Network error starting run.', 'error'); btn.disabled=false; }});
  }});

  function _createRecordingPlaceholder(text) {{
    var row = document.createElement('div');
    row.className = 'attachment-row attachment-placeholder';
    var dot = document.createElement('span');
    dot.className = 'att-pulse-dot';
    row.appendChild(dot);
    var info = document.createElement('div');
    info.className = 'attachment-info';
    var summary = document.createElement('div');
    summary.className = 'attachment-summary';
    summary.textContent = text;
    info.appendChild(summary);
    var meta = document.createElement('div');
    meta.className = 'attachment-meta';
    meta.textContent = 'Just now';
    info.appendChild(meta);
    row.appendChild(info);
    return row;
  }}

  // Link button click — link latest session
  if (linkBtn) {{
    linkBtn.addEventListener('click', function() {{
      var tid = linkBtn.dataset.ticketId;
      if (!tid) return;
      fetch(EDIT_API + '/feedbacks/link', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ ticket_id: tid }})
      }})
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (data.ok) {{
          loadAttachments(tid);
        }} else {{
          showAppToast(data.error || 'No session to link', 'error');
        }}
      }})
      .catch(function() {{ showAppToast('Failed to link session', 'error'); }});
    }});
  }}

  // Hook into overlay open
  var _origOpenForAttachments = window.openDetailOverlay;
  window.openDetailOverlay = function(tid, section) {{
    if (_origOpenForAttachments) _origOpenForAttachments(tid, section);
    // Load attachments after a short delay to let overlay populate
    setTimeout(function() {{
      loadAttachments(tid);
      updateRecordButton(tid);
    }}, 150);
  }};

  // On page load: show/hide card record buttons
  fetch(EDIT_API + '/feedbacks/status')
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      var show = data.installed && data.enabled;
      document.querySelectorAll('.card-record-btn').forEach(function(btn) {{
        btn.style.display = show ? 'inline-block' : 'none';
      }});
    }})
    .catch(function() {{}});
}})();
</script>

<script>
/* =========================================================
   Task 11.5: Workflow Bounce — ticket detail panel
   ========================================================= */
(function() {{
  var editApiMeta = document.querySelector('meta[name="edit-api"]');
  var EDIT_API = editApiMeta ? editApiMeta.content : null;
  if (!EDIT_API) return;

  var workflowSelect = document.getElementById('workflow-select');
  var workflowRunBtn = document.getElementById('workflow-run-btn');
  var workflowRunsList = document.getElementById('workflow-runs-list');
  var overlay = document.getElementById('ticket-detail-overlay');
  var currentTicketId = null;
  var pollTimers = {{}};

  function loadWorkflowOptions() {{
    fetch(EDIT_API + '/workflow/workflows')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        var workflows = data.workflows || data || [];
        // Clear existing options except the placeholder
        while (workflowSelect.options.length > 1) {{
          workflowSelect.removeChild(workflowSelect.options[1]);
        }}
        workflows.forEach(function(wf) {{
          var opt = document.createElement('option');
          opt.value = wf.id;
          opt.textContent = wf.name || wf.id;
          workflowSelect.appendChild(opt);
        }});
      }})
      .catch(function() {{}});
  }}

  if (workflowSelect) {{
    workflowSelect.addEventListener('change', function() {{
      workflowRunBtn.disabled = !workflowSelect.value;
    }});
  }}

  function loadWorkflowRuns(ticketId) {{
    fetch(EDIT_API + '/tickets/' + encodeURIComponent(ticketId) + '/workflow/runs')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        var runs = data.runs || data || [];
        if (!Array.isArray(runs)) return;
        // Clear stale poll timers
        Object.keys(pollTimers).forEach(function(k) {{
          clearInterval(pollTimers[k]);
          delete pollTimers[k];
        }});
        while (workflowRunsList.firstChild) workflowRunsList.removeChild(workflowRunsList.firstChild);
        runs.forEach(function(run) {{
          var block = renderRunBlock(run);
          workflowRunsList.appendChild(block);
          if (run.status === 'running' || run.status === 'paused') {{
            startPolling(run.id);
          }}
        }});
      }})
      .catch(function() {{}});
  }}

  function renderRunBlock(run) {{
    var block = document.createElement('div');
    block.className = 'workflow-run-block';
    block.dataset.runId = run.id;

    var header = document.createElement('div');
    header.className = 'workflow-run-header';

    var statusBadge = document.createElement('span');
    statusBadge.className = 'wf-run-status ' + (run.status || 'pending');
    statusBadge.textContent = run.status || 'pending';
    header.appendChild(statusBadge);

    var info = document.createElement('span');
    info.textContent = (run.workflow_name || run.workflow_id || '') + ' — step ' + ((run.current_step || 0) + 1);
    header.appendChild(info);

    var spacer = document.createElement('span');
    spacer.style.flex = '1';
    header.appendChild(spacer);

    if (run.status === 'running') {{
      var cancelBtn = document.createElement('button');
      cancelBtn.textContent = 'Cancel';
      cancelBtn.style.cssText = 'font-size:9px;padding:1px 5px;border:1px solid var(--border);border-radius:3px;background:transparent;color:var(--fg);cursor:pointer;';
      cancelBtn.addEventListener('click', function(e) {{
        e.stopPropagation();
        cancelRun(run.id);
      }});
      header.appendChild(cancelBtn);
    }}

    if (run.status === 'paused') {{
      var resumeBtn = document.createElement('button');
      resumeBtn.textContent = 'Resume';
      resumeBtn.style.cssText = 'font-size:9px;padding:1px 5px;border:1px solid var(--accent);border-radius:3px;background:transparent;color:var(--accent);cursor:pointer;';
      resumeBtn.addEventListener('click', function(e) {{
        e.stopPropagation();
        resumeRun(run.id);
      }});
      header.appendChild(resumeBtn);
    }}

    header.addEventListener('click', function() {{
      block.classList.toggle('expanded');
    }});
    block.appendChild(header);

    var conversation = document.createElement('div');
    conversation.className = 'workflow-conversation';
    if (run.conversation && run.conversation.length > 0) {{
      renderConversation(conversation, run.conversation);
    }}
    block.appendChild(conversation);

    return block;
  }}

  function updateRunBlock(block, run) {{
    var statusBadge = block.querySelector('.wf-run-status');
    if (statusBadge) {{
      statusBadge.className = 'wf-run-status ' + (run.status || 'pending');
      statusBadge.textContent = run.status || 'pending';
    }}
    var info = block.querySelector('.workflow-run-header span:nth-child(2)');
    if (info) {{
      info.textContent = (run.workflow_name || run.workflow_id || '') + ' — step ' + ((run.current_step || 0) + 1);
    }}
    var conversation = block.querySelector('.workflow-conversation');
    if (conversation && run.conversation) {{
      renderConversation(conversation, run.conversation);
    }}
  }}

  function renderConversation(container, conversation) {{
    while (container.firstChild) container.removeChild(container.firstChild);
    conversation.forEach(function(turn) {{
      var turnDiv = document.createElement('div');
      turnDiv.className = 'workflow-turn' + (turn.type === 'disagreement' ? ' disagreement' : '');

      var headerDiv = document.createElement('div');
      headerDiv.className = 'workflow-turn-header';

      var agentName = document.createElement('span');
      agentName.className = 'agent-name';
      agentName.textContent = turn.agent_name || turn.agent || turn.agent_id || 'unknown';
      headerDiv.appendChild(agentName);

      var meta = document.createElement('span');
      meta.className = 'turn-meta';
      meta.textContent = turn.timestamp || '';
      headerDiv.appendChild(meta);

      turnDiv.appendChild(headerDiv);

      var content = document.createElement('div');
      content.className = 'workflow-turn-content';
      content.textContent = turn.content || '';
      turnDiv.appendChild(content);

      if (turn.contention_points && turn.contention_points.length > 0) {{
        turn.contention_points.forEach(function(point) {{
          var cp = document.createElement('div');
          cp.style.cssText = 'font-size:10px;color:var(--fg-dim);margin-top:4px;padding-left:8px;border-left:2px solid rgba(234,179,8,0.4);';
          cp.textContent = point;
          turnDiv.appendChild(cp);
        }});
      }}

      container.appendChild(turnDiv);
    }});
  }}

  function setCardWfIndicator(ticketId, active) {{
    var card = document.querySelector('.card[data-item-id="' + ticketId + '"]');
    if (!card) return;
    var existing = card.querySelector('.card-wf-indicator');
    if (active && !existing) {{
      var ind = document.createElement('span');
      ind.className = 'card-wf-indicator';
      ind.textContent = '\u25B6 workflow running';
      var titleEl = card.querySelector('.card-title') || card.querySelector('.item-title');
      if (titleEl) titleEl.parentNode.insertBefore(ind, titleEl.nextSibling);
      else card.appendChild(ind);
    }} else if (!active && existing) {{
      existing.parentNode.removeChild(existing);
    }}
  }}

  function startPolling(runId) {{
    if (pollTimers[runId]) return;
    if (currentTicketId) setCardWfIndicator(currentTicketId, true);
    pollTimers[runId] = setInterval(function() {{
      fetch(EDIT_API + '/workflow/runs/' + encodeURIComponent(runId))
        .then(function(r) {{ return r.json(); }})
        .then(function(run) {{
          var block = workflowRunsList.querySelector('[data-run-id="' + runId + '"]');
          if (block) {{
            updateRunBlock(block, run);
          }}
          if (run.status !== 'running' && run.status !== 'paused') {{
            clearInterval(pollTimers[runId]);
            delete pollTimers[runId];
            if (currentTicketId && Object.keys(pollTimers).length === 0) {{
              setCardWfIndicator(currentTicketId, false);
            }}
          }}
        }})
        .catch(function() {{
          clearInterval(pollTimers[runId]);
          delete pollTimers[runId];
        }});
    }}, 2000);
  }}

  function cancelRun(runId) {{
    fetch(EDIT_API + '/workflow/runs/' + encodeURIComponent(runId) + '/cancel', {{ method: 'POST' }})
      .then(function() {{
        if (currentTicketId) loadWorkflowRuns(currentTicketId);
      }})
      .catch(function() {{ showAppToast('Failed to cancel run', 'error'); }});
  }}

  function resumeRun(runId) {{
    fetch(EDIT_API + '/workflow/runs/' + encodeURIComponent(runId) + '/resume', {{ method: 'POST' }})
      .then(function() {{
        if (currentTicketId) loadWorkflowRuns(currentTicketId);
      }})
      .catch(function() {{ showAppToast('Failed to resume run', 'error'); }});
  }}

  if (workflowRunBtn) {{
    workflowRunBtn.addEventListener('click', function() {{
      if (!currentTicketId || !workflowSelect.value) return;
      var wfName = workflowSelect.options[workflowSelect.selectedIndex].textContent;
      workflowRunBtn.disabled = true;

      // Instant placeholder — show a running block immediately
      var placeholder = renderRunBlock({{
        id: '_pending',
        status: 'running',
        workflow_id: workflowSelect.value,
        workflow_name: wfName,
        current_step: 0,
        total_steps: 0,
        conversation: []
      }});
      placeholder.classList.add('expanded');
      workflowRunsList.insertBefore(placeholder, workflowRunsList.firstChild);

      fetch(EDIT_API + '/tickets/' + encodeURIComponent(currentTicketId) + '/workflow/run', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ workflow_id: workflowSelect.value }})
      }})
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
          workflowRunBtn.disabled = false;
          if (data.run_id) {{
            // Replace placeholder with real run, start polling
            if (placeholder.parentNode) placeholder.parentNode.removeChild(placeholder);
            var realBlock = renderRunBlock({{
              id: data.run_id,
              status: 'running',
              workflow_id: workflowSelect.value,
              workflow_name: wfName,
              current_step: 0,
              total_steps: 0,
              conversation: []
            }});
            realBlock.classList.add('expanded');
            workflowRunsList.insertBefore(realBlock, workflowRunsList.firstChild);
            startPolling(data.run_id);
          }} else {{
            if (placeholder.parentNode) placeholder.parentNode.removeChild(placeholder);
            showAppToast(data.error || 'Failed to start workflow', 'error');
          }}
        }})
        .catch(function() {{
          workflowRunBtn.disabled = false;
          if (placeholder.parentNode) placeholder.parentNode.removeChild(placeholder);
          showAppToast('Failed to start workflow', 'error');
        }});
    }});
  }}

  // Hook into overlay open (chain pattern)
  var _origOpenForWorkflow = window.openDetailOverlay;
  window.openDetailOverlay = function(tid, section) {{
    if (_origOpenForWorkflow) _origOpenForWorkflow(tid, section);
    currentTicketId = tid;
    setTimeout(function() {{
      loadWorkflowOptions();
      loadWorkflowRuns(tid);
    }}, 200);
  }};

  // Clear poll timers when overlay closes
  if (overlay) {{
    var observer = new MutationObserver(function(mutations) {{
      mutations.forEach(function(m) {{
        if (m.attributeName === 'class' || m.attributeName === 'style') {{
          var isHidden = overlay.classList.contains('hidden') ||
            overlay.style.display === 'none' ||
            !overlay.classList.contains('visible');
          if (isHidden) {{
            Object.keys(pollTimers).forEach(function(k) {{
              clearInterval(pollTimers[k]);
              delete pollTimers[k];
            }});
            currentTicketId = null;
          }}
        }}
      }});
    }});
    observer.observe(overlay, {{ attributes: true }});
  }}
}})();
</script>

<script>
/* Project switcher dropdown */
(function () {{
  var projectsMeta = document.querySelector('meta[name="projects-list"]');
  var currentMeta  = document.querySelector('meta[name="current-project"]');
  if (!projectsMeta || !currentMeta) return;

  var projects;
  try {{ projects = JSON.parse(projectsMeta.content); }} catch (e) {{ return; }}
  var currentId = currentMeta.content || '';
  if (!Array.isArray(projects) || projects.length === 0) return;

  var nameSpan = document.querySelector('.project-name');
  if (!nameSpan) return;

  var currentLabel = (projects.find(function (p) {{ return p.id === currentId; }}) || {{}}).name
                     || nameSpan.textContent;

  var wrapper = document.createElement('div');
  wrapper.className = 'proj-switcher';

  var btn = document.createElement('button');
  btn.className = 'proj-switcher-btn';
  btn.setAttribute('aria-haspopup', 'listbox');
  btn.setAttribute('aria-expanded', 'false');
  btn.setAttribute('aria-label', 'Switch project — current: ' + currentLabel);
  btn.setAttribute('data-testid', 'proj-switcher-btn');

  var labelSpan = document.createElement('span');
  labelSpan.textContent = currentLabel;
  labelSpan.setAttribute('data-testid', 'proj-switcher-label');

  var chevron = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  chevron.setAttribute('viewBox', '0 0 10 10');
  chevron.setAttribute('aria-hidden', 'true');
  chevron.setAttribute('class', 'proj-switcher-chevron');
  var poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  poly.setAttribute('points', '1,3 5,7 9,3');
  poly.setAttribute('fill', 'none');
  poly.setAttribute('stroke', 'currentColor');
  poly.setAttribute('stroke-width', '1.5');
  poly.setAttribute('stroke-linecap', 'round');
  poly.setAttribute('stroke-linejoin', 'round');
  chevron.appendChild(poly);

  btn.appendChild(labelSpan);
  btn.appendChild(chevron);

  var menu = document.createElement('div');
  menu.className = 'proj-switcher-menu';
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', 'Projects');
  menu.setAttribute('data-testid', 'proj-switcher-menu');

  projects.forEach(function (p) {{
    var a = document.createElement('a');
    a.href = '/' + p.id;
    a.className = 'proj-switcher-item' + (p.id === currentId ? ' current' : '');
    a.textContent = p.name;
    a.setAttribute('role', 'option');
    a.setAttribute('aria-selected', p.id === currentId ? 'true' : 'false');
    a.setAttribute('data-testid', 'proj-item-' + p.id);
    menu.appendChild(a);
  }});

  var divider = document.createElement('div');
  divider.className = 'proj-switcher-divider';
  divider.setAttribute('role', 'separator');
  menu.appendChild(divider);

  function addFooterItem(href, text, testId) {{
    var a = document.createElement('a');
    a.href = href;
    a.className = 'proj-switcher-footer-item';
    a.textContent = text;
    a.setAttribute('data-testid', testId);
    menu.appendChild(a);
  }}

  addFooterItem('/', 'All Projects', 'proj-switcher-all-projects');
  addFooterItem(currentId ? '/' + currentId + '/settings' : '/settings', 'Settings', 'proj-switcher-settings');

  wrapper.appendChild(btn);
  wrapper.appendChild(menu);
  nameSpan.parentNode.replaceChild(wrapper, nameSpan);

  function isOpen() {{ return menu.classList.contains('open'); }}
  function openMenu() {{
    menu.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
    var first = menu.querySelector('.proj-switcher-item, .proj-switcher-footer-item');
    if (first) first.focus();
  }}
  function closeMenu() {{
    menu.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
    btn.focus();
  }}

  btn.addEventListener('click', function (e) {{
    e.stopPropagation();
    if (isOpen()) {{ closeMenu(); }} else {{ openMenu(); }}
  }});

  var FOCUSABLE_SEL = '.proj-switcher-item, .proj-switcher-footer-item';
  menu.addEventListener('keydown', function (e) {{
    var items = Array.prototype.slice.call(menu.querySelectorAll(FOCUSABLE_SEL));
    var idx = items.indexOf(document.activeElement);
    switch (e.key) {{
      case 'Escape': e.preventDefault(); closeMenu(); break;
      case 'ArrowDown': e.preventDefault(); items[(idx + 1) % items.length].focus(); break;
      case 'ArrowUp': e.preventDefault(); items[(idx - 1 + items.length) % items.length].focus(); break;
      case 'Home': e.preventDefault(); if (items.length) items[0].focus(); break;
      case 'End': e.preventDefault(); if (items.length) items[items.length - 1].focus(); break;
      case 'Tab': closeMenu(); break;
    }}
  }});

  btn.addEventListener('keydown', function (e) {{
    if (e.key === 'ArrowDown' && !isOpen()) {{ e.preventDefault(); openMenu(); }}
    else if (e.key === 'Escape' && isOpen()) {{ e.preventDefault(); closeMenu(); }}
  }});

  document.addEventListener('click', function (e) {{
    if (isOpen() && !wrapper.contains(e.target)) {{ closeMenu(); }}
  }});

  wrapper.addEventListener('focusout', function (e) {{
    if (!wrapper.contains(e.relatedTarget)) {{ closeMenu(); }}
  }});
}})();
</script>
<div id="app-toast" role="status" aria-live="polite"><span id="app-toast-msg"></span></div>
<div id="confirm-modal" style="display:none;position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px);align-items:center;justify-content:center;" role="dialog" aria-modal="true">
  <div style="background:var(--bg-card);border:1px solid var(--border-default);border-radius:12px;padding:24px;max-width:400px;width:90vw;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
    <h3 id="confirm-modal-title" style="font-size:14px;font-weight:600;margin-bottom:8px;"></h3>
    <p id="confirm-modal-msg" style="font-size:13px;color:var(--text-secondary);margin-bottom:20px;"></p>
    <div style="display:flex;gap:8px;justify-content:flex-end;">
      <button id="confirm-modal-cancel" style="font-size:12px;padding:6px 16px;border-radius:6px;border:1px solid var(--border-default);background:none;color:var(--text-secondary);cursor:pointer;font-family:inherit;">Cancel</button>
      <button id="confirm-modal-ok" style="font-size:12px;padding:6px 16px;border-radius:6px;border:none;background:rgba(239,68,68,0.15);color:#ef4444;cursor:pointer;font-weight:600;font-family:inherit;">Delete</button>
    </div>
  </div>
</div>
<script>
(function() {{
  var projMeta = document.querySelector('meta[name="current-project"]');
  var journeysBtn = document.getElementById('journeysBtn');
  if (projMeta && journeysBtn) {{
    window.__goJourneys = function() {{
      window.location.href = '/' + projMeta.content + '/journeys';
    }};
  }} else if (journeysBtn) {{
    journeysBtn.style.display = 'none';
  }}
}})();
</script>
</body>
</html>"""

    return html


def _render_cards(tickets: list[Ticket], slug: str, child_tickets: dict[str, list] = None, dep_state: dict = None) -> str:
    """Render full-size kanban cards."""
    if child_tickets is None:
        child_tickets = {}
    if dep_state is None:
        dep_state = {}
    card_class = CARD_CLASS_BY_SLUG.get(slug, "")
    lines = []
    for t in tickets:
        title_esc = escape(t.title)
        id_esc = escape(t.id)
        desc_esc = escape(t.description) if t.description else ""
        status_class = t.status.replace(" ", "-").lower()

        # Blocked-by-deps class
        dep_info = dep_state.get(t.id, {})
        blocked_class = " blocked" if dep_info.get("blocking_deps") else ""

        # Skip children here — they'll be rendered in the child-group after their parent
        if t.parent:
            continue

        children = child_tickets.get(t.id, [])

        # Children toggle for parent tickets
        child_badge_html = ""
        if children:
            n_children = len(children)
            child_badge_html = (
                f'<span class="children-toggle collapsed" data-parent="{id_esc}">'
                f'<span class="arrow">&#9660;</span> {n_children}</span>'
            )

        lines.append(_render_single_card(t, slug, card_class, dep_state, child_badge_html))

        # Render children as full cards in a connected group
        if children:
            lines.append(f'      <div class="child-group collapsed" data-parent="{id_esc}">')
            for child in children:
                lines.append(_render_single_card(child, slug, card_class, dep_state, ""))
            lines.append(f'      </div>')

    return "\n".join(lines)


def _render_single_card(t, slug: str, card_class: str, dep_state: dict, child_badge_html: str) -> str:
    """Render a single card (parent or child) as full HTML."""
    title_esc = escape(t.title)
    id_esc = escape(t.id)
    desc_esc = escape(t.description) if t.description else ""
    status_class = t.status.replace(" ", "-").lower()

    dep_info = dep_state.get(t.id, {})
    blocked_class = " blocked" if dep_info.get("blocking_deps") else ""

    # Parent link — always render (empty placeholder when no parent, for click-to-add)
    if t.parent:
        parent_link_html = f'        <div class="card-parent-link">\u21b3 {escape(t.parent)}</div>\n'
    else:
        parent_link_html = f'        <div class="card-parent-link empty">+ parent</div>\n'

    # Depends — always render (empty placeholder when no deps)
    if t.depends:
        dep_list = ", ".join(escape(d) for d in t.depends)
        deps_html = f'        <div class="card-deps">&#10547; {dep_list}</div>\n'
        blocking = dep_info.get("blocking_deps", [])
        if blocking:
            deps_html += f'        <span class="card-blocked-badge">blocked by: {escape(", ".join(blocking))}</span>\n'
    else:
        deps_html = f'        <div class="card-deps empty">+ depends</div>\n'

    desc_html = ""
    if t.description:
        desc_html = f'        <div class="card-desc">{desc_esc}</div>\n'
    else:
        desc_html = f'        <div class="card-desc empty">+ description</div>\n'

    criteria_html = ""
    criteria_items = []
    if t.acceptance_criteria:
        for checked, text in t.acceptance_criteria:
            cls = ' class="criterion checked"' if checked else ' class="criterion"'
            marker = "&#9745;" if checked else "&#9744;"
            criteria_items.append(f'          <div{cls}>{marker} {escape(text)}</div>')
    criteria_items.append('          <button class="add-criterion-btn">+ Add Criterion</button>')
    criteria_html = '        <div class="card-criteria">\n' + "\n".join(criteria_items) + "\n        </div>\n"

    # Git traceability (shown on expanded cards)
    git_html = ""
    if t.commit_hash:
        git_html += f'        <div class="card-commit"><span class="commit-badge">{escape(t.commit_hash)}</span></div>\n'
    if t.release_tag:
        git_html += f'        <div class="card-release"><span class="release-badge">{escape(t.release_tag)}</span></div>\n'

    readiness_html = _render_readiness_row(t)
    actions_html = _render_action_buttons(slug, id_esc)

    draft_class = " is-draft" if getattr(t, 'draft', False) else ""
    draft_attr = ' data-draft="true"' if getattr(t, 'draft', False) else ""
    att_count = getattr(t, 'attachment_count', 0)
    att_badge_html = f'<span class="attachment-count-badge" title="{att_count} attachment(s)">{att_count}</span>' if att_count > 0 else ""

    # Kitchen badge (M1a). Latest run status takes precedence over mode-only states.
    # Visible classes: kb-idle (auto, no run yet), kb-held, kb-queued/preparing/running/needs-input/failed/cancelled,
    # plus "hidden" for manual mode with no run.
    kb_class = ""
    kb_title = ""
    if t.latest_run_status in ("queued", "preparing"):
        kb_class, kb_title = "kb-queued", "Queued for run"
    elif t.latest_run_status == "running":
        kb_class, kb_title = "kb-running", "Run in progress"
    elif t.latest_run_status == "needs_input":
        kb_class, kb_title = "kb-needs-input", "Run needs your input"
    elif t.latest_run_status == "failed":
        kb_class, kb_title = "kb-failed", "Last run failed"
    elif t.latest_run_status == "cancelled":
        kb_class, kb_title = "kb-cancelled", "Last run cancelled"
    elif t.latest_run_status == "stalled":
        kb_class, kb_title = "kb-failed", "Last run stalled"
    elif t.automation_mode == "held":
        kb_class, kb_title = "kb-held", "Automation held"
    elif t.automation_mode == "auto":
        kb_class, kb_title = "kb-idle", "Auto — eligible to run"
    kb_html = (
        f'<span class="kitchen-badge {kb_class}" title="{escape(kb_title)}" '
        f'data-automation-mode="{escape(t.automation_mode)}" '
        f'data-run-status="{escape(t.latest_run_status or "")}"></span>'
        if kb_class else ""
    )

    return (
        f'      <div class="card {card_class}{blocked_class}{draft_class}" data-section="{slug}" '
        f'data-title="{title_esc}" data-item-id="{id_esc}" data-desc="{desc_esc}" '
        f'data-status="{status_class}" data-complexity="{escape(t.complexity)}" data-testid="ticket-card-{id_esc}"'
        f'{"" if slug != "bugs" and status_class not in ("bug", "bug-fixed") else " data-is-bug=" + chr(34) + "true" + chr(34)}'
        f'{" data-parent=" + chr(34) + escape(t.parent) + chr(34) if t.parent else ""}'
        f' data-automation-mode="{escape(t.automation_mode)}"'
        f'{" data-eligible=" + chr(34) + "true" + chr(34) if t.automation_eligible else ""}'
        f'{" data-run-status=" + chr(34) + escape(t.latest_run_status) + chr(34) if t.latest_run_status else ""}'
        f'{draft_attr}>\n'
        f'        <div class="card-top"><span class="priority-dot {t.priority}"></span>'
        f'<span class="card-id">{id_esc}</span>'
        f'<span class="card-title">{title_esc}</span>{child_badge_html}{att_badge_html}</div>\n'
        f'        <div class="card-meta">'
        f'<span class="status-badge {status_class}">{status_class}</span>'
        f'{kb_html}'
        f'<button class="card-record-btn" data-action="record" data-ticket-id="{id_esc}" style="display:none" title="Record feedback">{_svg_icon("mic", 12)}</button>'
        f'<button class="card-run-now-btn" data-testid="card-run-now-{id_esc}" data-ticket-id="{id_esc}" title="Run now" aria-label="Run now for {id_esc}">{_svg_icon("play", 12)}</button>'
        f'<button class="card-open-btn" data-testid="card-open-btn-{id_esc}" title="Open ticket details" aria-label="Open {id_esc}">{_svg_icon("arrow-up-right", 14)}</button></div>\n'
        f'{readiness_html}'
        f'{parent_link_html}{deps_html}{desc_html}{criteria_html}'
        f'{git_html}'
        f'{actions_html}'
        f'        <div class="card-footer"><span class="complexity-badge">{escape(t.complexity)}</span></div>\n'
        f'      </div>'
    )


def _render_readiness_row(t) -> str:
    """Render readiness indicator dots for a ticket."""
    flag_map = {"D": "description", "C": "criteria", "S": "smoke", "T": "tests", "L": "reviewed"}
    icon_name_map = {"D": "file-text", "C": "check-square", "S": "flame", "T": "flask-conical", "L": "eye"}
    indicators = [
        ("D", "Description", bool(t.description)),
        ("C", "Criteria", len(t.acceptance_criteria) > 0),
        ("S", "Smoke tested", "smoke" in t.readiness_flags),
        ("T", "Tests", "tests" in t.readiness_flags),
        ("L", "Learnings", "reviewed" in t.readiness_flags),
    ]
    dots = []
    for letter, title, filled in indicators:
        cls = "filled" if filled else "empty"
        flag_name = flag_map[letter]
        icon = _svg_icon(icon_name_map[letter], size=12)
        dots.append(f'<span class="readiness-dot {cls}" title="{title}" data-flag="{flag_name}" aria-label="{title}">{icon}</span>')
    return '        <div class="readiness-row">' + "".join(dots) + '</div>\n'


def _render_action_buttons(slug: str, ticket_id: str) -> str:
    """Render contextual action buttons for a card (only visible in edit mode when expanded)."""
    buttons = []
    if slug == "ideas":
        buttons.append(f'<button class="action-btn primary" data-action="move" data-section="Backlog">{_svg_icon("arrow-right", 12)} Backlog</button>')
    elif slug == "backlog":
        buttons.append(f'<button class="action-btn primary" data-action="move" data-section="WIP">{_svg_icon("play", 12)} Start</button>')
    elif slug == "wip":
        buttons.append(f'<button class="action-btn primary" data-action="move" data-section="For Review">{_svg_icon("check", 12)} Done</button>')
        buttons.append(f'<button class="action-btn" data-action="move" data-section="Icebox">{_svg_icon("snowflake", 12)} Icebox</button>')
    elif slug == "review":
        buttons.append(f'<button class="action-btn primary" data-action="accept">{_svg_icon("check", 12)} Accept</button>')
        buttons.append(f'<button class="action-btn" data-action="move" data-section="WIP">{_svg_icon("arrow-left", 12)} Back to WIP</button>')
    if not buttons:
        return ""
    return '        <div class="card-actions">' + "".join(buttons) + '</div>\n'


def _render_list_rows(tickets: list[Ticket], slug: str, child_tickets: dict[str, list] = None, dep_state: dict = None) -> str:
    """Render compact list rows for bottom sections (bugs, done, icebox, won't do)."""
    if child_tickets is None:
        child_tickets = {}
    if dep_state is None:
        dep_state = {}
    lines = []
    for t in tickets:
        # Skip children — they appear in child-group after their parent
        if t.parent:
            continue

        title_esc = escape(t.title)
        id_esc = escape(t.id)
        desc_esc = escape(t.description) if t.description else ""
        status_class = t.status.replace(" ", "-").lower()

        children = child_tickets.get(t.id, [])
        child_badge_html = ""
        if children:
            child_badge_html = (
                f'<span class="children-toggle collapsed" data-parent="{id_esc}">'
                f'<span class="arrow">&#9660;</span> {len(children)}</span>'
            )

        # Expandable detail panel
        detail_parts = []
        if t.description:
            detail_parts.append(f'          <div class="card-desc" style="display:block">{desc_esc}</div>')
        if t.acceptance_criteria:
            criteria_items = []
            for checked, text in t.acceptance_criteria:
                cls = ' class="criterion checked"' if checked else ' class="criterion"'
                marker = "&#9745;" if checked else "&#9744;"
                criteria_items.append(f'            <div{cls}>{marker} {escape(text)}</div>')
            detail_parts.append('          <div class="card-criteria" style="display:block">\n' + "\n".join(criteria_items) + "\n          </div>")

        detail_html = ""
        if detail_parts:
            detail_html = '        <div class="list-row-detail">\n' + "\n".join(detail_parts) + "\n        </div>\n"

        # Git traceability badges
        commit_badge = ""
        if t.commit_hash:
            commit_badge = f'<span class="commit-badge">{escape(t.commit_hash)}</span>'
        release_badge = ""
        if t.release_tag:
            release_badge = f'<span class="release-badge">{escape(t.release_tag)}</span>'

        readiness_html = _render_readiness_row(t)
        open_btn = f'<button class="card-open-btn" data-testid="card-open-btn-{id_esc}" title="Open ticket details">{_svg_icon("arrow-up-right", 12)}</button>'

        lines.append(
            f'      <div class="list-row card" data-section="{slug}" '
            f'data-title="{title_esc}" data-item-id="{id_esc}" data-desc="{desc_esc}" '
            f'data-status="{status_class}" data-complexity="{escape(t.complexity)}" data-testid="ticket-card-{id_esc}"'
            f'{"" if slug != "bugs" and status_class not in ("bug", "bug-fixed") else " data-is-bug=" + chr(34) + "true" + chr(34)}'
            f'{" data-parent=" + chr(34) + escape(t.parent) + chr(34) if t.parent else ""}>\n'
            f'        <div class="list-row-main">'
            f'<span class="priority-dot {t.priority}"></span>'
            f'<span class="card-id">{id_esc}</span>'
            f'<span class="card-title">{title_esc}</span>'
            f'<span class="status-badge {status_class}">{status_class}</span>'
            f'<span class="complexity-badge">{escape(t.complexity)}</span>'
            f'{commit_badge}{release_badge}'
            f'{child_badge_html}{open_btn}</div>\n'
            f'{readiness_html}'
            f'{detail_html}'
            f'      </div>'
        )

        # Render children as list rows in a connected group
        if children:
            lines.append(f'      <div class="child-group collapsed" data-parent="{id_esc}">')
            for child in children:
                child_title = escape(child.title)
                child_id = escape(child.id)
                child_desc = escape(child.description) if child.description else ""
                child_status = child.status.replace(" ", "-").lower()
                lines.append(
                    f'      <div class="list-row card" data-section="{slug}" '
                    f'data-title="{child_title}" data-item-id="{child_id}" data-desc="{child_desc}" '
                    f'data-status="{child_status}" data-complexity="{escape(child.complexity)}"'
                    f'{"" if slug != "bugs" and child_status not in ("bug", "bug-fixed") else " data-is-bug=" + chr(34) + "true" + chr(34)}'
                    f' data-parent="{id_esc}">\n'
                    f'        <div class="list-row-main">'
                    f'<span class="priority-dot {child.priority}"></span>'
                    f'<span class="card-id">{child_id}</span>'
                    f'<span class="card-title">{child_title}</span>'
                    f'<span class="status-badge {child_status}">{child_status}</span>'
                    f'<span class="complexity-badge">{escape(child.complexity)}</span></div>\n'
                    f'      </div>'
                )
            lines.append(f'      </div>')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_json_output(projects: list[Project]) -> str:
    """Generate structured JSON output of all project/ticket data."""
    all_tickets = []
    for proj in projects:
        all_tickets.extend(proj.tickets)
    dep_state = compute_dependency_state(all_tickets)

    output = {
        "generated_at": datetime.now().isoformat(),
        "projects": [],
    }

    for proj in projects:
        proj_tickets = []
        for t in proj.tickets:
            dep_info = dep_state.get(t.id, {"deps_resolved": True, "blocking_deps": []})
            proj_tickets.append({
                "id": t.id,
                "title": t.title,
                "priority": t.priority,
                "complexity": t.complexity,
                "status": t.status,
                "section": t.section,
                "description": t.description,
                "acceptance_criteria": [
                    {"checked": c, "text": txt} for c, txt in t.acceptance_criteria
                ],
                "parent": t.parent,
                "depends": t.depends,
                "summary": t.summary,
                "archived": t.archived,
                "commit_hash": t.commit_hash,
                "release_tag": t.release_tag,
                "deps_resolved": dep_info["deps_resolved"],
                "blocking_deps": dep_info["blocking_deps"],
            })

        cs = proj.code_stats
        output["projects"].append({
            "id": proj.id,
            "name": proj.name,
            "path": proj.path,
            "description": proj.description,
            "active": proj.active,
            "code_stats": {
                "files": cs.files,
                "loc": cs.loc,
                "deps": cs.deps,
                "last_commit": cs.last_commit,
                "releases": cs.releases,
                "version": cs.version,
            },
            "tickets": proj_tickets,
        })

    return json.dumps(output, indent=2)


def main():
    # Ensure output directory exists
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

    # Output mode
    json_mode = "--json" in sys.argv

    # Determine which project to generate for
    # --project <id> flag, or auto-detect from cwd, or all
    filter_project = None
    if "--project" in sys.argv:
        idx = sys.argv.index("--project")
        if idx + 1 < len(sys.argv):
            filter_project = sys.argv[idx + 1]
    elif "--all" not in sys.argv:
        # Auto-detect: match cwd against registered project paths
        cwd = os.path.realpath(os.getcwd())
        # Will be matched below after loading registry

    # Load registry
    if not REGISTRY_PATH.exists():
        print(f"Registry not found at {REGISTRY_PATH}, creating empty dashboard.", file=sys.stderr)
        projects_data = {"projects": []}
    else:
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                projects_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error reading registry: {e}", file=sys.stderr)
            projects_data = {"projects": []}

    # Auto-detect project from cwd if no explicit flag
    if filter_project is None and "--all" not in sys.argv:
        cwd = os.path.realpath(os.getcwd())
        for entry in projects_data.get("projects", []):
            proj_path = os.path.realpath(os.path.expanduser(entry.get("path", "")))
            if cwd == proj_path or cwd.startswith(proj_path + os.sep):
                filter_project = entry.get("id")
                break

    projects: list[Project] = []

    for entry in projects_data.get("projects", []):
        if not entry.get("active", True):
            continue
        if filter_project and entry.get("id") != filter_project:
            continue

        proj = Project(
            id=entry.get("id", "unknown"),
            name=entry.get("name", entry.get("id", "Unknown")),
            path=os.path.expanduser(entry.get("path", "")),
            description=entry.get("description", ""),
            active=entry.get("active", True),
        )

        # Load tickets: try SQLite first, fall back to markdown
        db_path = Path.home() / ".claude" / "ticket-takeaway" / "tickets.db"
        if not db_path.exists():
            db_path = DASHBOARD_DIR / "tickets.db"
        if db_path.exists():
            proj.tickets = load_tickets_from_db(str(db_path), proj.id)
        else:
            backlog_path = os.path.join(proj.path, "PRODUCT_BACKLOG.md")
            proj.tickets = parse_backlog(backlog_path)

        # DB is the single source of truth for tickets.
        # PRODUCT_SPECIFICATION.md is read-only output (written by /accept).
        # Spec items must be seeded into the DB via tickets-cli.py.

        # Enrich tickets with release tags from git
        if proj.path:
            for t in proj.tickets:
                if t.commit_hash and not t.release_tag:
                    tag = run_cmd(f"git tag --contains {t.commit_hash} --sort=-creatordate | head -1", cwd=proj.path)
                    if tag:
                        t.release_tag = tag

        # Collect code stats
        proj.code_stats = collect_code_stats(proj.path)

        projects.append(proj)

    if not projects:
        # Create a placeholder project so dashboard still renders
        projects = [Project(id="none", name="No Projects", path="")]

    # JSON output mode: print and exit
    if json_mode:
        print(generate_json_output(projects))
        return

    output_paths = []
    for proj in projects:
        html = generate_html(proj)
        if proj.path:
            docs_dir = Path(proj.path) / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            out_path = docs_dir / "sdlc-dashboard.html"
            out_path.write_text(html, encoding="utf-8")
            output_paths.append(out_path)

    # Print summary
    all_tickets = []
    for proj in projects:
        all_tickets.extend(proj.tickets)

    counts = {}
    for t in all_tickets:
        counts[t.section] = counts.get(t.section, 0) + 1

    backlog_n = counts.get("Backlog", 0)
    wip_n = counts.get("WIP", 0)
    review_n = counts.get("For Review", 0)
    done_n = counts.get("Done", 0)
    ideas_n = counts.get("Ideas", 0)
    icebox_n = counts.get("Icebox", 0)
    bugs_n = counts.get("Bugs", 0)

    print(f"Dashboard updated: {backlog_n} backlog, {wip_n} WIP, {review_n} review, {done_n} done, {ideas_n} ideas, {icebox_n} icebox, {bugs_n} bugs")
    for p in output_paths:
        print(f"Output: {p}")

    # Open first project's dashboard in browser (skip if --no-open)
    if output_paths and "--no-open" not in sys.argv:
        open_path = output_paths[0]
        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.Popen(["open", str(open_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif system == "Linux":
                subprocess.Popen(["xdg-open", str(open_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif system == "Windows":
                os.startfile(str(open_path))
        except Exception:
            pass


if __name__ == "__main__":
    main()
