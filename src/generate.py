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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DASHBOARD_DIR = Path.home() / ".claude" / "ticket-takeaway"
REGISTRY_PATH = DASHBOARD_DIR / "registry.json"
# OUTPUT_PATH is now per-project: {project.path}/docs/sdlc-dashboard.html

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

CARD_CLASS_BY_COLUMN = {
    "backlog": "backlog-card",
    "wip": "wip-card",
    "review": "review-card",
    "ideas": "idea-card",
    "done": "done-card",
    "wontdo": "wontdo-card",
    "icebox": "icebox-card",
    "bugs": "bug-card",
}


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
    column: str = "ideas"
    description: str = ""
    acceptance_criteria: list = field(default_factory=list)
    parent: Optional[str] = None
    rationale: str = ""
    depends: list = field(default_factory=list)
    summary: str = ""
    archived: bool = False
    commit_hash: str = ""
    release_tag: str = ""
    readiness_flags: set = field(default_factory=set)  # explicit flags from DB
    readiness_content: dict = field(default_factory=dict)  # {flag: content_text}


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
            column = SECTION_TO_COLUMN.get(current_section, "backlog")
            default_status = DEFAULT_STATUS_BY_SECTION.get(current_section, "proposed")

            current_ticket = Ticket(
                id=ticket_id,
                title=title,
                section=current_section,
                column=column,
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

        # Detect Rationale: field
        if current_ticket and line_stripped.startswith("Rationale:"):
            rationale_value = line_stripped.split(":", 1)[1].strip()
            if rationale_value:
                current_ticket.rationale = rationale_value
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

        tickets.append(Ticket(
            id=r["id"],
            title=r["title"],
            priority=r["priority"],
            complexity=r["complexity"],
            status=r["status"],
            section=r["section"],
            column=r["column"],
            description=r["description"],
            acceptance_criteria=criteria,
            parent=r["parent"],
            rationale=r["rationale"],
            depends=depends,
            summary=r["summary"],
            archived=bool(r["archived"]),
            commit_hash=commit_hash,
            release_tag=release_tag,
            readiness_flags=flags,
            readiness_content=readiness_content,
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
                    column="done",
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

    # Categorize tickets by column
    by_column: dict[str, list[Ticket]] = {
        "backlog": [],
        "wip": [],
        "review": [],
        "ideas": [],
        "done": [],
        "wontdo": [],
        "icebox": [],
        "bugs": [],
    }
    for t in all_tickets:
        col = t.column
        if col in by_column:
            by_column[col].append(t)

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

    # Auto-promote parents to review when all child tickets are resolved
    review_statuses = {"for-review", "bug-fixed", "done"}
    promoted_ids: set[str] = set()
    parented_ids = {t.id for t in all_tickets if t.parent}
    for parent_id, children in child_tickets.items():
        if all(c.status in review_statuses for c in children):
            for col in ("wip", "backlog", "bugs"):
                for t in by_column[col]:
                    if t.id == parent_id:
                        by_column[col].remove(t)
                        by_column["review"].append(t)
                        promoted_ids.add(t.id)
                        break

    # Reorder columns: place children directly after their parent
    # Children NOT in the same column as their parent get a standalone entry
    for col in by_column:
        ordered = []
        seen = set()
        for t in by_column[col]:
            if t.id in seen:
                continue
            seen.add(t.id)
            ordered.append(t)
            # Insert children right after parent
            for child in child_tickets.get(t.id, []):
                if child.id not in seen:
                    seen.add(child.id)
                    ordered.append(child)
        by_column[col] = ordered

    # Count totals (exclude children from headline counts)
    count_backlog = sum(1 for t in by_column["backlog"] if t.id not in parented_ids)
    count_wip = sum(1 for t in by_column["wip"] if t.id not in parented_ids)
    count_ideas = sum(1 for t in by_column["ideas"] if t.id not in parented_ids)
    count_wontdo = sum(1 for t in by_column["wontdo"] if t.id not in parented_ids)
    count_review = sum(1 for t in by_column["review"] if t.id not in parented_ids)
    count_done = sum(1 for t in by_column["done"] if t.id not in parented_ids)
    count_icebox = sum(1 for t in by_column["icebox"] if t.id not in parented_ids)
    count_bugs = sum(1 for t in by_column["bugs"] if t.id not in parented_ids)
    count_total = count_backlog + count_wip + count_review + count_ideas + count_done

    # Cross-cutting filter counts (across all columns, excluding children)
    all_visible = [t for col in by_column.values() for t in col if t.id not in parented_ids]
    count_status_proposed = sum(1 for t in all_visible if t.status.replace(" ", "-").lower() == "proposed")
    count_status_inprogress = sum(1 for t in all_visible if t.status.replace(" ", "-").lower() == "in-progress")
    count_status_forreview = sum(1 for t in all_visible if t.status.replace(" ", "-").lower() == "for-review")
    count_type_bug = sum(1 for t in all_visible if t.column == "bugs" or t.status.replace(" ", "-").lower() in ("bug", "bug-fixed"))
    count_size_s = sum(1 for t in all_visible if t.complexity == "S")
    count_size_m = sum(1 for t in all_visible if t.complexity == "M")
    count_size_l = sum(1 for t in all_visible if t.complexity == "L")

    # Progress: done items / (done + remaining)
    total_all = count_total + count_wontdo + count_icebox
    progress_pct = round((count_done / total_all * 100)) if total_all > 0 else 0

    # Compute dependency state
    dep_state = compute_dependency_state(all_tickets)

    # Build card HTML
    backlog_cards = _render_cards(by_column["backlog"], "backlog", child_tickets, dep_state)
    wip_cards = _render_cards(by_column["wip"], "wip", child_tickets, dep_state)
    ideas_cards = _render_cards(by_column["ideas"], "ideas", child_tickets, dep_state)
    wontdo_cards = _render_list_rows(by_column["wontdo"], "wontdo", child_tickets, dep_state)
    review_cards = _render_cards(by_column["review"], "review", child_tickets, dep_state)
    done_cards = _render_list_rows(by_column["done"], "done", child_tickets, dep_state)
    icebox_cards = _render_list_rows(by_column["icebox"], "icebox", child_tickets, dep_state)
    bugs_cards = _render_list_rows(by_column["bugs"], "bugs", child_tickets, dep_state)

    releases_text = f"{cs.releases} releases" if cs.releases != 1 else "1 release"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="gen-ts" content="{gen_ts}">
<meta name="schema-version" content="2">
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
  align-items: flex-start;
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
.card-rationale {{ font-size: 11px; color: var(--text-tertiary); line-height: 1.3; margin-top: 4px; font-style: italic; display: none; }}
.card.expanded .card-rationale {{ display: block; }}
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
  margin-left: 20px; padding-left: 12px;
  border-left: 1px solid var(--border-default);
}}
.child-group .card {{ margin-left: 0; position: relative; }}
.child-group .card::before {{
  content: ''; position: absolute; left: -13px; top: 12px;
  width: 8px; border-top: 1px solid var(--border-default);
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
.edit-enabled .priority-dot:hover {{ transform: scale(1.5); transition: transform 0.15s; }}
.edit-enabled .status-badge:hover {{ filter: brightness(1.3); transition: filter 0.15s; }}

/* Drag-drop (edit mode) */
.edit-enabled .card {{ cursor: grab; }}
.edit-enabled .card:active {{ cursor: grabbing; }}
.card.dragging {{ opacity: 0.4; }}
.card.drag-target {{ border-color: var(--accent) !important; box-shadow: 0 0 0 1px var(--accent), 0 0 8px rgba(59,130,246,0.2); }}
.column-body.drag-over {{ background: rgba(59,130,246,0.06); border: 1px dashed var(--accent); border-radius: 6px; min-height: 40px; }}
.bottom-section-body.drag-over {{ background: rgba(59,130,246,0.06); }}

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
.gate-panel {{
  margin-top: 8px; padding: 10px; border-radius: 6px;
  background: var(--bg-surface); border: 1px solid var(--border-default);
  animation: panelSlide 0.2s ease-out; font-size: 11px;
}}
.gate-verdict {{
  display: flex; align-items: center; gap: 6px; margin-bottom: 8px;
  padding-bottom: 6px; border-bottom: 1px solid var(--border-default);
}}
.gate-verdict-badge {{
  font-size: 9px; font-weight: 700; padding: 2px 8px; border-radius: 10px;
  text-transform: uppercase; letter-spacing: 0.5px;
}}
.gate-verdict-badge.ready {{ background: rgba(34,197,94,0.15); color: #22c55e; }}
.gate-verdict-badge.needs-work {{ background: rgba(234,179,8,0.15); color: #eab308; }}
.gate-verdict-badge.blocked {{ background: rgba(239,68,68,0.15); color: #ef4444; }}
.gate-verdict-summary {{ color: var(--text-secondary); font-size: 11px; }}

/* Gate category rows */
.gate-category {{
  padding: 6px 8px; margin: 4px 0; border-radius: 4px;
  border-left: 3px solid var(--border-default);
  background: var(--bg-card);
}}
.gate-category.ok {{ border-left-color: #22c55e; }}
.gate-category.needs-work {{ border-left-color: #eab308; }}
.gate-cat-header {{
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 4px;
}}
.gate-cat-label {{
  font-weight: 700; font-size: 10px; text-transform: uppercase;
  letter-spacing: 0.3px; color: var(--text-secondary);
}}
.gate-cat-status {{
  font-size: 9px; font-weight: 600; padding: 1px 6px; border-radius: 8px;
}}
.gate-cat-status.ok {{ background: rgba(34,197,94,0.12); color: #22c55e; }}
.gate-cat-status.needs-work {{ background: rgba(234,179,8,0.12); color: #eab308; }}
.gate-cat-summary {{ color: var(--text-secondary); margin-bottom: 3px; }}
.gate-cat-suggestion {{
  color: var(--accent); font-style: italic; margin-bottom: 4px;
  padding: 3px 6px; background: rgba(59,130,246,0.06); border-radius: 3px;
}}
.gate-cat-edit {{
  width: 100%; min-height: 28px; font-size: 11px; padding: 4px 6px;
  border-radius: 4px; border: 1px solid var(--border-default);
  background: var(--bg-page); color: var(--text-primary);
  font-family: var(--font-sans); resize: vertical; outline: none;
}}
.gate-cat-edit:focus {{ border-color: var(--accent); }}
.gate-save-btn {{
  font-size: 9px; padding: 2px 8px; border-radius: 4px;
  border: 1px solid var(--border-default); background: var(--bg-page);
  color: var(--text-secondary); cursor: pointer; font-weight: 600; margin-top: 3px;
}}
.gate-save-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
.gate-save-btn.saved {{ color: #22c55e; border-color: #22c55e; pointer-events: none; }}

/* Gate panel footer */
.gate-footer {{
  display: flex; gap: 6px; margin-top: 8px; padding-top: 6px;
  border-top: 1px solid var(--border-default);
}}
.gate-confirm-btn {{
  font-size: 10px; padding: 4px 14px; border-radius: 4px; border: none;
  background: var(--accent); color: #fff; cursor: pointer; font-weight: 600;
  font-family: var(--font-sans);
}}
.gate-confirm-btn:hover {{ background: #2563eb; }}
.gate-cancel-btn {{
  font-size: 10px; padding: 4px 14px; border-radius: 4px;
  border: 1px solid var(--border-default); background: none;
  color: var(--text-secondary); cursor: pointer; font-family: var(--font-sans);
}}
.gate-cancel-btn:hover {{ color: var(--text-primary); border-color: var(--text-secondary); }}

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
  width: 16px; height: 16px; border-radius: 50%; font-size: 8px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono); line-height: 1; cursor: default;
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
.detail-panel {{ position: relative; width: 90vw; max-width: 820px; max-height: 88vh; background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 12px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 24px 60px rgba(0,0,0,0.5); }}
.detail-header {{ display: flex; align-items: center; gap: 10px; padding: 14px 20px; border-bottom: 1px solid var(--border-subtle); }}
.detail-header .detail-id {{ font-family: var(--font-mono); font-size: 13px; color: var(--accent); font-weight: 700; }}
.detail-header .detail-title {{ font-size: 15px; font-weight: 600; color: var(--text-primary); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.detail-close {{ background: none; border: none; color: var(--text-tertiary); font-size: 22px; cursor: pointer; padding: 0 4px; line-height: 1; }}
.detail-close:hover {{ color: var(--text-primary); }}
.detail-tabs {{ display: flex; gap: 4px; padding: 10px 20px; border-bottom: 1px solid var(--border-subtle); }}
.detail-tab {{ width: 36px; height: 28px; border-radius: 6px; font-size: 12px; font-weight: 700; font-family: var(--font-mono); display: flex; align-items: center; justify-content: center; cursor: pointer; border: 1px solid var(--border-subtle); background: transparent; color: var(--text-tertiary); transition: all 0.15s; }}
.detail-tab.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.detail-tab.filled {{ color: var(--status-done); border-color: rgba(34,197,94,0.3); }}
.detail-tab.active.filled {{ background: var(--accent); color: #fff; }}
.detail-body {{ flex: 1; overflow-y: auto; padding: 16px 20px; }}
.detail-section {{ display: none; }}
.detail-section.active {{ display: block; }}
.detail-section-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }}
.detail-section-header h3 {{ margin: 0; font-size: 14px; font-weight: 600; color: var(--text-primary); }}
.detail-clipboard-btns {{ display: flex; gap: 6px; }}
.detail-clip-btn {{ font-size: 11px; padding: 4px 10px; border-radius: 6px; border: 1px solid var(--border-default); background: var(--bg-card); color: var(--text-secondary); cursor: pointer; font-family: var(--font-mono); transition: all 0.15s; }}
.detail-clip-btn:hover {{ border-color: var(--accent); color: var(--text-primary); }}
.detail-editor {{ width: 100%; min-height: 180px; background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-default); border-radius: 6px; padding: 12px; font-family: var(--font-mono); font-size: 13px; resize: vertical; line-height: 1.5; box-sizing: border-box; }}
.detail-editor:focus {{ outline: none; border-color: var(--accent); }}
.detail-criteria-list {{ list-style: none; padding: 0; margin: 0 0 10px 0; }}
.detail-criteria-item {{ display: flex; align-items: flex-start; gap: 8px; padding: 4px 0; font-size: 13px; color: var(--text-secondary); }}
.detail-criteria-item input[type="checkbox"] {{ margin-top: 3px; }}
.detail-save-row {{ display: flex; justify-content: flex-end; margin-top: 10px; gap: 8px; }}
.detail-save-btn {{ font-size: 12px; padding: 6px 16px; border-radius: 6px; border: 1px solid var(--accent); background: var(--accent); color: #fff; cursor: pointer; font-weight: 600; }}
.detail-save-btn:hover {{ opacity: 0.9; }}
.detail-toast {{ position: absolute; top: 14px; right: 60px; font-size: 11px; font-weight: 600; color: var(--status-done); background: var(--status-done-bg); padding: 3px 10px; border-radius: 4px; opacity: 0; transition: opacity 0.3s; pointer-events: none; }}
.detail-toast.show {{ opacity: 1; }}

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
  <input type="text" class="search-input" id="searchInput" placeholder="Search items...">
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
      var col = this.dataset.column;
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
      root.querySelectorAll('[data-item-id]').forEach(function(el) {{ map[el.dataset.itemId] = el; }});
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
        var oldCol = oldEl.dataset.column, newCol = newEl.dataset.column;
        var wasExpanded = oldEl.classList.contains('expanded');

        if (oldCol !== newCol) {{
          var sel = findContainerSel(newEl, newDoc);
          if (sel) {{
            var target = document.querySelector(sel);
            if (target) {{
              oldEl.dataset.column = newCol;
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
          var id = this.dataset.itemId, title = this.dataset.title, col = this.dataset.column, text;
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
        card.classList.add('gate-checking', 'expanded');
      }} else {{
        card.classList.remove('gate-checking');
      }}
    }}

    function removeGatePanel(card) {{
      var panel = card.querySelector('.gate-panel');
      if (panel) panel.remove();
      card.classList.remove('gate-checking');
    }}

    function startGateCheck(ticketId, targetSection) {{
      var card = document.querySelector('[data-item-id="' + ticketId + '"]');
      if (!card) return;
      removeGatePanel(card);
      setCardGateChecking(card, true);
      apiGateCheck(ticketId, targetSection).then(function(data) {{
        setCardGateChecking(card, false);
        renderGatePanel(card, data, ticketId, targetSection);
      }}).catch(function(err) {{
        setCardGateChecking(card, false);
        showToast(card, 'Gate check failed');
      }});
    }}

    function renderGatePanel(card, data, ticketId, targetSection) {{
      removeGatePanel(card);
      card.classList.add('expanded');
      var panel = document.createElement('div');
      panel.className = 'gate-panel';

      // Verdict header — safe DOM construction
      var verdict = data.verdict || 'needs-work';
      var verdictDiv = document.createElement('div');
      verdictDiv.className = 'gate-verdict';
      var badge = document.createElement('span');
      badge.className = 'gate-verdict-badge ' + verdict;
      badge.textContent = verdict.replace(/-/g, ' ');
      var summarySpan = document.createElement('span');
      summarySpan.className = 'gate-verdict-summary';
      summarySpan.textContent = data.summary || '';
      verdictDiv.appendChild(badge);
      verdictDiv.appendChild(summarySpan);
      panel.appendChild(verdictDiv);

      // Category rows
      var catOrder = ['D', 'C', 'T', 'R', 'S'];
      var catLabels = {{ D: 'Description', C: 'Criteria', T: 'Tests', R: 'Reviewed', S: 'Smoke Tested' }};
      var cats = data.categories || {{}};

      catOrder.forEach(function(key) {{
        var cat = cats[key] || {{}};
        var status = cat.status || 'ok';
        var row = document.createElement('div');
        row.className = 'gate-category ' + status;
        row.dataset.cat = key;

        // Header
        var header = document.createElement('div');
        header.className = 'gate-cat-header';
        var label = document.createElement('span');
        label.className = 'gate-cat-label';
        label.textContent = key + ' \\u2014 ' + catLabels[key];
        var statusEl = document.createElement('span');
        statusEl.className = 'gate-cat-status ' + status;
        statusEl.textContent = status.replace(/-/g, ' ');
        header.appendChild(label);
        header.appendChild(statusEl);
        row.appendChild(header);

        // Summary
        var summaryDiv = document.createElement('div');
        summaryDiv.className = 'gate-cat-summary';
        summaryDiv.textContent = cat.current_summary || '';
        row.appendChild(summaryDiv);

        // Suggestion
        if (cat.suggestion) {{
          var sugDiv = document.createElement('div');
          sugDiv.className = 'gate-cat-suggestion';
          sugDiv.textContent = cat.suggestion;
          row.appendChild(sugDiv);
        }}

        // Editable field for Description
        if (key === 'D') {{
          var ta = document.createElement('textarea');
          ta.className = 'gate-cat-edit';
          ta.dataset.field = 'description';
          ta.placeholder = 'Edit description...';
          ta.value = card.dataset.desc || '';
          row.appendChild(ta);
        }}

        // Suggested new criteria for C
        if (key === 'C') {{
          var addCriteria = cat.add_criteria || [];
          if (addCriteria.length > 0) {{
            var hint = document.createElement('div');
            hint.style.cssText = 'margin-top:3px;font-size:10px;color:var(--text-tertiary)';
            hint.textContent = 'Suggested additions:';
            row.appendChild(hint);
            addCriteria.forEach(function(c, i) {{
              var cta = document.createElement('textarea');
              cta.className = 'gate-cat-edit';
              cta.dataset.field = 'add_criteria';
              cta.dataset.index = String(i);
              cta.style.cssText = 'min-height:22px;margin-top:2px';
              cta.value = c;
              row.appendChild(cta);
            }});
          }}
        }}

        // Save button for D and C
        if (key === 'D' || (key === 'C' && (cat.add_criteria || []).length > 0)) {{
          var saveBtn = document.createElement('button');
          saveBtn.className = 'gate-save-btn';
          saveBtn.dataset.cat = key;
          saveBtn.textContent = 'Save ' + catLabels[key];
          row.appendChild(saveBtn);
        }}

        panel.appendChild(row);
      }});

      // Footer
      var footer = document.createElement('div');
      footer.className = 'gate-footer';
      var confirmBtn = document.createElement('button');
      confirmBtn.className = 'gate-confirm-btn';
      confirmBtn.textContent = 'Confirm Move \\u2192 ' + targetSection;
      var cancelBtn = document.createElement('button');
      cancelBtn.className = 'gate-cancel-btn';
      cancelBtn.textContent = 'Cancel';
      footer.appendChild(confirmBtn);
      footer.appendChild(cancelBtn);
      panel.appendChild(footer);

      // Wire up per-section Save buttons
      panel.querySelectorAll('.gate-save-btn').forEach(function(btn) {{
        btn.addEventListener('click', function(e) {{
          e.stopPropagation();
          var catKey = btn.dataset.cat;
          var catRow = btn.closest('.gate-category');

          if (catKey === 'D') {{
            var textarea = catRow.querySelector('textarea[data-field="description"]');
            if (textarea) {{
              apiPut(ticketId, {{ description: textarea.value }}).then(function() {{
                btn.textContent = 'Saved \\u2714';
                btn.classList.add('saved');
              }});
            }}
          }} else if (catKey === 'C') {{
            var fields = catRow.querySelectorAll('textarea[data-field="add_criteria"]');
            var promises = [];
            fields.forEach(function(f) {{
              var text = f.value.trim();
              if (text) {{
                promises.push(apiPut(ticketId, {{ add_criteria: text }}));
              }}
            }});
            Promise.all(promises).then(function() {{
              btn.textContent = 'Saved \\u2714';
              btn.classList.add('saved');
            }});
          }}
        }});
      }});

      // Wire up Confirm Move
      confirmBtn.addEventListener('click', function(e) {{
        e.stopPropagation();
        if (targetSection === 'Done') {{
          fetch(EDIT_API + '/tickets/' + ticketId + '/accept', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: '{{}}'
          }}).then(function(r) {{ return r.json(); }}).then(function() {{
            removeGatePanel(card);
            showToast(card, 'Accepted!');
          }});
        }} else {{
          apiMove(ticketId, targetSection).then(function() {{
            removeGatePanel(card);
            showToast(card, 'Moved!');
          }});
        }}
      }});

      // Wire up Cancel
      cancelBtn.addEventListener('click', function(e) {{
        e.stopPropagation();
        removeGatePanel(card);
      }});

      // Insert panel into card (before action buttons if present)
      var actions = card.querySelector('.card-actions');
      if (actions) {{
        card.insertBefore(panel, actions);
      }} else {{
        card.appendChild(panel);
      }}
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
        apiPut(card.dataset.itemId, {{ priority: next }}).then(function() {{
          showToast(card, next);
        }});
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
        // Create dropdown
        var statuses = ['proposed','specified','ready','in-progress','blocked','rework','for-review','done','bug','bug-fixed','icebox','wont-do'];
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
            apiPut(card.dataset.itemId, {{ status: s }}).then(function() {{
              showToast(card, s);
            }});
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

      // Acceptance criteria checkbox click — toggle
      document.addEventListener('click', function(e) {{
        var criterion = e.target.closest('.criterion');
        if (!criterion) return;
        var card = criterion.closest('.card');
        if (!card || !card.dataset.itemId) return;
        e.stopPropagation();
        e.preventDefault();
        clearTimeout(card._clickTimer);
        var criteriaContainer = criterion.closest('.card-criteria');
        if (!criteriaContainer) return;
        var allCriteria = criteriaContainer.querySelectorAll('.criterion');
        var idx = Array.prototype.indexOf.call(allCriteria, criterion);
        if (idx < 0) return;
        // Toggle visual state
        var isChecked = criterion.classList.contains('checked');
        criterion.classList.toggle('checked');
        // Update the marker text safely (replace first character entity)
        var textNode = criterion.firstChild;
        if (textNode) {{
          var newMarker = isChecked ? '\u2610 ' : '\u2611 ';
          criterion.textContent = newMarker + criterion.textContent.substring(2);
        }}
        apiPut(card.dataset.itemId, {{ toggle_criterion: idx }}).then(function() {{
          showToast(card, isChecked ? 'unchecked' : 'checked');
        }});
      }}, true);

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
      document.querySelectorAll('.column-body, .bottom-section-body').forEach(function(zone) {{
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
          // Determine target section from parent element
          var parent = zone.parentElement;
          var section = null;
          if (parent && parent.dataset && parent.dataset.col) {{
            // Kanban column
            var colMap = {{ ideas: 'Ideas', backlog: 'Backlog', wip: 'WIP', review: 'For Review' }};
            section = colMap[parent.dataset.col];
          }} else if (parent && parent.id) {{
            // Bottom section
            var secMap = {{ bugSection: 'Bugs', iceboxSection: 'Icebox', doneSection: 'Done', wontdoSection: "Won't Do" }};
            section = secMap[parent.id];
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

      // --- Inline text editing ---
      document.addEventListener('dblclick', function(e) {{
        var titleEl = e.target.closest('.card-title');
        var descEl = e.target.closest('.card-desc');
        var target = titleEl || descEl;
        if (!target) return;
        var card = target.closest('.card');
        if (!card || !card.dataset.itemId) return;
        // Only in expanded cards
        if (!card.classList.contains('expanded')) return;
        e.stopPropagation();
        e.preventDefault();
        clearTimeout(card._clickTimer);
        // Prevent starting edit if already editing
        if (target.contentEditable === 'true') return;
        card.dataset.editing = 'true';
        target.contentEditable = 'true';
        target.focus();
        // Select all text
        var range = document.createRange();
        range.selectNodeContents(target);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        // Save on blur
        function save() {{
          target.contentEditable = 'false';
          card.dataset.editing = '';
          var field = titleEl ? 'title' : 'description';
          var value = target.textContent.trim();
          if (field === 'title') card.dataset.title = value;
          if (field === 'description') card.dataset.desc = value;
          var body = {{}};
          body[field] = value;
          apiPut(card.dataset.itemId, body).then(function() {{
            showToast(card, 'Saved');
          }});
          target.removeEventListener('blur', save);
          target.removeEventListener('keydown', keyHandler);
        }}
        function keyHandler(ev) {{
          if (ev.key === 'Enter' && !ev.shiftKey) {{ ev.preventDefault(); target.blur(); }}
          if (ev.key === 'Escape') {{ target.textContent = titleEl ? card.dataset.title : card.dataset.desc; target.blur(); }}
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
  }})();
}})();
</script>

<!-- Detail overlay for readiness flag editing -->
<div id="ticket-detail-overlay" class="detail-overlay hidden">
  <div class="detail-backdrop"></div>
  <div class="detail-panel">
    <div class="detail-header">
      <span class="detail-id"></span>
      <span class="detail-title"></span>
      <span class="detail-toast"></span>
      <button class="detail-close">&times;</button>
    </div>
    <div class="detail-tabs">
      <button class="detail-tab" data-section="description">D</button>
      <button class="detail-tab" data-section="criteria">C</button>
      <button class="detail-tab" data-section="tests">T</button>
      <button class="detail-tab" data-section="reviewed">R</button>
      <button class="detail-tab" data-section="smoke">S</button>
    </div>
    <div class="detail-body">
      <div class="detail-section" data-section="description">
        <div class="detail-section-header"><h3>Description</h3>
          <div class="detail-clipboard-btns">
            <button class="detail-clip-btn" data-action="create" data-flag="description">Create New</button>
            <button class="detail-clip-btn" data-action="review" data-flag="description">Review Existing</button>
          </div>
        </div>
        <textarea class="detail-editor" data-field="description" placeholder="Ticket description..."></textarea>
        <div class="detail-save-row"><button class="detail-save-btn" data-field="description">Save</button></div>
      </div>
      <div class="detail-section" data-section="criteria">
        <div class="detail-section-header"><h3>Acceptance Criteria</h3>
          <div class="detail-clipboard-btns">
            <button class="detail-clip-btn" data-action="create" data-flag="criteria">Create New</button>
            <button class="detail-clip-btn" data-action="review" data-flag="criteria">Review Existing</button>
          </div>
        </div>
        <ul class="detail-criteria-list"></ul>
        <textarea class="detail-editor" data-field="criteria" placeholder="Add new criteria (one per line)..." style="min-height:80px"></textarea>
        <div class="detail-save-row"><button class="detail-save-btn" data-field="criteria">Add Criteria</button></div>
      </div>
      <div class="detail-section" data-section="tests">
        <div class="detail-section-header"><h3>Tests</h3>
          <div class="detail-clipboard-btns">
            <button class="detail-clip-btn" data-action="create" data-flag="tests">Create New</button>
            <button class="detail-clip-btn" data-action="review" data-flag="tests">Review Existing</button>
          </div>
        </div>
        <textarea class="detail-editor" data-field="tests" placeholder="Test definitions, TDD plan, coverage notes..."></textarea>
        <div class="detail-save-row"><button class="detail-save-btn" data-field="tests">Save</button></div>
      </div>
      <div class="detail-section" data-section="reviewed">
        <div class="detail-section-header"><h3>Review</h3>
          <div class="detail-clipboard-btns">
            <button class="detail-clip-btn" data-action="create" data-flag="reviewed">Create New</button>
            <button class="detail-clip-btn" data-action="review" data-flag="reviewed">Review Existing</button>
          </div>
        </div>
        <textarea class="detail-editor" data-field="reviewed" placeholder="Review notes: decisions, bugs found, feature implications, /sync output..."></textarea>
        <div class="detail-save-row"><button class="detail-save-btn" data-field="reviewed">Save</button></div>
      </div>
      <div class="detail-section" data-section="smoke">
        <div class="detail-section-header"><h3>Smoke Tests</h3>
          <div class="detail-clipboard-btns">
            <button class="detail-clip-btn" data-action="create" data-flag="smoke">Create New</button>
            <button class="detail-clip-btn" data-action="review" data-flag="smoke">Review Existing</button>
          </div>
        </div>
        <textarea class="detail-editor" data-field="smoke" placeholder="Smoke test plan: manual verification steps, pass/fail results..."></textarea>
        <div class="detail-save-row"><button class="detail-save-btn" data-field="smoke">Save</button></div>
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
  var tabEls = overlay.querySelectorAll('.detail-tab');
  var secEls = overlay.querySelectorAll('.detail-section');
  var currentTicketId = null;
  var currentData = null;

  var FLAG_NAMES = {{ description:'Description', criteria:'Acceptance Criteria', tests:'Tests', reviewed:'Review', smoke:'Smoke Tests' }};

  var PROMPTS = {{
    description: {{
      create: function(t) {{ return 'Write a detailed description for ' + t.id + ': "' + t.title + '". Include problem statement, proposed solution, scope, and constraints.'; }},
      review: function(t) {{ return 'Review the description for ' + t.id + ': "' + t.title + '". Check clarity, completeness, feasibility.\\n\\nDescription:\\n' + (t.description || '(empty)'); }}
    }},
    criteria: {{
      create: function(t) {{ return 'Write acceptance criteria for ' + t.id + ': "' + t.title + '". Use Given/When/Then or checkbox format. Cover happy path, errors, edge cases.\\n\\nDescription:\\n' + (t.description || '(empty)'); }},
      review: function(t) {{ return 'Review acceptance criteria for ' + t.id + ': "' + t.title + '". Check testability, completeness, clarity.\\n\\nCriteria:\\n' + (t.criteria_text || '(none)'); }}
    }},
    tests: {{
      create: function(t) {{ return 'Using TDD, write test definitions for ' + t.id + ': "' + t.title + '". Start with failing tests that define expected behavior.\\n\\nAcceptance criteria:\\n' + (t.criteria_text || '(none)'); }},
      review: function(t) {{ var c = (t.readiness_flags && t.readiness_flags.tests) || ''; return 'Review test definitions for ' + t.id + ': "' + t.title + '". Check coverage gaps, edge cases, alignment with criteria.\\n\\nTests:\\n' + (c || '(empty)'); }}
    }},
    reviewed: {{
      create: function(t) {{ return 'Perform a review for ' + t.id + ': "' + t.title + '". Capture: decisions made, bugs found, feature implications, architectural trade-offs. Run /sync to collect session context.\\n\\nDescription:\\n' + (t.description || '(empty)'); }},
      review: function(t) {{ var c = (t.readiness_flags && t.readiness_flags.reviewed) || ''; return 'Review the review notes for ' + t.id + ': "' + t.title + '". Verify all items addressed, no regressions, approval criteria met.\\n\\nReview notes:\\n' + (c || '(empty)'); }}
    }},
    smoke: {{
      create: function(t) {{ return 'Create a smoke test plan for ' + t.id + ': "' + t.title + '". Define manual verification steps: happy path, error cases, browser/device matrix.\\n\\nAcceptance criteria:\\n' + (t.criteria_text || '(none)'); }},
      review: function(t) {{ var c = (t.readiness_flags && t.readiness_flags.smoke) || ''; return 'Review smoke test results for ' + t.id + ': "' + t.title + '". Verify all cases executed, pass/fail documented.\\n\\nSmoke test notes:\\n' + (c || '(empty)'); }}
    }}
  }};

  function toast(msg) {{ toastEl.textContent = msg; toastEl.classList.add('show'); setTimeout(function() {{ toastEl.classList.remove('show'); }}, 1500); }}

  function activateTab(name) {{
    tabEls.forEach(function(t) {{ t.classList.toggle('active', t.dataset.section === name); }});
    secEls.forEach(function(s) {{ s.classList.toggle('active', s.dataset.section === name); }});
  }}

  function refreshTabs() {{
    if (!currentData) return;
    var fl = currentData.readiness_flags || {{}};
    tabEls.forEach(function(t) {{
      var s = t.dataset.section;
      var ok = s === 'description' ? !!(currentData.description) : s === 'criteria' ? (currentData.acceptance_criteria || []).length > 0 : !!(fl[s]);
      t.classList.toggle('filled', ok);
    }});
  }}

  function populate(data) {{
    currentData = data;
    idEl.textContent = data.id;
    titleEl.textContent = data.title;
    overlay.querySelector('[data-field="description"]').value = data.description || '';

    var list = overlay.querySelector('.detail-criteria-list');
    while (list.firstChild) list.removeChild(list.firstChild);
    (data.acceptance_criteria || []).forEach(function(c, i) {{
      var li = document.createElement('li'); li.className = 'detail-criteria-item';
      var cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = c.checked;
      cb.addEventListener('change', function() {{
        fetch(EDIT_API + '/tickets/' + data.id, {{ method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{toggle_criterion:i}}) }})
          .then(function(r){{return r.json();}}).then(function(u){{ if(u&&u.acceptance_criteria) currentData=u; }});
      }});
      var sp = document.createElement('span'); sp.textContent = c.text;
      li.appendChild(cb); li.appendChild(sp); list.appendChild(li);
    }});
    var ce = overlay.querySelector('[data-field="criteria"]'); if(ce) ce.value='';

    var fl = data.readiness_flags || {{}};
    ['tests','reviewed','smoke'].forEach(function(f) {{
      var ed = overlay.querySelector('[data-field="'+f+'"]');
      if(ed) ed.value = fl[f] || '';
    }});
    refreshTabs();
  }}

  function openOverlay(tid, section) {{
    currentTicketId = tid;
    overlay.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    fetch(EDIT_API+'/tickets/'+tid).then(function(r){{return r.json();}}).then(function(d){{
      populate(d); activateTab(section||'description');
    }});
  }}

  function closeOverlay() {{
    overlay.classList.add('hidden');
    document.body.style.overflow = '';
    currentTicketId = null; currentData = null;
  }}

  overlay.querySelector('.detail-backdrop').addEventListener('click', closeOverlay);
  overlay.querySelector('.detail-close').addEventListener('click', closeOverlay);
  document.addEventListener('keydown', function(e) {{ if(e.key==='Escape' && !overlay.classList.contains('hidden')) closeOverlay(); }});

  tabEls.forEach(function(tab) {{ tab.addEventListener('click', function() {{ activateTab(tab.dataset.section); }}); }});

  // Save buttons
  overlay.querySelectorAll('.detail-save-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var field = btn.dataset.field;
      var ed = overlay.querySelector('[data-field="'+field+'"]');
      if(!ed || !currentTicketId) return;
      var val = ed.value;

      if(field === 'description') {{
        fetch(EDIT_API+'/tickets/'+currentTicketId, {{method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{description:val}})}})
          .then(function(r){{return r.json();}}).then(function(u){{ if(u)currentData=u; toast('Description saved'); refreshTabs(); }});
      }} else if(field === 'criteria') {{
        var lines = val.split('\\n').filter(function(l){{return l.trim();}});
        if(!lines.length) return;
        var chain = Promise.resolve();
        lines.forEach(function(line) {{
          var text = line.replace(/^-\\s*\\[[ xX]?\\]\\s*/, '').trim();
          if(!text) return;
          chain = chain.then(function() {{
            return fetch(EDIT_API+'/tickets/'+currentTicketId, {{method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{add_criteria:text}})}}).then(function(r){{return r.json();}});
          }});
        }});
        chain.then(function() {{ return fetch(EDIT_API+'/tickets/'+currentTicketId).then(function(r){{return r.json();}}); }})
          .then(function(d) {{ populate(d); activateTab('criteria'); toast('Criteria added'); }});
      }} else {{
        fetch(EDIT_API+'/tickets/'+currentTicketId+'/readiness/'+field, {{method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{content:val}})}})
          .then(function(r){{return r.json();}}).then(function(u){{ if(u)currentData=u; toast(FLAG_NAMES[field]+' saved'); refreshTabs(); }});
      }}
    }});
  }});

  // Clipboard buttons
  overlay.querySelectorAll('.detail-clip-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      if(!currentData) return;
      var fn = PROMPTS[btn.dataset.flag] && PROMPTS[btn.dataset.flag][btn.dataset.action];
      if(!fn) return;
      navigator.clipboard.writeText(fn(currentData)).then(function(){{ toast('Copied!'); }});
    }});
  }});

  // Ctrl+S saves active section
  overlay.addEventListener('keydown', function(e) {{
    if((e.ctrlKey||e.metaKey) && e.key==='s') {{
      e.preventDefault();
      var sec = overlay.querySelector('.detail-section.active');
      if(sec) {{ var sb = sec.querySelector('.detail-save-btn'); if(sb) sb.click(); }}
    }}
  }});

  // Readiness dot click — open detail view
  document.addEventListener('click', function(e) {{
    var dot = e.target.closest('.readiness-dot[data-flag]');
    if(!dot) return;
    var card = dot.closest('.card') || dot.closest('.list-row');
    if(!card || !card.dataset.itemId) return;
    e.stopPropagation(); e.preventDefault();
    if(card._clickTimer) clearTimeout(card._clickTimer);
    openOverlay(card.dataset.itemId, dot.dataset.flag);
  }}, true);

  window.DETAIL_OVERLAY_OPEN = function() {{ return currentTicketId; }};
}})();
</script>
</body>
</html>"""

    return html


def _render_cards(tickets: list[Ticket], column: str, child_tickets: dict[str, list] = None, dep_state: dict = None) -> str:
    """Render full-size kanban cards."""
    if child_tickets is None:
        child_tickets = {}
    if dep_state is None:
        dep_state = {}
    card_class = CARD_CLASS_BY_COLUMN.get(column, "")
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
                f'<span class="children-toggle" data-parent="{id_esc}">'
                f'<span class="arrow">&#9660;</span> {n_children}</span>'
            )

        lines.append(_render_single_card(t, column, card_class, dep_state, child_badge_html))

        # Render children as full cards in a connected group
        if children:
            lines.append(f'      <div class="child-group" data-parent="{id_esc}">')
            for child in children:
                lines.append(_render_single_card(child, column, card_class, dep_state, ""))
            lines.append(f'      </div>')

    return "\n".join(lines)


def _render_single_card(t, column: str, card_class: str, dep_state: dict, child_badge_html: str) -> str:
    """Render a single card (parent or child) as full HTML."""
    title_esc = escape(t.title)
    id_esc = escape(t.id)
    desc_esc = escape(t.description) if t.description else ""
    status_class = t.status.replace(" ", "-").lower()

    dep_info = dep_state.get(t.id, {})
    blocked_class = " blocked" if dep_info.get("blocking_deps") else ""

    parent_link_html = ""
    if t.parent:
        parent_link_html = f'        <div class="card-parent-link">\u21b3 {escape(t.parent)}</div>\n'

    deps_html = ""
    if t.depends:
        dep_list = ", ".join(escape(d) for d in t.depends)
        deps_html = f'        <div class="card-deps">&#10547; {dep_list}</div>\n'
        blocking = dep_info.get("blocking_deps", [])
        if blocking:
            deps_html += f'        <span class="card-blocked-badge">blocked by: {escape(", ".join(blocking))}</span>\n'

    desc_html = ""
    if t.description:
        desc_html = f'        <div class="card-desc">{desc_esc}</div>\n'

    rationale_html = ""
    if t.rationale:
        rationale_html = f'        <div class="card-rationale"><em>Rationale:</em> {escape(t.rationale)}</div>\n'

    criteria_html = ""
    if t.acceptance_criteria:
        criteria_items = []
        for checked, text in t.acceptance_criteria:
            cls = ' class="criterion checked"' if checked else ' class="criterion"'
            marker = "&#9745;" if checked else "&#9744;"
            criteria_items.append(f'          <div{cls}>{marker} {escape(text)}</div>')
        criteria_html = '        <div class="card-criteria">\n' + "\n".join(criteria_items) + "\n        </div>\n"

    readiness_html = _render_readiness_row(t)
    actions_html = _render_action_buttons(column, id_esc)

    return (
        f'      <div class="card {card_class}{blocked_class}" data-column="{column}" '
        f'data-title="{title_esc}" data-item-id="{id_esc}" data-desc="{desc_esc}" '
        f'data-status="{status_class}" data-complexity="{escape(t.complexity)}"'
        f'{"" if column != "bugs" and status_class not in ("bug", "bug-fixed") else " data-is-bug=" + chr(34) + "true" + chr(34)}'
        f'{" data-parent=" + chr(34) + escape(t.parent) + chr(34) if t.parent else ""}>\n'
        f'        <div class="copied-toast">Copied!</div>\n'
        f'        <div class="card-top"><span class="priority-dot {t.priority}"></span>'
        f'<span class="card-title">{title_esc}</span>{child_badge_html}</div>\n'
        f'        <div class="card-meta"><span class="card-id">{id_esc}</span>'
        f'<span class="status-badge {status_class}">{status_class}</span></div>\n'
        f'{readiness_html}'
        f'{parent_link_html}{deps_html}{desc_html}{rationale_html}{criteria_html}'
        f'{actions_html}'
        f'        <div class="card-footer"><span class="complexity-badge">{escape(t.complexity)}</span></div>\n'
        f'      </div>'
    )


def _render_readiness_row(t) -> str:
    """Render readiness indicator dots for a ticket."""
    flag_map = {"D": "description", "C": "criteria", "T": "tests", "R": "reviewed", "S": "smoke"}
    indicators = [
        ("D", "Description", bool(t.description)),
        ("C", "Criteria", len(t.acceptance_criteria) > 0),
        ("T", "Tests", "tests" in t.readiness_flags),
        ("R", "Reviewed", "reviewed" in t.readiness_flags),
        ("S", "Smoke tested", "smoke" in t.readiness_flags),
    ]
    dots = []
    for letter, title, filled in indicators:
        cls = "filled" if filled else "empty"
        flag_name = flag_map[letter]
        dots.append(f'<span class="readiness-dot {cls}" title="{title}" data-flag="{flag_name}">{letter}</span>')
    return '        <div class="readiness-row">' + "".join(dots) + '</div>\n'


def _render_action_buttons(column: str, ticket_id: str) -> str:
    """Render contextual action buttons for a card (only visible in edit mode when expanded)."""
    buttons = []
    if column == "ideas":
        buttons.append(f'<button class="action-btn primary" data-action="move" data-section="Backlog">&#8594; Backlog</button>')
    elif column == "backlog":
        buttons.append(f'<button class="action-btn primary" data-action="move" data-section="WIP">&#9654; Start</button>')
    elif column == "wip":
        buttons.append(f'<button class="action-btn primary" data-action="move" data-section="For Review">&#10003; Done</button>')
        buttons.append(f'<button class="action-btn" data-action="move" data-section="Icebox">&#10052; Icebox</button>')
    elif column == "review":
        buttons.append(f'<button class="action-btn primary" data-action="accept">&#10003; Accept</button>')
        buttons.append(f'<button class="action-btn" data-action="move" data-section="WIP">&#8592; Back to WIP</button>')
    if not buttons:
        return ""
    return '        <div class="card-actions">' + "".join(buttons) + '</div>\n'


def _render_list_rows(tickets: list[Ticket], column: str, child_tickets: dict[str, list] = None, dep_state: dict = None) -> str:
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
                f'<span class="children-toggle" data-parent="{id_esc}">'
                f'<span class="arrow">&#9660;</span> {len(children)}</span>'
            )

        # Expandable detail panel
        detail_parts = []
        if t.description:
            detail_parts.append(f'          <div class="card-desc" style="display:block">{desc_esc}</div>')
        if t.rationale:
            detail_parts.append(f'          <div class="card-rationale" style="display:block"><em>Rationale:</em> {escape(t.rationale)}</div>')
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
            f'      <div class="list-row card" data-column="{column}" '
            f'data-title="{title_esc}" data-item-id="{id_esc}" data-desc="{desc_esc}" '
            f'data-status="{status_class}" data-complexity="{escape(t.complexity)}"'
            f'{"" if column != "bugs" and status_class not in ("bug", "bug-fixed") else " data-is-bug=" + chr(34) + "true" + chr(34)}>\n'
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
            lines.append(f'      <div class="child-group" data-parent="{id_esc}">')
            for child in children:
                child_title = escape(child.title)
                child_id = escape(child.id)
                child_desc = escape(child.description) if child.description else ""
                child_status = child.status.replace(" ", "-").lower()
                lines.append(
                    f'      <div class="list-row card" data-column="{column}" '
                    f'data-title="{child_title}" data-item-id="{child_id}" data-desc="{child_desc}" '
                    f'data-status="{child_status}" data-complexity="{escape(child.complexity)}"'
                    f'{"" if column != "bugs" and child_status not in ("bug", "bug-fixed") else " data-is-bug=" + chr(34) + "true" + chr(34)}'
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
                "column": t.column,
                "description": t.description,
                "acceptance_criteria": [
                    {"checked": c, "text": txt} for c, txt in t.acceptance_criteria
                ],
                "parent": t.parent,
                "depends": t.depends,
                "rationale": t.rationale,
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

    # Route tickets to their correct columns
    for proj in projects:
        for t in proj.tickets:
            if t.section == "Done":
                t.column = "done"
            elif t.section == "For Review":
                t.column = "review"
            elif t.section == "Icebox":
                t.column = "icebox"
            elif t.section == "Bugs":
                t.column = "bugs"

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

    # Open first project's dashboard in browser
    if output_paths:
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
