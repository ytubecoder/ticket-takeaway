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
                       DEFAULT_STATUS_BY_SECTION, CARD_CLASS_BY_SLUG, STATUSES)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DASHBOARD_DIR = Path.home() / ".claude" / "ticket-takeaway"
REGISTRY_PATH = DASHBOARD_DIR / "registry.json"
# OUTPUT_PATH is now per-project: {project.path}/docs/sdlc-dashboard.html


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

def generate_html(projects: list[Project]) -> str:
    """Generate the full self-contained HTML dashboard."""

    # For now, use the first active project as primary display
    primary = projects[0] if projects else None

    # Aggregate all tickets across projects
    all_tickets: list[Ticket] = []
    for proj in projects:
        all_tickets.extend(proj.tickets)

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

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="gen-ts" content="{gen_ts}">
<meta name="schema-version" content="2">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0%25' stop-color='%233b82f6'/%3E%3Cstop offset='100%25' stop-color='%238b5cf6'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect x='3' y='2' width='26' height='28' rx='4' fill='url(%23g)'/%3E%3Ccircle cx='3' cy='12' r='3.5' fill='%230a0a0b'/%3E%3Ccircle cx='29' cy='12' r='3.5' fill='%230a0a0b'/%3E%3Cline x1='6.5' y1='12' x2='25.5' y2='12' stroke='%230a0a0b' stroke-width='1' stroke-dasharray='2.5 2'/%3E%3Crect x='8' y='5' width='11' height='2.5' rx='1.2' fill='%23ffffffcc'/%3E%3Crect x='8' y='16' width='16' height='1.5' rx='.7' fill='%23ffffff55'/%3E%3Crect x='8' y='19.5' width='12' height='1.5' rx='.7' fill='%23ffffff33'/%3E%3Crect x='8' y='23' width='14' height='1.5' rx='.7' fill='%23ffffff22'/%3E%3C/svg%3E">
<title>Ticket Takeaway — {escape(project_short)}</title>
<style>
:root {{
  --bg-page: #0a0a0b; --bg-surface: #141417; --bg-card: #1a1a1f; --bg-hover: #222228;
  --border-subtle: #1e1e24; --border-default: #2a2a32; --border-strong: #3a3a44;
  --text-primary: #ededef; --text-secondary: #a0a0ab; --text-tertiary: #6b6b76;
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
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
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
.column-body::-webkit-scrollbar {{ width: 4px; }}
.column-body::-webkit-scrollbar-thumb {{ background: var(--border-default); border-radius: 2px; }}

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
.card-id {{ font-size: 10px; color: var(--text-tertiary); font-family: var(--font-mono); font-weight: 500; }}

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
  font-size: 12px; padding: 0 2px; line-height: 1; opacity: 0.4;
  transition: opacity 0.15s;
}}
.card-open-btn:hover {{ opacity: 1; color: var(--accent); }}

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
  display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  cursor: pointer; user-select: none;
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
  background: transparent; border: none; border-radius: 4px; padding: 4px 8px;
  border-left: none !important;
}}
.list-row:hover {{ background: var(--bg-hover); }}
.list-row-main {{
  display: flex; align-items: center; gap: 8px; min-height: 24px;
}}
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
/* Undo toast */
#undo-toast {{
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(20px);
  background: var(--bg-card); border: 1px solid var(--border-default);
  border-radius: 8px; padding: 8px 16px; z-index: 9999;
  box-shadow: 0 4px 20px rgba(0,0,0,.5); font-size: 12px; color: var(--text-secondary);
  opacity: 0; transition: opacity 0.3s, transform 0.3s;
  pointer-events: none; max-width: 500px; white-space: nowrap;
}}
#undo-toast.visible {{ opacity: 1; transform: translateX(-50%) translateY(0); }}
#undo-toast.undo-fail {{ color: #ef4444; }}

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
.new-ticket-expand-btn {{
  display: inline-flex; align-items: center; gap: 4px; margin-top: 8px;
  font-size: 10px; padding: 2px 0; border: none; background: none;
  color: var(--text-tertiary); cursor: pointer; font-family: var(--font-sans);
  transition: color 0.15s;
}}
.new-ticket-expand-btn:hover {{ color: var(--text-secondary); }}
.new-ticket-expand-btn .arrow {{ display: inline-block; transition: transform 0.15s; font-size: 8px; }}
.new-ticket-expand-btn.expanded .arrow {{ transform: rotate(90deg); }}
.new-ticket-full {{
  margin-top: 10px; padding: 20px; border-radius: 8px;
  background: var(--bg-card); border: 1px solid var(--border-default);
}}
.coming-soon {{
  color: var(--text-tertiary); font-size: 13px; text-align: center;
  padding: 24px 0; font-style: italic;
}}

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
/* Toast */
.detail-toast {{ position: absolute; top: 14px; right: 60px; font-size: 11px; font-weight: 600; color: var(--status-done); background: var(--status-done-bg); padding: 3px 10px; border-radius: 4px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 1020; }}
.detail-toast.show {{ opacity: 1; }}
@media (max-width: 560px) {{
  .detail-panel {{ max-width: 100vw; max-height: 100vh; border-radius: 0; inset: 0; }}
  .detail-meta-strip {{ gap: 6px; }}
  .detail-dctrs-strip {{ order: 10; width: 100%; justify-content: center; padding-top: 4px; }}
}}

.status-dropdown-opt:hover {{ background: var(--bg-hover); }}
.list-row-detail {{ display: none; padding: 6px 8px 4px 22px; }}
.list-row.expanded .list-row-detail {{ display: block; }}

/* Copied toast */
.copied-toast {{
  position: absolute; top: -6px; right: 8px; font-size: 9px; font-weight: 700;
  color: var(--status-done); background: var(--status-done-bg); padding: 1px 6px;
  border-radius: 4px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 10;
}}
.copied-toast.show {{ opacity: 1; }}

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
  cursor: pointer; padding: 4px 8px; border-radius: 6px; line-height: 1;
  transition: color 0.15s, background 0.15s;
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
.settings-install-btn {{
  font-size: 11px; padding: 5px 14px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--accent); background: rgba(59,130,246,0.1);
  color: var(--accent); font-weight: 600; font-family: var(--font-sans); transition: all 0.15s;
}}
.settings-install-btn:hover {{ background: rgba(59,130,246,0.2); }}
.settings-install-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.settings-link {{
  font-size: 11px; color: var(--accent); text-decoration: none;
}}
.settings-link:hover {{ text-decoration: underline; }}

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
  <button class="filter-btn" id="draftsToggleBtn" data-filter="draft" data-group="draft">Drafts</button>
  <input type="text" class="search-input" id="searchInput" placeholder="Search items...">
  <button class="settings-toggle" id="settingsToggleBtn" title="Settings">&#9881;</button>
  <button class="new-ticket-btn" id="newTicketBtn">+ New</button>
  <div class="new-ticket-panel" id="newTicketPanel" style="display:none">
    <div class="new-ticket-quick">
      <input type="text" id="newTicketTitle" placeholder="What needs to be done?" class="new-ticket-input" />
      <select id="newTicketSection" class="new-ticket-select">
        <option value="ideas">Idea</option>
        <option value="backlog">Backlog</option>
        <option value="wip">WIP</option>
        <option value="bugs">Bug</option>
      </select>
      <button id="newTicketSubmit" class="new-ticket-submit">Create</button>
    </div>
    <button class="new-ticket-expand-btn" id="newTicketExpandBtn"><span class="arrow">&#9654;</span> Full ticket form</button>
    <div class="new-ticket-full" id="newTicketFull" style="display:none">
      <div class="coming-soon">Coming soon</div>
    </div>
  </div>
</div>

<!-- Settings drawer -->
<div id="settings-drawer" class="settings-drawer hidden">
  <div class="settings-drawer-header">
    <h2>Settings</h2>
    <button class="settings-drawer-close" id="settingsDrawerClose">&times;</button>
  </div>
  <div class="settings-drawer-body">
    <div class="settings-section">
      <div class="settings-section-title">Feedbacks Integration</div>
      <div class="settings-row">
        <label>Enable</label>
        <label class="settings-toggle-switch">
          <input type="checkbox" id="settingsFeedbacksEnabled">
          <span class="settings-toggle-slider"></span>
        </label>
        <span class="settings-status-dot" id="feedbacksStatusDot" title="Feedbacks status"></span>
      </div>
      <div class="settings-row">
        <label>Path</label>
        <input type="text" id="settingsFeedbacksPath" placeholder="~/projects/feedbacks">
      </div>
      <div class="settings-row">
        <a class="settings-link" href="https://github.com/ytubecoder/feedbacks" target="_blank" rel="noopener">GitHub</a>
        <button class="settings-install-btn" id="settingsFeedbacksInstall">Install</button>
      </div>
    </div>
  </div>
</div>

<div class="kanban" id="kanban">

  <!-- Ideas -->
  <div class="column" data-col="ideas" id="col-ideas">
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
  <div class="column" data-col="backlog" id="col-backlog">
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
  <div class="column" data-col="wip" id="col-wip">
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
  <div class="column" data-col="review" id="col-review">
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
          var toast = document.createElement('span');
          toast.className = 'copied-toast show';
          toast.textContent = 'Copied!';
          header.appendChild(toast);
          setTimeout(function() {{ toast.remove(); }}, 1200);
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
        var toast = el.querySelector('.copied-toast');
        if (toast) {{
          toast.classList.add('show');
          setTimeout(function() {{ toast.classList.remove('show'); }}, 1200);
        }}
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
            var toast = el.querySelector('.copied-toast');
            if (toast) {{ toast.classList.add('show'); setTimeout(function() {{ toast.classList.remove('show'); }}, 1200); }}
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
        showToast(card || document.body, 'Gate check failed');
      }});
    }}

    function showToast(el, text) {{
      var toast = el.querySelector('.copied-toast');
      if (toast) {{
        var orig = toast.textContent;
        toast.textContent = text || 'Saved!';
        toast.classList.add('show');
        setTimeout(function() {{ toast.classList.remove('show'); toast.textContent = orig; }}, 1200);
      }}
    }}

    // --- Undo/Redo system (Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y) ---
    var undoStack = [];
    var redoStack = [];
    var MAX_UNDO = 50;
    if (EDIT_API) {{
      var undoEl = document.createElement('div');
      undoEl.id = 'undo-toast';
      undoEl.textContent = '';
      document.body.appendChild(undoEl);
    }}

    function showUndoToast(text) {{
      var el = document.getElementById('undo-toast');
      if (!el) return;
      el.classList.remove('undo-fail');
      el.textContent = text;
      el.classList.add('visible');
      setTimeout(function() {{ el.classList.remove('visible'); }}, 2500);
    }}

    function pushUndo(ticketId, description, revertFn, redoFn) {{
      if (!EDIT_API) return;
      undoStack.push({{ ticketId: ticketId, description: description, revertFn: revertFn, redoFn: redoFn }});
      if (undoStack.length > MAX_UNDO) undoStack.shift();
      redoStack = []; // new edit clears redo history
      showUndoToast(description + '  (Ctrl+Z to undo)');
    }}

    function performUndo() {{
      if (!undoStack.length) return;
      var state = undoStack.pop();
      state.revertFn().then(function() {{
        redoStack.push(state);
        showUndoToast('Undone: ' + state.description);
      }}).catch(function() {{
        var el = document.getElementById('undo-toast');
        if (el) {{
          el.classList.add('undo-fail');
          el.textContent = 'Undo failed';
          el.classList.add('visible');
          setTimeout(function() {{ el.classList.remove('visible', 'undo-fail'); }}, 2000);
        }}
      }});
    }}

    function performRedo() {{
      if (!redoStack.length) return;
      var state = redoStack.pop();
      if (state.redoFn) {{
        state.redoFn().then(function() {{
          undoStack.push(state);
          showUndoToast('Redone: ' + state.description);
        }}).catch(function() {{
          var el = document.getElementById('undo-toast');
          if (el) {{
            el.classList.add('undo-fail');
            el.textContent = 'Redo failed';
            el.classList.add('visible');
            setTimeout(function() {{ el.classList.remove('visible', 'undo-fail'); }}, 2000);
          }}
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
      var expandBtn = document.getElementById('newTicketExpandBtn');
      var fullPanel = document.getElementById('newTicketFull');

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

      if (expandBtn) {{
        expandBtn.addEventListener('click', function() {{
          var open = fullPanel.style.display !== 'none';
          fullPanel.style.display = open ? 'none' : 'block';
          expandBtn.classList.toggle('expanded', !open);
        }});
      }}
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
    window.startGateCheck = startGateCheck;
  }})();
}})();
</script>

<!-- Ticket detail screen -->
<div id="ticket-detail-overlay" class="detail-overlay hidden" role="dialog" aria-modal="true">
  <div class="detail-backdrop"></div>
  <div class="detail-panel">
    <div class="detail-header">
      <span class="detail-id"></span>
      <span class="detail-title" contenteditable="false" title="Click to rename"></span>
      <span class="detail-path"></span>
      <div class="detail-dctrs-strip">
        <button class="readiness-dot" data-flag="description" title="Description">D</button>
        <button class="readiness-dot" data-flag="criteria" title="Criteria">C</button>
        <button class="readiness-dot" data-flag="smoke" title="Smoke">S</button>
        <button class="readiness-dot" data-flag="tests" title="Tests">T</button>
        <button class="readiness-dot" data-flag="reviewed" title="Learnings">L</button>
      </div>
      <span class="detail-toast" role="status" aria-live="polite"></span>
      <button class="detail-close" aria-label="Close ticket detail">&times;</button>
    </div>
    <div class="detail-meta-strip">
      <span class="meta-chip meta-chip--priority" title="Click to change priority"><span class="chip-dot"></span><span class="chip-text"></span></span>
      <span class="meta-chip meta-chip--status" title="Click to change status"><span class="chip-text"></span></span>
      <span class="meta-chip meta-chip--complexity" title="Click to change complexity"><span class="chip-text"></span></span>
      <span class="meta-chip meta-chip--parent"><span class="chip-label">Parent:</span> <span class="chip-value">None</span></span>
      <span class="meta-chip meta-chip--section"><span class="chip-text"></span></span>
    </div>
    <div class="detail-body">
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
        <textarea class="detail-editor desc-editor" data-field="description" placeholder="No description yet. Click to write one."></textarea>
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
            <button class="record-feedback-btn" id="record-feedback-btn" style="display:none">Record</button>
            <button class="link-session-btn" id="link-session-btn" style="display:none">+ Link</button>
          </div>
        </div>
        <div id="attachments-list" class="attachments-list"></div>
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
  var toastEl = overlay.querySelector('.detail-toast');
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

  function toast(msg) {{ toastEl.textContent = msg; toastEl.classList.add('show'); setTimeout(function() {{ toastEl.classList.remove('show'); }}, 1500); }}

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
    }});
  }}

  function closeOverlay() {{
    closeStatusDropdown();
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
  var showDrafts = false;

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
    draftsBtn.classList.toggle('active', showDrafts);
    applyDraftVisibility();
  }});
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
    msg.textContent = 'This ticket was auto-generated from a feedback session.';

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
      }}).catch(function() {{ alert('Failed to confirm ticket'); }});
    }});

    var rejectBtn = document.createElement('button');
    rejectBtn.style.cssText = 'font-size:11px;padding:4px 10px;border-radius:5px;border:1px solid rgba(239,68,68,0.5);background:none;color:#ef4444;cursor:pointer;font-weight:600;font-family:var(--font-sans);';
    rejectBtn.textContent = 'Reject';
    rejectBtn.addEventListener('click', function() {{
      if (!confirm('Delete this draft ticket?')) return;
      fetch(EDIT_API + '/tickets/' + ticketId, {{
        method: 'DELETE'
      }}).then(function() {{
        // Close overlay and remove card
        if (window.closeDetailOverlay) window.closeDetailOverlay();
        var card = document.querySelector('[data-item-id="' + ticketId + '"]');
        if (card) card.remove();
      }}).catch(function() {{ alert('Failed to reject ticket'); }});
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
  if (!EDIT_API) return;

  var toggleBtn = document.getElementById('settingsToggleBtn');
  var drawer = document.getElementById('settings-drawer');
  var closeBtn = document.getElementById('settingsDrawerClose');
  if (!toggleBtn || !drawer) return;

  var enabledChk = document.getElementById('settingsFeedbacksEnabled');
  var pathInput = document.getElementById('settingsFeedbacksPath');
  var statusDot = document.getElementById('feedbacksStatusDot');
  var installBtn = document.getElementById('settingsFeedbacksInstall');

  function openDrawer() {{
    drawer.classList.remove('hidden');
    loadSettings();
    checkFeedbacksStatus();
  }}

  function closeDrawer() {{
    drawer.classList.add('hidden');
  }}

  toggleBtn.addEventListener('click', function() {{
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
    fetch(EDIT_API + '/settings')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (enabledChk) enabledChk.checked = !!(data.feedbacks_enabled);
        if (pathInput) pathInput.value = data.feedbacks_path || '';
      }})
      .catch(function() {{ /* settings endpoint may not exist yet */ }});
  }}

  function saveSettings(patch) {{
    fetch(EDIT_API + '/settings', {{
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
        if (data.installed && data.enabled) {{
          statusDot.classList.add('ok');
          statusDot.title = 'Feedbacks installed and enabled';
        }} else if (data.installed) {{
          statusDot.classList.add('warn');
          statusDot.title = 'Feedbacks installed but disabled';
        }} else {{
          statusDot.classList.add('err');
          statusDot.title = 'Feedbacks not installed';
        }}
      }})
      .catch(function() {{
        statusDot.className = 'settings-status-dot err';
        statusDot.title = 'Could not check feedbacks status';
      }});
  }}

  if (enabledChk) {{
    enabledChk.addEventListener('change', function() {{
      saveSettings({{ feedbacks_enabled: enabledChk.checked }});
    }});
  }}

  if (pathInput) {{
    pathInput.addEventListener('blur', function() {{
      saveSettings({{ feedbacks_path: pathInput.value }});
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
   Task 11: Attachments + Record button
   ========================================================= */
(function() {{
  var editApiMeta = document.querySelector('meta[name="edit-api"]');
  var EDIT_API = editApiMeta ? editApiMeta.content : null;
  if (!EDIT_API) return;

  var attachmentsList = document.getElementById('attachments-list');
  var recordBtn = document.getElementById('record-feedback-btn');
  var linkBtn = document.getElementById('link-session-btn');

  function loadAttachments(ticketId) {{
    if (!attachmentsList) return;
    // Clear
    while (attachmentsList.firstChild) attachmentsList.removeChild(attachmentsList.firstChild);

    fetch(EDIT_API + '/tickets/' + ticketId + '/attachments')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        var items = data.attachments || data || [];
        if (!Array.isArray(items) || items.length === 0) {{
          var empty = document.createElement('div');
          empty.className = 'attachments-empty';
          empty.textContent = 'No attachments yet.';
          attachmentsList.appendChild(empty);
          return;
        }}
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
            if (!confirm('Unlink this attachment?')) return;
            fetch(EDIT_API + '/tickets/' + ticketId + '/attachments/' + att.id, {{
              method: 'DELETE'
            }}).then(function() {{ loadAttachments(ticketId); }})
              .catch(function() {{ alert('Failed to unlink attachment'); }});
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

  function updateRecordButton(ticketId) {{
    if (!recordBtn || !linkBtn) return;
    // Check feedbacks status
    fetch(EDIT_API + '/feedbacks/status')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (data.installed && data.enabled) {{
          recordBtn.style.display = 'inline-block';
          recordBtn.classList.add('active');
          recordBtn.title = 'Record a feedback session for this ticket';
          linkBtn.style.display = 'inline-block';
        }} else if (data.installed) {{
          recordBtn.style.display = 'inline-block';
          recordBtn.classList.remove('active');
          recordBtn.style.opacity = '0.5';
          recordBtn.title = 'Feedbacks disabled — enable in Settings';
          linkBtn.style.display = 'none';
        }} else {{
          recordBtn.style.display = 'none';
          linkBtn.style.display = 'none';
        }}
        // Store ticketId on buttons for click handler
        recordBtn.dataset.ticketId = ticketId;
        if (linkBtn) linkBtn.dataset.ticketId = ticketId;
      }})
      .catch(function() {{
        recordBtn.style.display = 'none';
        linkBtn.style.display = 'none';
      }});
  }}

  // Record button click — start a feedbacks session
  if (recordBtn) {{
    recordBtn.addEventListener('click', function() {{
      var tid = recordBtn.dataset.ticketId;
      if (!tid) return;
      fetch(EDIT_API + '/feedbacks/record', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ ticket_id: tid }})
      }})
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (data.ok) {{
          recordBtn.textContent = 'Recording...';
          setTimeout(function() {{ recordBtn.textContent = 'Record'; }}, 3000);
        }} else {{
          alert(data.error || 'Failed to start recording');
        }}
      }})
      .catch(function() {{ alert('Failed to start recording'); }});
    }});
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
          alert(data.error || 'No session to link');
        }}
      }})
      .catch(function() {{ alert('Failed to link session'); }});
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

    return (
        f'      <div class="card {card_class}{blocked_class}{draft_class}" data-section="{slug}" '
        f'data-title="{title_esc}" data-item-id="{id_esc}" data-desc="{desc_esc}" '
        f'data-status="{status_class}" data-complexity="{escape(t.complexity)}"'
        f'{"" if slug != "bugs" and status_class not in ("bug", "bug-fixed") else " data-is-bug=" + chr(34) + "true" + chr(34)}'
        f'{" data-parent=" + chr(34) + escape(t.parent) + chr(34) if t.parent else ""}'
        f'{draft_attr}>\n'
        f'        <div class="copied-toast">Copied!</div>\n'
        f'        <div class="card-top"><span class="priority-dot {t.priority}"></span>'
        f'<span class="card-title">{title_esc}</span>{child_badge_html}{att_badge_html}</div>\n'
        f'        <div class="card-meta"><span class="card-id">{id_esc}</span>'
        f'<span class="status-badge {status_class}">{status_class}</span>'
        f'<button class="card-open-btn" title="Open ticket details">&#8599;</button></div>\n'
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
    icon_map = {"D": "&#128196;", "C": "&#9745;", "S": "&#128168;", "T": "&#128300;", "L": "&#128065;"}
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
        icon = icon_map[letter]
        dots.append(f'<span class="readiness-dot {cls}" title="{title}" data-flag="{flag_name}">{icon}</span>')
    return '        <div class="readiness-row">' + "".join(dots) + '</div>\n'


def _render_action_buttons(slug: str, ticket_id: str) -> str:
    """Render contextual action buttons for a card (only visible in edit mode when expanded)."""
    buttons = []
    if slug == "ideas":
        buttons.append(f'<button class="action-btn primary" data-action="move" data-section="Backlog">&#8594; Backlog</button>')
    elif slug == "backlog":
        buttons.append(f'<button class="action-btn primary" data-action="move" data-section="WIP">&#9654; Start</button>')
    elif slug == "wip":
        buttons.append(f'<button class="action-btn primary" data-action="move" data-section="For Review">&#10003; Done</button>')
        buttons.append(f'<button class="action-btn" data-action="move" data-section="Icebox">&#10052; Icebox</button>')
    elif slug == "review":
        buttons.append(f'<button class="action-btn primary" data-action="accept">&#10003; Accept</button>')
        buttons.append(f'<button class="action-btn" data-action="move" data-section="WIP">&#8592; Back to WIP</button>')
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

        lines.append(
            f'      <div class="list-row card" data-section="{slug}" '
            f'data-title="{title_esc}" data-item-id="{id_esc}" data-desc="{desc_esc}" '
            f'data-status="{status_class}" data-complexity="{escape(t.complexity)}"'
            f'{"" if slug != "bugs" and status_class not in ("bug", "bug-fixed") else " data-is-bug=" + chr(34) + "true" + chr(34)}>\n'
            f'        <div class="copied-toast">Copied!</div>\n'
            f'        <div class="list-row-main">'
            f'<span class="priority-dot {t.priority}"></span>'
            f'<span class="card-id">{id_esc}</span>'
            f'<span class="card-title">{title_esc}</span>'
            f'<span class="status-badge {status_class}">{status_class}</span>'
            f'<span class="complexity-badge">{escape(t.complexity)}</span>'
            f'{commit_badge}{release_badge}'
            f'{child_badge_html}</div>\n'
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
                    f'        <div class="copied-toast">Copied!</div>\n'
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

    # Generate HTML and write to each project's docs/ folder
    html = generate_html(projects)

    output_paths = []
    for proj in projects:
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
