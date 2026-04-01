# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Ticket Takeaway is an SQLite-backed project board system. It stores tickets in a SQLite database, auto-generates `PRODUCT_BACKLOG.md` from the DB, and renders an interactive HTML kanban dashboard. All writes go through `tickets-cli.py`.

## Key Commands

```bash
# Ticket CLI (all writes go through here)
python3 ~/.claude/ticket-takeaway/tickets-cli.py list --project ticket-takeaway
python3 ~/.claude/ticket-takeaway/tickets-cli.py add ticket-takeaway "New feature"
python3 ~/.claude/ticket-takeaway/tickets-cli.py move ticket-takeaway B-01 wip
python3 ~/.claude/ticket-takeaway/tickets-cli.py update ticket-takeaway B-01 --status blocked
python3 ~/.claude/ticket-takeaway/tickets-cli.py accept ticket-takeaway B-01
python3 ~/.claude/ticket-takeaway/tickets-cli.py seed    # Rebuild DB from markdown
python3 ~/.claude/ticket-takeaway/tickets-cli.py sync    # Regenerate markdown from DB

# Generate the dashboard (opens in browser)
python3 ~/.claude/ticket-takeaway/generate.py

# Or via Claude Code skill
/dashboard

# JSON output (for programmatic agent queries)
python3 ~/.claude/ticket-takeaway/generate.py --json
```

## Architecture

```
tickets.db (SQLite)  ──→  tickets-cli.py  ──→  PRODUCT_BACKLOG.md (auto-generated)
                     ──→  generate.py     ──→  docs/sdlc-dashboard.html
PRODUCT_SPECIFICATION.md (write-only output from /accept, not read by generator)
```

**Data flow is one-directional:** DB → markdown → dashboard. The generator does NOT merge tickets from PRODUCT_SPECIFICATION.md — all tickets must exist in the DB. The `ingest_markdown` step in sync absorbs direct markdown edits into the DB but never deletes DB-only records.

**Source of truth:** `~/.claude/ticket-takeaway/tickets.db` (SQLite). All writes go through `tickets-cli.py`.

**`src/tickets-cli.py`** is the CLI for all ticket CRUD. Subcommands: `seed`, `list`, `add`, `update`, `move`, `accept`, `sync`. Every write auto-syncs DB → PRODUCT_BACKLOG.md.

**`src/generate.py`** (~3000 lines, Python 3.10+, no external deps) is the dashboard renderer. It:
1. Reads `~/.claude/ticket-takeaway/registry.json` for project paths
2. Loads tickets from SQLite (falls back to parsing PRODUCT_BACKLOG.md if no DB)
3. Collects git/code stats via shell commands
4. Renders a self-contained HTML file with inline CSS/JS (dark theme kanban)
5. Dashboard polls every 2s and does **in-place DOM diffing** (no full page reload) — moved cards get a glow indicator, new cards fade in, removed cards fade out, scroll/filter/expanded state preserved
6. **Cross-cutting filters** in the filter bar: Status (Proposed/In Progress/For Review), Type (Bug), Size (S/M/L). Multi-select with OR within groups, AND between groups. Composes with text search. Cards carry `data-status`, `data-complexity`, `data-is-bug` attributes for filtering.

Data model: `Ticket` dataclass (id, title, priority, complexity, status, section, column, description, acceptance_criteria, parent, rationale, depends, summary, archived, readiness_flags, readiness_content) → `Project` dataclass (tickets + CodeStats) → HTML or JSON.

**Three-layer hierarchy** (see `docs/LIFECYCLE.md` Section 3b):
- **Section** (column) = where the work is (Ideas → Backlog → WIP → Review → Done)
- **Status** (badge) = how the work is going (proposed, in-progress, blocked, etc.)
- **Readiness Flags** (D C T R S) = what's been done (Description, Criteria, Tests, Reviewed, Smoke)

**`src/skills/`** contains Claude Code skill definitions:
- `dashboard/SKILL.md` — the `/dashboard` skill
- `review/SKILL.md` — the `/review` skill for acceptance workflow
- `feedbacks/SKILL.md` — the `/feedbacks` wrapper skill (superset of base feedbacks skill, adds ticket-takeaway context)

Source files in `src/` are canonical. They deploy to `~/.claude/` for runtime use (see `INSTALL.md` for the deployment map).

**`src/serve.py`** is the interactive dashboard server. Serves the generated HTML over HTTP, injecting an `edit-api` meta tag that activates editing features:
- **Priority cycling** (click the colored dot), **Status dropdown** (click badge)
- **Drag-to-move** (drag cards between columns), **Inline text editing** (dblclick title/description when expanded)
- **Workflow buttons** (Start, Done, Accept — shown when expanded), **Create/Delete** via API
- **New ticket panel** ("+ New" in filter bar) — overlay panel with title input, section dropdown, and expandable "Full ticket form" (coming soon placeholder)
- **Gate-check on column moves** — dragging/moving a ticket to a top column triggers an AI-powered readiness analysis (DCTRS flags), showing results in an expandable panel with per-section editable fields
- **Ticket detail overlay** — single scrollable card (no tabs). Header: ID + contenteditable title + DCTRS readiness dots (click to scroll). Meta strip: priority/status/complexity chips (click to cycle/dropdown), parent (click to edit), column badge. Body: all DCTRS sections + rationale stacked and always visible with inline auto-save on blur. Single "Assess"/"Re-assess" ghost button per section (visible on hover). Criteria: bullet + editable text + × delete, Enter-to-add input.

Start: `python3 ~/.claude/ticket-takeaway/serve.py` (auto-detects project from cwd, port 8787)

**Progressive enhancement:** Same HTML works read-only via file://. Edit features only activate when `edit-api` meta tag present (injected by serve.py).

**Deployment:** Source files in `src/` deploy to `~/.claude/` for runtime use:
- `src/generate.py` → `~/.claude/ticket-takeaway/generate.py` AND `~/.claude/dashboard/generate.py` (fix DASHBOARD_DIR line 25)
- `src/tickets-cli.py` → `~/.claude/ticket-takeaway/tickets-cli.py`
- `src/serve.py` → `~/.claude/ticket-takeaway/serve.py`

**DB recovery:** If `tickets.db` is lost, run `tickets-cli.py seed` to reconstruct from PRODUCT_BACKLOG.md.

## Ticket Format in PRODUCT_BACKLOG.md

```markdown
### {ID}: {Title}
Priority: {priority} | Complexity: {complexity} | Status: {status}
Parent: {parent-id}       (optional — for sub-tickets)
Rationale: {reason}       (optional — captures "why" decisions)
Depends: {id1}, {id2}     (optional — inter-ticket dependencies)
{Description}
- [ ] Acceptance criterion
- [x] Completed criterion
Tests: {test notes}       (optional — readiness flag content)
Reviewed: {review notes}  (optional — readiness flag content)
Smoke: {smoke notes}      (optional — readiness flag content)
```

ID prefixes: `B-` (backlog), `R-` (released), `I-` (idea), `W-` (won't do), `Z-` (icebox), `BUG-` (bug).

Sections (`## WIP`, `## Backlog`, `## Ideas`, etc.) map directly to dashboard columns.

## Ticket Operations — Use the CLI

`PRODUCT_BACKLOG.md` is generated from the SQLite database. Ticket sections (`## WIP`, `## Backlog`, etc.) are overwritten on each sync, but the preamble and any custom sections you add are preserved.

**Use `tickets-cli.py` for all ticket changes** (adding, moving, updating status, criteria, etc.):

```bash
CLI=~/.claude/ticket-takeaway/tickets-cli.py

# Move tickets between sections (valid targets: wip, review, backlog, ideas, bugs, icebox, done, wontdo)
python3 $CLI move <project> <ID> wip        # Start work
python3 $CLI move <project> <ID> review     # Code complete
python3 $CLI move <project> <ID> icebox     # Shelve for later
python3 $CLI move <project> <ID> wontdo     # Won't do
python3 $CLI move <project> <ID> done       # Mark done (use /accept for full acceptance flow)

# Update status within a section
python3 $CLI update <project> <ID> --status blocked
python3 $CLI update <project> <ID> --status rework

# Accept a feature (moves to Done + appends to PRODUCT_SPECIFICATION.md)
python3 $CLI accept <project> <ID>

# Add tickets
python3 $CLI add <project> "Title" --section ideas
python3 $CLI add <project> "Title" --section backlog --priority high
python3 $CLI add <project> "Bug description" --section bugs --parent <parent-ID>

# Update description, criteria, or metadata
python3 $CLI update <project> <ID> --description "..." --add-criteria "..."

# Watch for live dashboard updates (detects direct markdown edits too)
python3 $CLI watch &
```

Every CLI write auto-syncs the DB back to PRODUCT_BACKLOG.md. The sync preserves the file's preamble and any custom `##` sections not managed by the DB — only the ticket sections are regenerated.

## Parent-Child Ticket Behavior

Bug sub-tickets with a `Parent: {ID}` field are **never shown as standalone cards**. They are:
1. Filtered out of all column lists (WIP, Backlog, Review, Bugs, Done, etc.)
2. Rendered as clickable boxes nested inside their parent card (visible on expand)
3. Each linked bug has its own double-click → clipboard prompt

**Auto-promotion:** If all child bugs of a parent have status `for-review`, `bug-fixed`, or `done`, the parent card automatically moves to the For Review column (keeping its original status badge like `rework`).

## Bottom List Sections

The bottom sections (Bugs, Done, Icebox, Won't Do) render as **compact list rows** instead of full kanban cards. Same click/dblclick behavior, different visual style. Orphan bugs (no parent) appear in the Bug Backlog list; parented bugs only appear nested under their parent.

## Generated Files

- `PRODUCT_BACKLOG.md` — ticket sections are regenerated from the SQLite DB by `tickets-cli.py sync`. Preamble and custom sections are preserved.
- `docs/sdlc-dashboard.html` — regenerated by `generate.py`, gitignored
- `docs/features/*/` — ephemeral per-feature working files, gitignored

## Feedbacks Integration (Optional)

Ticket Takeaway integrates with [feedbacks](https://github.com/ytubecoder/feedbacks) — a browser-based screen recording + voice annotation tool for visual UI feedback. **Feedbacks is optional; ticket-takeaway works fully without it.**

**Architecture:** One-way dependency — ticket-takeaway depends on feedbacks, never the reverse. Feedbacks is a standalone capture tool; ticket-takeaway is the orchestrator that layers SDLC context on top.

**Skills:** Both repos ship a skill to `~/.claude/skills/feedbacks/`:
- **feedbacks repo** ships the base skill (`skills/feedbacks/SKILL.md`) — setup, start, analyze
- **ticket-takeaway repo** ships a wrapper skill (`src/skills/feedbacks/SKILL.md`) — superset of base, adds ticket-linked output dirs and context push after analysis
- If both installed, ticket-takeaway's wrapper overwrites the base (it's a superset)

**Two use cases:**

1. **Review stage** (`/review`):
   - Step 4a: checks `.feedbacks/{ticket-id}/` for prior sessions, auto-analyzes latest as review context
   - Step 1b: when giving feedback, offers `/feedbacks start {ticket-id}` for visual capture — saves directly to `.feedbacks/{ticket-id}/`

2. **General feedback** (`/feedbacks` standalone):
   - Sessions save to feedbacks' default location (`~/projects/feedbacks/sessions/`)
   - After analysis, session path + summary are pushed into agent context (informational only)
   - Agent decides what to do — create ticket, link to existing, or nothing

**Session format:** Each session in `.feedbacks/{ticket-id}/feedbacks-{timestamp}/` contains:
- `session.md` (timestamped transcript + screenshot refs), `player.html` (playback), `images/` (screenshots with cursor), `meta.json`, `summary.json`

**Detection:** Skills check for `~/projects/feedbacks/start.sh` — if absent, all feedbacks-related steps are silently skipped.
