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
UI (browser)  →  API (serve.py)  →  App Layer (actions.py)  →  DB (sqlite)
                                                              →  Markdown (output)
CLI (tickets-cli.py)  →  App Layer (same)  →  DB → Markdown

src/constants.py   — canonical STATUSES, VALID_STATUSES_BY_SECTION, compute_status_on_move()
src/db.py          — get_db(), init_db(), schema, migrations
src/actions.py     — move_ticket(), accept_ticket(), add_ticket(), update_ticket() + post-change hooks
src/tickets-cli.py — thin CLI wrapper calling actions.py
src/serve.py       — HTTP server routing to actions.py + background threads
src/generate.py    — dashboard HTML renderer
src/scenarios.py   — scenario manifest discovery, validation, gallery publishing
src/scenario_drafting.py — template-based draft generation from natural-language goals
src/journeys.py    — user journey CRUD, compilation to scenario manifests, inference engine
src/seek.py        — project file discovery engine (scanners, dedup, draft ingestion)
src/page_scraper.py — screen discovery for journey path builder
src/kitchen.py     — Kitchen orchestrator (poll/claim/dispatch + pause/resume) — see docs/KITCHEN.md
src/workspaces.py  — per-subject git worktree manager + lifecycle hooks (Kitchen)
src/runners.py     — Runner ABC + AgentRunner + ScenarioRunner + classify_scenario_failure
src/workflow_config.py — WORKFLOW.toml + PROMPT.md reader (Kitchen policy files)
src/evidence.py    — Kitchen evidence rotation pipeline (live → summarised → pruned)
```

**Kitchen** (M1a–M6, branch `feat/kitchen`): agentic work orchestrator layered on top of the kanban. Two paradigms — tickets (build something) and journeys (prove user can do something) — flow through one isolated-worktree execution layer with a closed loop (red journey → gap ticket → human triage → ticket Done → journey re-runs). See `docs/KITCHEN.md` for the full spec; key design rules below in Critical gotchas.

**Workflow Bounce** (I-19): Multi-agent prompt routing system. Users define agents (name + CLI command + system prompt) and workflows (ordered steps, each with an agent + optional step instructions). Applying a workflow to a ticket bounces its content through the agent sequence. Primary agent (step 1) mediates disagreements.

- **DB tables:** `workflow_agents`, `workflows`, `workflow_runs` (migration 4)
- **API:** `/api/workflow/agents`, `/api/workflow/workflows` (CRUD), `/api/workflow/workflows/{id}/duplicate` (clone any workflow — including disabled system rows — into a user-owned editable copy), `/api/tickets/{id}/workflow/run` (execution), `/api/workflow/runs/{id}` (status/polling), `/api/workflow/runs/active` (active runs across all tickets for kanban indicators)
- **CLI:** `tickets-cli.py agent list/add/update/remove`, `workflow list/add/add-step/remove-step/remove`
- **UI:** Agents and workflows are managed in a full-page "Workflows & Agents" view (zap icon in nav bar, `body.bounce-open`). Settings (Appearance, Feedbacks, Managed Files, Project metadata, Scenarios, Danger Zone) live in the right-hand drawer (gear icon). Ticket detail has workflow dropdown + Run button with instant placeholder and polling.
- **Execution:** Background thread per run, `subprocess.run(["claude", "-p", ...])` per step, `--no-session-persistence` flag, 120s timeout (`WORKFLOW_AGENT_TIMEOUT`). Progress conversation entries flushed to DB before each subprocess call. Return code checked; stderr surfaced on failure. Disagreement detection via primary agent evaluation after each step.
- **System workflows (`system=1`):** Seeded immutable defaults that ship enabled-or-disabled out of the box. Users can disable, can duplicate (creates an editable user-owned copy via `POST .../duplicate`), but cannot edit the system row in place — `PUT` with non-`enabled` fields and `DELETE` both return `403 system_workflow`. System workflows **bypass the `automation_mode='auto'` filter** in the dispatcher: they evaluate against ALL non-draft non-archived tickets in the catchment sections. User workflows (`system=0`) keep the legacy auto-mode-only behaviour.
- **Zero-step workflows (NoopRunner):** System workflows like `Parent auto-promote` and `Auto-accept reviewed tickets` ship with `steps: []` — pure mutation rules with no agent subprocess. The dispatcher routes these through `runners.NoopRunner`, which skips workspace creation and applies the workflow's `on_success_json` effects directly under `ActorContext.system()`.
- **`on_success` effects:** `move_section` / `move_to` (move target ticket), `set_status`, `add_tags`, `remove_tags`, `accept_ticket: true` (calls `actions.accept_ticket`; refuses silently unless target is in `For Review` with status `done`), and `apply_to: "self"|"parent"` (default `self`; when `parent`, all the above effects target the ticket's parent — used by parent-promote so the workflow runs on the parent itself but could be repurposed in user workflows).
- **Resilience:** `_recover_stuck_workflow_runs()` on server startup marks orphaned "running" records as "failed". GET run handler detects dead threads (DB says running, no thread in memory) and auto-fails them. Agreement check errors logged to conversation instead of silently swallowed.
- **Kanban indicators:** 3s poll to `/api/workflow/runs/active` shows pulsing `▶ workflow running` text on kanban cards while active. Static accent dot (`.card-wf-unread`) appears when run completes; cleared when user opens ticket detail overlay.
- **Validation:** `_normalize_json_array` and `_normalize_workflow_steps` reject invalid args, `_project_*` agent IDs, and missing agents.
- **API response format:** All workflow/journey list APIs return wrapped objects: `{"agents": [...]}`, `{"workflows": [...]}`, `{"runs": [...]}`. JS must always unwrap with `data.agents || data || []` — never iterate the response directly. This has caused bugs 5+ times.

**Source of truth:** `~/.claude/ticket-takeaway/tickets.db` (SQLite). All writes go through `actions.py`.

**Markdown sync:** DB → markdown is one-directional on every write. Draft tickets (`draft = 1`) are excluded from markdown output. External markdown edits (by LLM agents) are detected by a hash-based watcher thread (5s poll) and diff-imported into DB.

**Seek (project discovery):** `tickets-cli.py seek <project>` or `POST /api/seek` scans project files for ticket-like content (markdown tasks, README TODOs, code TODO/FIXME/HACK comments, CHANGELOG unreleased, GitHub Issues via `gh` CLI). Creates draft tickets in Ideas section. Deduplicates against existing tickets and previous drafts using title normalization + source-key matching. Empty boards show a CTA with Seek button as primary discovery point.

**Business rules:** Post-change hooks in `actions.py` fire after moves/status changes (journey cascade on move-to-Done, commit-hash capture). The legacy parent-auto-promote and 5-min auto-accept rules are now system workflows (`workflows_seed.py` → `Parent auto-promote` enabled, `Auto-accept reviewed tickets` disabled) — visible in the Workflows & Agents page and toggle-able. Scheduled events table + 30s poller stay in place dormant for future delayed-effect support.

**`src/tickets-cli.py`** is the CLI for all ticket CRUD. Subcommands: `seed`, `list`, `add`, `update`, `move`, `accept`, `sync`, `register`, `unregister`. Every write auto-syncs DB → PRODUCT_BACKLOG.md.

**`src/generate.py`** (~5100 lines, Python 3.10+, no external deps) is the dashboard renderer. It:
1. Reads `~/.claude/ticket-takeaway/registry.json` for project paths
2. Loads tickets from SQLite (falls back to parsing PRODUCT_BACKLOG.md if no DB)
3. Collects git/code stats via shell commands
4. Renders a self-contained HTML file with inline CSS/JS (light/dark/system theming)
5. Dashboard polls every 2s and does **in-place DOM diffing** (no full page reload) — moved cards get a glow indicator, new cards fade in, removed cards fade out, scroll/filter/expanded state preserved. **Polling is skipped when `body.bounce-open` is set** to prevent form/editor destruction.
6. **Cross-cutting filters** in the filter bar: Status (Proposed/In Progress/For Review), Type (Bug). Multi-select with OR within groups, AND between groups. Composes with text search. Cards carry `data-status`, `data-is-bug` attributes for filtering.

Data model: `Ticket` dataclass (id, title, priority, status, section, description, acceptance_criteria, parent, depends, summary, archived, commit_hash, release_tag, readiness_flags, readiness_content, tags) → `Project` dataclass (tickets + CodeStats) → HTML or JSON. `section` is the single term for kanban placement; `column` is derived from section (not stored). `rationale` field removed.

**Three-layer hierarchy** (see `docs/LIFECYCLE.md` Section 3b):
- **Section** = where the work is (Ideas → Backlog → WIP → Review → Done)
- **Status** (badge) = how the work is going (proposed, in-progress, blocked, etc.)
- **Readiness Flags** (D C S T L) = what's been done (Description, Criteria, Smoke, Tests, Learnings)

**`src/skills/`** contains Claude Code skill definitions:
- `dashboard/SKILL.md` — the `/dashboard` skill
- `review/SKILL.md` — the `/review` skill for acceptance workflow
- `feedbacks/SKILL.md` — the `/feedbacks` wrapper skill (superset of base feedbacks skill, adds ticket-takeaway context)

Source files in `src/` are canonical. They deploy to `~/.claude/` for runtime use (see `INSTALL.md` for the deployment map).

**`src/serve.py`** is the interactive dashboard server. Serves the generated HTML over HTTP, injecting an `edit-api` meta tag that activates editing features:
- **Priority cycling** (click the colored dot), **Status dropdown** (click badge)
- **Drag-to-move** (drag cards between columns), **Inline text editing** (dblclick title/description when expanded)
- **Workflow buttons** (Start, Done, Accept — shown when expanded), **Create/Delete** via API
- **New ticket panel** ("+ New" in filter bar) — quick-create with title input + section dropdown
- **Gate-check on column moves** — dragging/moving a ticket to a top column triggers an AI-powered readiness analysis (DCTRS flags), showing results in an expandable panel with per-section editable fields
- **Ticket detail overlay** — single scrollable card. Header: ID + contenteditable title + DCSTL readiness dots (click to scroll). Meta strip: priority/status chips (click to cycle/dropdown), parent (click to edit), section badge. Body: D C S T L sections stacked with inline auto-save on blur. "Assess"/"Re-assess" button per section (always visible at 40% opacity). Criteria/Tests/Smoke as individual list items (add/edit/delete per-item). Learnings as prose textarea. AI responses cached per-ticket. ↗ open button on kanban cards.
- **Undo/Redo** — Ctrl+Z undoes last edit, Ctrl+Shift+Z/Ctrl+Y redoes. Stack depth 50. Covers priority, status, criteria, text edits.
- **Keyboard shortcuts** — Ctrl+Enter saves textarea, Escape reverts without closing overlay.

Start: `python3 ~/.claude/ticket-takeaway/serve.py` (auto-detects project from cwd, port 8787)

**Multi-project support:** serve.py handles multiple projects simultaneously via project-scoped URL routing (`/{project-id}/api/...`). Root `/` serves a project picker page with a folder picker for adding projects (Browse button → server-side directory listing via `GET /api/browse`). Each project page has a **project switcher dropdown** in the header (replaces the static project name span). The server injects `projects-list` and `current-project` meta tags for the JS switcher. The legacy `/{project-id}/settings` route redirects (302) to the dashboard — all settings now live in the drawer. Background threads (markdown watcher, scheduled events) iterate all registered projects. CLI commands `register`/`unregister` manage the project registry.

**Project onboarding:** Registration auto-detects existing work. If `PRODUCT_BACKLOG.md` exists, `seed_project()` imports tickets into SQLite immediately. If not, `scaffold_project()` creates empty `PRODUCT_BACKLOG.md` (with section headers) and `PRODUCT_SPECIFICATION.md`. Both CLI (`register`) and API (`POST /api/projects`) paths call `regenerate_dashboard()` so the board loads immediately. Settings drawer has a "Managed Files" section (`GET /api/managed-files`) showing all files TT manages with existence indicators.

**Global route ordering:** Global routes (`/api/projects`, `/api/browse`, `/api/managed-files`) are handled BEFORE the `_LEGACY_PROJECT_ID` redirect. The legacy redirect sits just before the 404 fallback. New global endpoints must be added above the redirect block.

**Theming:** Light/dark/system theme via `<html data-theme="dark|light">`. Preference stored in `localStorage('tt-theme')`. Synchronous `<script>` in `<head>` prevents flash. Three-way toggle in settings drawer. Applied to dashboard, project picker, and settings pages. file:// mode uses system preference with localStorage fallback.

**Card layout:** Ticket ID always precedes the title on cards (`B-24 Draft ticket concept`). ID in accent mono, title in primary sans — visual separation without a separator character. The edit (↗) button is 14px with 0.6 opacity and hover background.

**Icons:** Inline SVG icons via `SVG_ICONS` dict + `_svg_icon(name, size, cls)` helper at module level. Lucide-style, `currentColor` stroke, consistent cross-platform. No external dependencies. Includes `mic` icon for record buttons.

**Toast system:** Single `showAppToast(message, type, duration, undoFn)` function replaces 3 prior implementations. Priority tiers: error/undo (high) cannot be displaced by success/copy (low). Undo toasts include clickable Undo button built via DOM methods. No native `alert()` or `confirm()` calls anywhere.

**Dialog patterns:** Reversible actions (attachment unlink) use inline confirm with undo. Destructive actions (draft delete, project remove) use custom modal. Rule: no inline confirm without working undo path.

**Progressive enhancement:** Same HTML works read-only via file://. Edit features only activate when `edit-api` meta tag present (injected by serve.py).

**Deployment:** Source files in `src/` deploy to `~/.claude/` for runtime use:
- `src/generate.py` → `~/.claude/ticket-takeaway/generate.py` AND `~/.claude/dashboard/generate.py`
- `src/tickets-cli.py` → `~/.claude/ticket-takeaway/tickets-cli.py`
- `src/serve.py` → `~/.claude/ticket-takeaway/serve.py`
- `src/constants.py` → `~/.claude/ticket-takeaway/constants.py`
- `src/db.py` → `~/.claude/ticket-takeaway/db.py`
- `src/actions.py` → `~/.claude/ticket-takeaway/actions.py`
- `src/journeys.py` → `~/.claude/ticket-takeaway/journeys.py`

**Ticket Tagging** (migration 6): Tags are stored in `ticket_tags` table (ticket_id, project_id, tag). Supports CLI (`--tag`, `--add-tag`, `--remove-tag`), API (`GET /api/tags`, add/remove/set on PATCH, `tags` array on POST), and dashboard UI (filter bar tag buttons, card tag pills, detail overlay add/remove, new ticket panel input). Tag logic flows through `actions.py` (`add_ticket(tags=...)`, `update_ticket(add_tags=..., remove_tags=...)`). Tags are round-tripped through markdown sync as `Tags: tag1, tag2` lines.

**GitHub Branch Awareness** (migration 7): Branches are stored in `ticket_branches` table (ticket_id, project_id, branch_name, remote, pr_number, pr_status, pr_url, ahead, behind, auto_linked, last_synced). Three-tier discovery: manual link, naming convention scan (`git branch -r` matches branches starting with ticket IDs), PR enrichment (`gh pr list`). Supports CLI (`branches list/link/unlink/scan`, `--add-branch`/`--remove-branch` on update), API (`GET /api/branches`, `GET /api/branches/overview`, `POST /api/branches/scan`, `add_branch`/`remove_branch` on PUT), and dashboard UI (branch pills on cards color-coded by PR status, detail overlay branch strip with link/unlink/scan, header "Branches" dropdown panel showing all remote branches with grouped tickets and inline add/remove). Branch logic flows through `actions.py` (`link_branch()`, `unlink_branch()`, `scan_branches()`, `scan_prs()`). Branches are round-tripped through markdown sync as `Branch: branch1, branch2` lines. `gh` failures are graceful — git branch data always works offline, PR metadata is cached from last successful scan.

**Deployment gotcha:** Source files in `src/` must be deployed to `~/.claude/ticket-takeaway/` for runtime use. The running `serve.py` and CLI read from the deployed copies, not from `src/`. After merging new features, always redeploy changed files (e.g., `cp src/actions.py ~/.claude/ticket-takeaway/actions.py`) and restart the server. Forgetting this step causes runtime errors where CLI/API references code that doesn't exist in the deployed copy.

**DB recovery:** If `tickets.db` is lost, run `tickets-cli.py seed` to reconstruct from PRODUCT_BACKLOG.md.

## Ticket Format in PRODUCT_BACKLOG.md

```markdown
### {ID}: {Title}
Priority: {priority} | Status: {status}
Parent: {parent-id}       (optional — for sub-tickets)
Depends: {id1}, {id2}     (optional — inter-ticket dependencies)
Tags: {tag1}, {tag2}      (optional — thematic/sprint tags, stored in ticket_tags table)
Commit: {hash}            (optional — git commit hash, auto-captured on done/accept)
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

# Tags — add when creating or update later
python3 $CLI add <project> "Title" --tag ux --tag onboarding
python3 $CLI update <project> <ID> --add-tag sprint-apr-25
python3 $CLI update <project> <ID> --remove-tag old-tag

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

The bottom sections (Bugs, Done, Icebox, Won't Do) render as **compact list rows** with the same visual tokens as kanban cards — same card background, borders, priority dots, status badges, readiness dots, and left-border color coding. Different density (horizontal layout, denser padding) but visual continuity with the active board above. Each row has a card-open-btn. Orphan bugs (no parent) appear in the Bug Backlog list; parented bugs only appear nested under their parent.

## Testing

Three-category test framework (`tests/`). Requires `pytest` and `playwright`.

```bash
python3 -m pytest tests/test_tdd_*.py -v      # TDD: pure logic, no server (instant)
python3 -m pytest tests/test_smoke_*.py -v     # Smoke: API + UI (needs serve.py)
python3 -m pytest tests/test_e2e_*.py -v       # E2E: full workflows (needs serve.py + browser)
python3 -m pytest tests/ -v                    # Everything
```

- **TDD tests** cover: status-on-move mappings, `auto_promote_parents()`, `resolve_section()`, `auto_generate_id()`, `compute_dependency_state()`
- **Smoke tests** cover: all API endpoints (tickets, settings, feedbacks status, attachments CRUD, record URL), all UI elements (filter bar, cards, detail overlay, settings drawer, attachment rows)
- **E2E tests** cover: ticket lifecycle journey, bug workflow + parent auto-promote, quick edit persistence
- `conftest.py` provides: `dashboard_server` (starts serve.py on free port, yields project-scoped URL `http://localhost:{port}/ticket-takeaway`), `browser`/`page` (Playwright with mocked gate-check), `live_page` (no mocks), shared API helpers

**Key testability note:** Business logic lives in `actions.py` (importable). Constants in `constants.py`. DB layer in `db.py`. All importable without side effects.

## User Journeys

First-class entity for defining, validating, and documenting user flows. Journeys compile to scenario manifests and execute via the existing scenario runner.

**Data model:** 5 tables (migration 5): `journeys`, `journey_steps`, `journey_runs`, `journey_step_results`, `journey_tickets`. Full relational — per-step results, run history.

**Module:** `src/journeys.py` — CRUD, compilation, inference, run result storage. Follows `actions.py` pattern (pure DB, no side effects).

**Dashboard UI:** `/{pid}/journeys` page with list view + unified timeline detail. Each journey has its own URL: `/{pid}/journeys/{journey-id}` (direct-linkable, pushState navigation). Rendered by `_render_journeys_page()` in `serve.py`.

**Timeline view** (unified — replaces separate Flow/Steps tabs):
- Vertical spine with status dots, screenshots on left, step details on right
- Steps grouped by URL — header row shows page URL when navigation changes
- Capture steps show thumbnails (click to lightbox), action steps show compact detail cards
- Human-readable descriptions on each card (e.g. "Click element: [data-testid=...]")
- Inline edit on pencil click — fields for label, action, value, key, target with help text placeholders
- "+ Add Step" button at bottom of timeline
- Failed steps: red border, error text; skipped steps: faded opacity
- Screenshots served from `.artifacts/journeys/{id}/{run-id}/` via API, backfilled to capture steps on run completion
- Journey IDs shown as monospace subtitles in list cards and detail header

**Two entry flows:**
1. **Tickets-first:** "Infer from Tickets" button analyzes existing tickets, groups by lifecycle stage, suggests journeys
2. **Journey-first:** "New Journey" → define steps manually → run for red/green validation

**Compilation:** `compile_to_manifest()` converts journey + steps from DB into a valid scenario manifest dict. Validated via `validate_manifest()`. Never persisted as a file.

**API routes** (all under `/{pid}/api/journeys/`):
- CRUD: GET/POST/PUT/DELETE for journeys and steps
- `POST .../validate` — compile + validate without executing
- `POST .../run` — compile + execute + store results
- `POST .../link` / `DELETE .../link/{tid}` — ticket linking
- `POST /api/journeys/infer` — generate suggestions from tickets

**Deployment:** `src/journeys.py` → `~/.claude/ticket-takeaway/journeys.py`

## Scenario Runner

Manifest-driven UI scenario execution with screenshot publishing. Scenarios are JSON files in `tests/scenarios/` that define multi-actor click paths with deterministic seed data and capture points.

```bash
# Run all scenarios
python3 -m pytest tests/test_scenarios.py -v

# Run one scenario
python3 -m pytest tests/test_scenarios.py -v --scenario-id full-tour-showcase

# Run and publish screenshots to gallery
python3 -m pytest tests/test_scenarios.py -v --publish

# Run a specific scenario and publish
python3 -m pytest tests/test_scenarios.py -v --scenario-id full-tour-showcase --publish

# Run against an already-running Chrome (CDP mode)
# First start Chrome with: google-chrome --remote-debugging-port=9222
python3 -m pytest tests/test_scenarios.py -v --backend=cdp
python3 -m pytest tests/test_scenarios.py -v --backend=cdp --cdp-endpoint=http://localhost:9333
```

**Architecture:**
- `tests/scenarios/*.json` — checked-in scenario manifests (schema: id, title, tags, actors, seed, steps, optional theme/viewport)
- `tests/scenario_backend.py` — Backend protocol + PlaywrightBackend (launched) + CDPBackend (connect_over_cdp). Target resolution lives here.
- `tests/scenario_runner.py` — Execution engine: dispatches action steps through the Backend interface (12 actions: open, reload, click, double_click, fill, select, press, wait_for, assert_visible, assert_text, capture)
- `tests/scenario_seed.py` — deterministic ticket seeding via API + cleanup
- `tests/test_scenarios.py` — pytest parametrized entrypoint with `--scenario-id` and `--publish` options
- `src/scenarios.py` — manifest discovery, validation, gallery publishing
- `src/scenario_drafting.py` — template-based draft generation from natural-language goals (7 intents: create, edit, move, review, lifecycle, delete, overview)

**Artifacts:**
- `.artifacts/scenarios/{run-id}/` — raw run output (gitignored): manifest.json, summary.json, screenshots/
- `docs/scenarios/gallery/` — published stable screenshots (tracked): `{publish_slot}.png` + `index.json`

**Theme support:** Manifests can specify `"theme": "dark"` or `"theme": "light"` to force a theme via localStorage before captures.

**Settings page integration:** `GET /api/scenarios` lists manifests, `POST /api/scenarios/run` launches via subprocess, `GET /api/scenarios/runs/{id}` polls status. Draft endpoint: `POST /api/scenarios/draft` generates candidate manifests from a goal string.

**Backend toggle:** Journey detail view has a PW/CDP dropdown next to the Run button. Sends `{backend: "playwright"|"cdp"}` in the POST body. Server appends `--backend={value}` to the pytest subprocess. Same manifests work with both backends — no duplication needed. `summary.json` records which backend was used.

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

**Dashboard integration:** Settings drawer (gear icon) has a Feedbacks Integration section:
- Enable toggle — starts/stops the feedbacks whisper server. Disabled until installed.
- Auto-start recording toggle — appends `&autostart=1` to recorder URL.
- Path input — `feedbacks.home` setting, grayed when disabled.
- Status dot — green (server running), yellow (installed, not running), neutral (disabled), red (not installed).
- Install/Re-install button — clones feedbacks repo, saves path to settings.

**Record flow:** Record button appears in card meta row (mic icon) and ticket detail header. Clicking opens feedbacks recorder in a 550x420 popup. A pulsing placeholder row appears in the attachments list ("Recording in progress..." → "Processing session..."). The file watcher detects new sessions automatically.

**Session watcher:** Background thread in serve.py polls the feedbacks output directory every 3s for new `feedbacks-*` directories with `meta.json`. Reads `ticketId` from meta, matches to a ticket, creates an attachment record. Seeds known sessions on startup to avoid re-importing.

**Attachments API:** `GET /api/tickets/{id}/attachments` enriches feedbacks-type attachments with `player_url` and `thumbnail_url` pointing to the feedbacks server. Play button opens `player.html` in a new tab.

**Settings keys:** `feedbacks.enabled`, `feedbacks.home`, `feedbacks.autostart` — stored in the `settings` table as string values ("true"/"false"). Detection reads `feedbacks.home` for install path, `feedbacks.enabled` for toggle state.

**Security:** Install endpoint validates `install_dir` within home directory and `repo_url` against GitHub/GitLab HTTPS allowlist.

## Critical gotchas

- **Kitchen orchestrator is paused by default on every fresh server boot.** Eligible tickets will NOT auto-dispatch until the user clicks Resume in the Kitchen view (or POSTs `/api/kitchen/resume`). Persistence: choice is stored as `settings.kitchen.paused`. Manual "Run now" buttons bypass the pause — clicking one IS the explicit OK for that single ticket. If a future agent finds themselves debugging "why didn't my eligible ticket run", check `kitchen.is_paused()` first.

- **Next migration must be #9, not #6 or #7.** `origin/main` shipped migrations 6 (`ticket_branches`) and 7 (`ticket_tags`). The Kitchen branch shipped migration 8 (`automation_subjects`, `runs`, `activity_events`). Sequence: `1, 2, 3, 4, 5, 6 (branches), 7 (tags), 8 (Kitchen)`. The `_migrations` table is a set of versions, not a strict sequence — gaps are harmless — but never reuse a number.

- **macOS `socket.getfqdn(127.0.0.1)` hangs `serve.py` startup for 30s+.** Symptom: `Serving N project(s)` prints, then nothing (the `ThreadingHTTPServer.__init__` never returns). Fix: a 6-line wrapper that monkey-patches `HTTPServer.server_bind` to skip the reverse-DNS lookup before importing `serve.py`. Reference at `/tmp/start-kitchen-demo.py`. Doesn't affect WSL/Linux. Worth pulling into `serve.py` itself if it bites again.

- **Cross-machine DB writes go through WSL.** `~/.claude/ticket-takeaway/tickets.db` and `~/.claude/ticket-takeaway/registry.json` are anchored to WSL where the production server runs. macOS sessions can read them but should not run `tickets-cli.py` writes or modify the registry — those go through WSL. Code edits + git push happen anywhere; CLI/dashboard writes belong on WSL.

- **Same-transaction event emission is the M1a/M1b audit invariant.** Every mutation in `actions.py` (and the routed-through helpers in `serve.py`) MUST call `emit_event()` inside the same `conn` transaction as the SQL write. The wrapper `db_session()` in `runners.py` enforces this for runner code; ad-hoc helpers must do it explicitly. If you split mutation and event into two transactions, rollback can leave the audit log disagreeing with state.
