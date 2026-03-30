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
PRODUCT_SPECIFICATION.md (plain markdown, not in DB)
```

**Source of truth:** `~/.claude/ticket-takeaway/tickets.db` (SQLite). All writes go through `tickets-cli.py`.

**`src/tickets-cli.py`** is the CLI for all ticket CRUD. Subcommands: `seed`, `list`, `add`, `update`, `move`, `accept`, `sync`. Every write auto-syncs DB → PRODUCT_BACKLOG.md.

**`src/generate.py`** (~1600 lines, Python 3.10+, no external deps) is the dashboard renderer. It:
1. Reads `~/.claude/ticket-takeaway/registry.json` for project paths
2. Loads tickets from SQLite (falls back to parsing PRODUCT_BACKLOG.md if no DB)
3. Parses `PRODUCT_SPECIFICATION.md` for accepted features
4. Collects git/code stats via shell commands
5. Renders a self-contained HTML file with inline CSS/JS (dark theme kanban)

Data model: `Ticket` dataclass (id, title, priority, complexity, status, section, column, description, acceptance_criteria, parent, rationale, depends, summary, archived) → `Project` dataclass (tickets + CodeStats) → HTML or JSON.

**`src/skills/`** contains Claude Code skill definitions:
- `dashboard/SKILL.md` — the `/dashboard` skill
- `review/SKILL.md` — the `/review` skill for acceptance workflow

Source files in `src/` are canonical. They deploy to `~/.claude/` for runtime use (see `INSTALL.md` for the deployment map).

**Deployment:** Source files in `src/` deploy to `~/.claude/` for runtime use:
- `src/generate.py` → `~/.claude/ticket-takeaway/generate.py` AND `~/.claude/dashboard/generate.py` (fix DASHBOARD_DIR line 25)
- `src/tickets-cli.py` → `~/.claude/ticket-takeaway/tickets-cli.py`

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

Ticket Takeaway integrates with [feedbacks](https://github.com/user/feedbacks) — a browser-based screen recording + voice annotation tool for visual UI feedback. **Feedbacks is optional; ticket-takeaway works fully without it.**

**How it works:**
- During `/review`, if `.feedbacks/{ticket-id}/` exists with prior sessions, they are analyzed as additional review context
- When giving feedback on a ticket, if feedbacks is installed (`~/projects/feedbacks/`), the reviewer is offered visual capture via `/feedbacks start`
- Feedbacks sessions save to `.feedbacks/{ticket-id}/feedbacks-{timestamp}/` in the project root (gitignored)
- Each session contains `session.md` (annotated transcript), `player.html` (playback), and `images/` (annotated screenshots)

**Detection:** The `/review` skill checks for `~/projects/feedbacks/start.sh` — if absent, all feedbacks-related steps are silently skipped.
