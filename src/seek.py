"""Seek — discover ticket-like items in project files and create draft tickets.

Scanners are pure functions: (project_path: str) -> list[DiscoveredItem]
The orchestrator runs scanners, deduplicates against existing tickets, and
creates draft tickets via actions.add_ticket().
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from actions import ActorContext, add_ticket, emit_event

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredItem:
    title: str  # Cleaned text, max ~120 chars
    source_type: (
        str  # "md_task" | "readme_todo" | "code_todo" | "changelog" | "github_issue"
    )
    source_file: str  # Relative path from project root
    source_line: int  # Line number (or issue number for GitHub)
    raw_text: str  # Full original text -> becomes ticket description
    priority: str  # "medium" default, "high" for FIXME/HACK
    section: str  # Always "Ideas" for v1


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKIP_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "dist",
    "build",
    ".next",
    ".artifacts",
    ".feedbacks",
    "docs",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    "vendor",
    "target",
}

SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".go",
    ".rs",
    ".rb",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".css",
    ".lua",
    ".sh",
    ".bash",
    ".zsh",
}

MD_SKIP_FILES = {
    "README.md",
    "PRODUCT_BACKLOG.md",
    "PRODUCT_SPECIFICATION.md",
    "CHANGELOG.md",
}


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------


def scan_md_tasks(project_path: str) -> list[DiscoveredItem]:
    """Scan markdown files in project root for unchecked task items."""
    items = []
    root = Path(project_path)
    for md_file in root.glob("*.md"):
        if md_file.name in MD_SKIP_FILES:
            continue
        try:
            lines = md_file.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            # Skip checked items
            if re.match(r"^\s*- \[x\]", line, re.IGNORECASE):
                continue
            m = re.match(r"^\s*- \[ \]\s+(.+)$", line)
            if m:
                title = m.group(1).strip()
                items.append(
                    DiscoveredItem(
                        title=title,
                        source_type="md_task",
                        source_file=md_file.name,
                        source_line=lineno,
                        raw_text=title,
                        priority="medium",
                        section="Ideas",
                    )
                )
    return items


def scan_readme_todos(project_path: str) -> list[DiscoveredItem]:
    """Scan README.md for TODO/Roadmap/Planned/Future sections."""
    readme = Path(project_path) / "README.md"
    if not readme.exists():
        return []
    try:
        lines = readme.read_text(errors="replace").splitlines()
    except OSError:
        return []

    items = []
    in_section = False
    section_level = 0
    todo_pattern = re.compile(r"TODO|Roadmap|Planned|Future", re.IGNORECASE)

    for lineno, line in enumerate(lines, 1):
        header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if header_match:
            level = len(header_match.group(1))
            header_text = header_match.group(2)
            if todo_pattern.search(header_text):
                in_section = True
                section_level = level
                continue
            elif in_section and level <= section_level:
                in_section = False
                continue

        if in_section:
            bullet_match = re.match(r"^\s*[-*]\s+(.+)$", line)
            if bullet_match:
                title = bullet_match.group(1).strip()
                items.append(
                    DiscoveredItem(
                        title=title,
                        source_type="readme_todo",
                        source_file="README.md",
                        source_line=lineno,
                        raw_text=title,
                        priority="medium",
                        section="Ideas",
                    )
                )

    return items


def scan_code_todos(project_path: str) -> list[DiscoveredItem]:
    """Walk source files for TODO/FIXME/HACK comments."""
    items = []
    root = Path(project_path)
    todo_re = re.compile(r"(?:TODO|FIXME|HACK)\s*[:—\-]\s*(.+)", re.IGNORECASE)

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for fname in filenames:
            ext = os.path.splitext(fname)[1]
            if ext not in SOURCE_EXTENSIONS:
                continue
            fpath = Path(dirpath) / fname
            try:
                text = fpath.read_text(errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                m = todo_re.search(line)
                if m:
                    raw = m.group(1).strip()
                    # Strip trailing comment closers
                    raw = raw.rstrip("*/").strip()
                    # Determine priority
                    tag_match = re.search(r"(TODO|FIXME|HACK)", line, re.IGNORECASE)
                    tag = tag_match.group(1).upper() if tag_match else "TODO"
                    priority = "high" if tag in ("FIXME", "HACK") else "medium"

                    rel_path = str(fpath.relative_to(root))
                    items.append(
                        DiscoveredItem(
                            title=raw[:120],
                            source_type="code_todo",
                            source_file=rel_path,
                            source_line=lineno,
                            raw_text=raw,
                            priority=priority,
                            section="Ideas",
                        )
                    )
    return items


def scan_changelog_unreleased(project_path: str) -> list[DiscoveredItem]:
    """Scan CHANGELOG.md for items under an [Unreleased] section."""
    changelog = Path(project_path) / "CHANGELOG.md"
    if not changelog.exists():
        return []
    try:
        lines = changelog.read_text(errors="replace").splitlines()
    except OSError:
        return []

    items = []
    in_section = False
    section_level = 0
    unreleased_re = re.compile(r"unreleased", re.IGNORECASE)

    for lineno, line in enumerate(lines, 1):
        header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if header_match:
            level = len(header_match.group(1))
            header_text = header_match.group(2)
            if unreleased_re.search(header_text):
                in_section = True
                section_level = level
                continue
            elif in_section and level <= section_level:
                in_section = False
                continue

        if in_section:
            bullet_match = re.match(r"^\s*[-*]\s+(.+)$", line)
            if bullet_match:
                title = bullet_match.group(1).strip()
                items.append(
                    DiscoveredItem(
                        title=title,
                        source_type="changelog",
                        source_file="CHANGELOG.md",
                        source_line=lineno,
                        raw_text=title,
                        priority="medium",
                        section="Ideas",
                    )
                )

    return items


def scan_github_issues(project_path: str) -> list[DiscoveredItem]:
    """Fetch open GitHub issues via `gh` CLI. Returns [] on any failure."""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--json",
                "number,title,body,labels",
                "--limit",
                "50",
                "--state",
                "open",
            ],
            capture_output=True,
            timeout=10,
            cwd=project_path,
            text=True,
        )
        if result.returncode != 0:
            return []
        issues = json.loads(result.stdout)
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ):
        return []

    items = []
    for issue in issues:
        body = (issue.get("body") or "")[:500]
        items.append(
            DiscoveredItem(
                title=issue.get("title", "Untitled issue"),
                source_type="github_issue",
                source_file="github",
                source_line=issue.get("number", 0),
                raw_text=body,
                priority="medium",
                section="Ideas",
            )
        )
    return items


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

SCANNERS = {
    "md_task": scan_md_tasks,
    "readme_todo": scan_readme_todos,
    "code_todo": scan_code_todos,
    "changelog": scan_changelog_unreleased,
    "github_issue": scan_github_issues,
}


def discover(
    project_path: str, sources: list[str] | None = None
) -> list[DiscoveredItem]:
    """Run scanners and return combined results."""
    to_run = {k: v for k, v in SCANNERS.items() if sources is None or k in sources}
    results = []
    for scanner in to_run.values():
        try:
            results.extend(scanner(project_path))
        except Exception:
            pass  # Individual scanner failures don't block others
    return results


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _normalize(title: str) -> str:
    return title.strip().lower()


def _parse_source_from_desc(description: str) -> tuple[str, str, int] | None:
    """Extract (source_type, source_file, source_line) from 'Source: type @ file:line' prefix."""
    if not description:
        return None
    m = re.match(r"^Source:\s+(\S+)\s+@\s+(.+):(\d+)", description)
    if m:
        return (m.group(1), m.group(2), int(m.group(3)))
    return None


def deduplicate(
    items: list[DiscoveredItem],
    existing_titles: list[str],
    existing_draft_descriptions: list[str],
) -> list[DiscoveredItem]:
    """Remove items that match existing tickets or previous seek drafts."""
    existing_norm = {_normalize(t) for t in existing_titles}

    # Parse source keys from existing draft descriptions for source-level dedup
    existing_sources = set()
    for desc in existing_draft_descriptions:
        parsed = _parse_source_from_desc(desc)
        if parsed:
            existing_sources.add(parsed)

    seen_titles = set()
    result = []
    for item in items:
        norm = _normalize(item.title)
        source_key = (item.source_type, item.source_file, item.source_line)

        if norm in existing_norm:
            continue  # Matches existing real ticket
        if norm in seen_titles:
            continue  # Duplicate within this scan
        if source_key in existing_sources:
            continue  # Same source already has a draft

        seen_titles.add(norm)
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def ingest(
    conn: sqlite3.Connection, project_id: str, items: list[DiscoveredItem]
) -> list[str]:
    """Create draft tickets from discovered items. Returns list of new ticket IDs."""
    created = []
    for item in items:
        description = f"Source: {item.source_type} @ {item.source_file}:{item.source_line}\n\n{item.raw_text}"
        tid = add_ticket(
            conn,
            project_id,
            title=item.title[:200],
            section=item.section,
            priority=item.priority,
            description=description,
            draft=True,
            emit_created_event=False,
        )
        emit_event(
            conn,
            project_id,
            "ticket",
            tid,
            "ticket_created",
            {
                "origin": "seek",
                "draft": True,
                "source_type": item.source_type,
                "source_file": item.source_file,
                "source_line": item.source_line,
            },
            ActorContext.system(),
        )
        created.append(tid)
    conn.commit()
    return created


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def run_seek(
    conn: sqlite3.Connection,
    project_id: str,
    project_path: str,
    sources: list[str] | None = None,
) -> dict:
    """Discover ticket-like items and create drafts. Returns summary dict."""
    items = discover(project_path, sources=sources)

    # Get existing ticket titles for dedup
    rows = conn.execute(
        "SELECT title FROM tickets WHERE project_id = ?", (project_id,)
    ).fetchall()
    existing_titles = [r["title"] for r in rows]

    # Get existing draft descriptions for source-level dedup
    draft_rows = conn.execute(
        "SELECT description FROM tickets WHERE project_id = ? AND draft = 1",
        (project_id,),
    ).fetchall()
    existing_draft_descs = [r["description"] for r in draft_rows]

    unique = deduplicate(items, existing_titles, existing_draft_descs)
    created_ids = ingest(conn, project_id, unique) if unique else []

    return {
        "discovered": len(items),
        "created": len(created_ids),
        "skipped_duplicates": len(items) - len(unique),
        "tickets": created_ids,
    }
