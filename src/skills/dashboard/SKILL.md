---
name: dashboard
description: Ticket Takeaway — track features from ideation to release. Generate visual dashboard, update item status in real-time, review completed features, add new items. Cross-project.
user_invocable: true
---

# Dashboard — Ticket Takeaway Feature Tracker

Track features across all projects from ideation through release. Generates a self-contained dark-theme HTML kanban dashboard.

**Architecture:** `SQLite DB` -> `tickets-cli.py` -> `PRODUCT_BACKLOG.md` + `HTML dashboard`

- **SQLite DB** at `~/.claude/ticket-takeaway/tickets.db` is the **source of truth** for all active work.
- **`tickets-cli.py`** is the CLI for all CRUD operations. Every write auto-syncs DB -> PRODUCT_BACKLOG.md.
- `PRODUCT_BACKLOG.md` is a **derived artifact** — auto-generated from the DB. Do NOT edit it directly.
- Features stay in the DB until **accepted/shipped**, then they get moved to `PRODUCT_SPECIFICATION.md` as the permanent record.
- HTML dashboard reads from the DB directly (with markdown fallback).

**File layout:**
```
~/.claude/ticket-takeaway/
  tickets.db                       # SQLite database (source of truth)
  tickets-cli.py                   # CLI for all ticket operations
  registry.json                    # Which projects to track
  generate.py                      # Dashboard generator script
{project}/PRODUCT_BACKLOG.md       # Auto-generated from DB (do not edit directly)
{project}/PRODUCT_SPECIFICATION.md # Shipped features (permanent record, plain markdown)
{project}/docs/
  sdlc-dashboard.html             # Generated output (open in browser)
```

**Feature lifecycle:**
```
PRODUCT_BACKLOG.md                              PRODUCT_SPECIFICATION.md
  Ideas -> Backlog -> WIP -> For Review  -->  accepted, moved here permanently
```

---

## CLI Quick Reference

All ticket changes go through `tickets-cli.py`. The CLI shorthand used below:
```
CLI=~/.claude/ticket-takeaway/tickets-cli.py
```

### `move` vs `update` — IMPORTANT DISTINCTION

**`move`** changes the **section** (column on the board) AND sets the default status for that section. Use this for all section transitions.

**`update --status`** changes ONLY the **status label** within the current section. Use this for fine-grained status changes where the ticket stays in the same column (e.g., `in-progress` -> `blocked` within WIP, or `proposed` -> `specified` within Backlog).

### Common workflows with examples

**Start work on a ticket:**
```bash
python3 $CLI move goodform B-05 wip
# B-05 -> WIP section, status = in-progress
```

**Code complete — move to review:**
```bash
python3 $CLI move goodform B-05 review
# B-05 -> For Review section, status = for-review
```

**Accept a feature (moves to Done + appends to PRODUCT_SPECIFICATION.md):**
```bash
python3 $CLI accept goodform B-05
# B-05 -> Done section, status = done, summary appended to PRODUCT_SPECIFICATION.md
```

**Add a new ticket:**
```bash
python3 $CLI add goodform "New feature idea" --section ideas
python3 $CLI add goodform "Ready to build" --section backlog --priority high
python3 $CLI add goodform "Epic ticket" --section backlog --container
```

**Check/uncheck acceptance criteria:**
```bash
python3 $CLI update goodform B-05 --check-criteria 1    # Check the 1st criterion
python3 $CLI update goodform B-05 --uncheck-criteria 2  # Uncheck the 2nd criterion
python3 $CLI update goodform B-05 --remove-criteria 3   # Remove the 3rd criterion
```

**Manage criteria directly (criteria subcommand):**
```bash
python3 $CLI criteria goodform list B-05
python3 $CLI criteria goodform add B-05 "Users can filter by date range"
python3 $CLI criteria goodform check B-05 1
python3 $CLI criteria goodform uncheck B-05 1
python3 $CLI criteria goodform remove B-05 2
```

**List tickets:**
```bash
python3 $CLI list --project goodform                  # All tickets
python3 $CLI list --project goodform --section wip    # WIP only
```

### Valid section targets for `move`

| Target | Section | Default status |
|--------|---------|---------------|
| `wip` | WIP | in-progress |
| `review` | For Review | for-review |
| `backlog` | Backlog | proposed |
| `ideas` | Ideas | proposed |
| `bugs` | Bugs | bug |
| `icebox` | Icebox | icebox |
| `done` | Done | done |
| `wontdo` | Won't Do | wontdo |

---

## Container Tickets (Epic Flag)

A **container ticket** is an ordinary ticket with `is_container = 1`. It is not a different ticket type — it is a cosmetic + behavioral flag that changes how the ticket renders.

**What changes when a ticket is flagged as container:**
- Card renders without description preview; leads with a child progress pill (`5/8 done`) and a child status sparkline
- A "Container" badge appears next to the ticket ID on the card
- Full-page view (`/{project}/tickets/{id}`) leads with the children panel; description is collapsed by default; criteria is treated as parent-level / cross-cutting

**How to flag:**
```bash
# At creation
python3 $CLI add goodform "Auth Overhaul" --section backlog --container

# Flip on existing ticket
python3 $CLI update goodform B-12 --container

# Clear the flag
python3 $CLI update goodform B-12 --no-container
```

Via API:
```bash
curl -X PATCH http://localhost:8787/goodform/api/tickets/B-12 \
  -H 'Content-Type: application/json' \
  -d '{"is_container": true}'
```

Via the ticket detail UI: toggle in the full-page ticket view header area.

Adding an `epic` tag is also recommended alongside the flag — the tag groups related tickets in the filter bar while the flag drives the visual treatment:
```bash
python3 $CLI update goodform B-12 --add-tag epic
```

---

## Full-Page Ticket View

The canonical detail view for a ticket is `/{project}/tickets/{id}` (full page, not the floating overlay). The kanban card's open button (arrow icon) navigates here. The floating overlay remains as a quick-peek for in-flow edits from the kanban.

**URL-routed tabs:**
- `/{project}/tickets/{id}?tab=overview` — gate banner, criteria panel, description, children (if container), parent/deps/tags
- `/{project}/tickets/{id}?tab=activity` — activity event timeline for this ticket
- `/{project}/tickets/{id}?tab=runs` — list of past + active runs; click a run for terminal-style detail (stdout/stderr, handoff blob, evidence files, exit code, duration)
- `/{project}/tickets/{id}?tab=files` — attachments and linked files
- `/{project}/tickets/{id}?tab=graph` — dependency graph (placeholder)

**Gate banner (Overview tab, just above criteria):**

| Section | Banner text |
|---------|------------|
| Ideas | "Add a description and at least one criterion to auto-move to Backlog." |
| Backlog | "Resolve dependencies to auto-move to WIP." |
| WIP | "Land a commit to auto-move to For Review." |
| For Review | "All criteria checked + no open bugs to auto-accept." |

The banner is informational — it tells the user what to do next, not what is blocked.

---

## Criteria as the Central Gate

Acceptance criteria are promoted above the description in the full-page view. Each criterion has:
- Check/uncheck toggle
- "Ask AI" button — prompts the project's default agent to help fulfill the criterion (uses the freeform workflow step pattern; no new feature)

On kanban cards, criteria render as an `X/Y` pill (checked/total). The pill is grey when no criteria, accent-colored when some checked, green when all checked.

**To set the default agent for a project:**
```bash
python3 $CLI agent set-default <agent_id> --project <project_id>
# Stored as setting key: {project_id}.agent.default
```

---

## Activity Feed

The **Activity** tab in the full-page ticket view shows a vertical timeline of `activity_events` for this ticket. Polls every 5 seconds while the tab is focused.

- Each event row: icon by `event_kind`, relative timestamp, actor (human / agent name / system), one-line summary
- Run-related events link to the corresponding run in the **Runs** tab
- Events are read via `GET /{project}/api/tickets/{id}/activity?limit=50&before={ISO}` — response wrapped as `{"events": [...]}`, unwrap before iterating
- Only **outcomes** appear in the timeline: `criteria_added`, `description_updated`, `tags_added`, `run_succeeded`, `section_change`, `status_change`, `criteria_check`, `run_started`
- The orchestrator's interview chat transcript is NOT in the activity feed — it lives in the Runs tab -> run detail (Runs tab -> click the run -> chat history visible)

---

## Mode Detection

| Invocation | Mode |
|---|---|
| `/dashboard` (no args) | **generate** |
| `/dashboard generate` | **generate** |
| `/dashboard status <project> <item-id> <new-section>` | **status** — move item between sections |
| `/accept <item-id>` | **accept** — now a separate skill, see `/accept` |
| `/dashboard add <project> "<title>" [--section S] [--priority P]` | **add** — add new entry |
| `/dashboard show [project]` | **show** — terminal summary |

---

## Mode 1: generate (default)

```bash
python3 ~/.claude/ticket-takeaway/generate.py
```

Parses SQLite DB for each registered project, generates the HTML dashboard, opens in browser. For programmatic/agent queries:
```bash
python3 ~/.claude/ticket-takeaway/generate.py --json
```

---

## Mode 2: status <project> <item-id> <new-section>

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py move <project> <item-id> <new-section>
```

Updates DB, sets default status, syncs PRODUCT_BACKLOG.md, regenerates HTML. Browser auto-refreshes within 2 seconds.

---

## Mode 3: add <project> "<title>" [options]

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py add <project> "<title>" [--section S] [--priority P] [--parent ID] [--description D] [--container]
```

Auto-generates the ID (B- for backlog, I- for ideas, BUG- for bugs, etc.).

---

## Mode 4: show [project]

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py list [--project <project>] [--section S] [--status S]
```

---

## Rules

- **Use `move` to change sections, `update --status` to change status within a section.**
- **SQLite DB is the source of truth** — `~/.claude/ticket-takeaway/tickets.db`. All writes go through `tickets-cli.py`.
- **PRODUCT_BACKLOG.md is auto-generated** from the DB — do not edit directly.
- **The dashboard auto-refreshes** — CLI regenerates HTML after every write; browser polls every 2 seconds.
- **Case-insensitive ID matching** — `b-01` matches `B-01`
- **Acceptance = move to PRODUCT_SPECIFICATION.md** — use `tickets-cli.py accept` for the full flow
- **Activity feed unwrap:** response from `/api/tickets/{id}/activity` is `{"events": [...]}` — always unwrap with `data.events || data || []`
- **If DB doesn't exist**, run `python3 ~/.claude/ticket-takeaway/tickets-cli.py seed` to create it from existing markdown
