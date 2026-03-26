# Ticket Takeaway

```
                         _______________________________________________
                        /                                              /|
                       /  TICKET TAKEAWAY                             / |
                      /    ═══════════════                           /  |
                     /     ☐ grab  ☐ paste  ☐ build  ☐ ship        /   |
                    /_______________________________________________/    |
                    |                                               |    |
     ___            |  ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐     |    |
    /   \    .--.   |  │ IDEAS │ │BACKLOG│ │  WIP  │ │REVIEW │     |   /
   | o o |  |    |  |  │ ░░░░  │ │ ░░░░░ │ │ ▓▓▓▓▓ │ │ ████  │     |  /
    \ _ /   |    |  |  │ ░░    │ │ ░░░   │ │ ▓▓▓   │ │       │     | /
     |||    '----'  |  └───────┘ └───────┘ └───────┘ └───────┘     |/
    /   \           |_______________________________________________|
```

> Markdown-native project board. Double-click. Paste. Build.

Your project board lives in `PRODUCT_BACKLOG.md`. Ticket Takeaway renders it as a kanban dashboard — no database, no JSON intermediary. Double-click any ticket to copy a ready-made prompt, paste it into Claude Code, and take it away.

## Project Structure

```
ticket-takeaway/
├── README.md                          # This file — quick reference for all constructs
├── INSTALL.md                         # How to deploy the system to a new machine
├── PRODUCT_BACKLOG.md                 # This project's own backlog (dogfooding)
├── PRODUCT_SPECIFICATION.md           # This project's accepted features
├── src/
│   ├── generate.py                    # Dashboard HTML generator (Python)
│   ├── registry.example.json          # Example project registry
│   └── skills/
│       ├── dashboard/SKILL.md         # /dashboard skill for Claude Code
│       └── review/SKILL.md            # /review skill for Claude Code
├── docs/
│   ├── LIFECYCLE.md                   # Authoritative lifecycle spec (statuses, transitions, working files)
│   ├── REVIEW_PROCESS.md             # Review process spec (batching, feedback, acceptance)
│   ├── sdlc-dashboard.html           # Generated dashboard (do not edit)
│   ├── sdlc-dashboard-spec.md        # Original dashboard design spec (historical)
│   └── sdlc-methodology.md           # SDLC methodology reference (historical)
```

**Key docs:**
- [`INSTALL.md`](INSTALL.md) — Deploy to a new machine
- [`docs/LIFECYCLE.md`](docs/LIFECYCLE.md) — Complete lifecycle spec (statuses, transitions, working files, acceptance flow)
- [`docs/REVIEW_PROCESS.md`](docs/REVIEW_PROCESS.md) — Review process (batching, feedback→bugs, test criteria, acceptance)

## Getting Started

### Prerequisites

- Python 3.10+
- [Claude Code](https://claude.ai/code) CLI installed
- Git (for code stats in the dashboard header)

### Install

```bash
# Copy the generator + skills into Claude Code's config directory
mkdir -p ~/.claude/dashboard ~/.claude/skills/dashboard ~/.claude/skills/review
cp src/generate.py ~/.claude/dashboard/generate.py
cp src/skills/dashboard/SKILL.md ~/.claude/skills/dashboard/SKILL.md
cp src/skills/review/SKILL.md ~/.claude/skills/review/SKILL.md
cp src/registry.example.json ~/.claude/dashboard/registry.json
```

See [`INSTALL.md`](INSTALL.md) for the full deployment map and update instructions.

### Set Up Your First Project

**Step 1 — Check what you already have.** Look at your project directory. Do you have existing tickets, a TODO list, GitHub issues, or a product roadmap? If so, you'll convert those into `PRODUCT_BACKLOG.md`. If not, you'll start fresh.

**Step 2 — Create `PRODUCT_BACKLOG.md` in your project root.**

If you have existing work items, organize them into sections. Each ticket needs at minimum an **ID** and a **Title** — everything else has sensible defaults (priority defaults to `medium`, complexity to `M`, status is inferred from the section):

```markdown
# Product Backlog — My Project

## WIP

### B-01: User authentication flow
Priority: high | Complexity: L | Status: in-progress
OAuth2 login with Google and GitHub providers.
- [ ] Google OAuth integration
- [ ] GitHub OAuth integration
- [ ] Session management

## For Review

## Backlog

### B-02: Export to CSV
CSV export for the reports page.
- [ ] Column selection
- [ ] Date range filter

## Ideas

### I-01: Dark mode toggle
Let users switch between light and dark themes.

## Bugs

## Icebox

## Done

## Won't Do
```

If you're starting from scratch with nothing to import, create the file with just the section headings and add your first ticket:

```
/dashboard add myproject "My first feature"
```

**Step 3 — Register your project.** Edit `~/.claude/dashboard/registry.json` — three fields are required:

```json
{
  "projects": [
    {
      "id": "myproject",
      "name": "My Project",
      "path": "~/projects/myproject"
    }
  ]
}
```

**Step 4 — Generate and verify.** Run `/dashboard` from your project directory (or `python3 ~/.claude/dashboard/generate.py`). The dashboard should open in your browser with your tickets visible on the board.

### Minimum Requirements

The system needs two things to operate:

1. A `PRODUCT_BACKLOG.md` in your project root with at least one `###` ticket under a `##` section
2. A registry entry in `~/.claude/dashboard/registry.json` with `id`, `name`, and `path`

Everything else — priority, complexity, status, description, acceptance criteria — is optional and has defaults. But the more you fill in, the more useful the board becomes. Acceptance criteria (checkbox items) are what drive the review and acceptance workflow, so you'll want those before a ticket moves to WIP.

## How It Works

```
PRODUCT_BACKLOG.md  ──┐
                      ├──→  generate.py  ──→  sdlc-dashboard.html  ──→  browser
PRODUCT_SPECIFICATION.md ─┘
```

- **PRODUCT_BACKLOG.md** in each project root tracks active work (ideas, backlog, WIP, for review, done, won't do, icebox, bugs)
- **PRODUCT_SPECIFICATION.md** tracks shipped features (shown in a collapsible "Done" section)
- The generator script parses both files and renders a self-contained HTML dashboard
- No drift — the files ARE the board

## How the Process Works

### Stages and States

Ticket Takeaway uses a **stage-and-state** model drawn from Kanban methodology. The board has two layers:

- **Stages** are the `## Section` headings in your backlog file — they become the columns on your board. A stage represents *where* a ticket sits in the workflow: Ideas, Backlog, WIP, For Review, Done.
- **States** are the `Status:` values on individual tickets — they represent *what's happening* within that stage. Multiple states can exist in the same stage. A ticket in the Backlog column might be `proposed` (just a title), `specified` (has acceptance criteria), or `ready` (fully specced and unblocked). They're all in the same column, but they tell you very different things about how close that ticket is to being worked on.

If you've used JIRA, this maps to the distinction between board columns and workflow statuses. If you've used GitHub Projects, it's the difference between which column a card sits in and its Status field value. The section heading is the physical lane; the status is the logical position within it.

| Stage (Column) | States within it | What this stage means |
|----------------|-----------------|----------------------|
| **Ideas** | `proposed` | Unvetted — just a title or rough notion |
| **Backlog** | `proposed`, `specified`, `ready` | Being specced and queued for work |
| **WIP** | `in-progress`, `blocked` | Actively being built |
| **For Review** | `for-review`, `rework` | Code complete, awaiting sign-off |
| **Done** | `done`, `released` | Accepted or shipped |
| **Bugs** | `bug`, `bug-fixed` | Defects tracked separately |
| **Icebox** | `icebox` | Parked — not rejected, not active |
| **Won't Do** | `wont-do` | Decided against |

To move a ticket between **stages**: cut the entire `###` block and paste it under a different `##` heading.
To change **state** within a stage: edit the `Status:` value on the ticket's metadata line.

### Lifecycle: A Ticket from Idea to Release

**1. Idea enters the board.** Someone adds a ticket to `## Ideas` or `## Backlog`. At this point it only needs an ID and title. Status defaults to `proposed`.

**2. Idea gets specified.** A description and acceptance criteria are written — checkboxes that define what "done" looks like. Status becomes `specified`. The ticket is still in Backlog.

*Gate: ticket must have a description and at least one acceptance criterion (`- [ ]` item) to be considered specified.*

**3. Ticket becomes ready.** Dependencies are identified and met. Acceptance criteria are specific enough to be testable. Status becomes `ready`. Still in Backlog — it's queued, not started.

*Gate: criteria are actionable, nothing is blocking the start of work.*

**4. Work begins.** The ticket moves from `## Backlog` to `## WIP`. Status becomes `in-progress`. If using per-feature working files, a `docs/features/{ID}/` directory is created for plans, notes, and test results.

*Gate: ticket is `ready`, someone has picked it up.*

**5. Work completes.** All acceptance criteria have been addressed in code. The ticket moves from `## WIP` to `## For Review`. Status becomes `for-review`.

*Gate: implementation covers every acceptance criterion.*

**6. Review and acceptance.** The reviewer walks through the criteria. If issues are found, status changes to `rework` and the ticket moves back to `## WIP` for fixes. Bug sub-tickets may be created in `## Bugs`. If everything passes, run `/dashboard accept` — this moves the ticket to `PRODUCT_SPECIFICATION.md` as a permanent record and cleans up working files.

*Gate: all criteria verified, review passed.*

**Side paths:**
- **Blocked** — status changes to `blocked`, stays in `## WIP`. Unblocks by returning to `in-progress`.
- **Icebox** — ticket moves to `## Icebox` from any stage. Can return to `## Backlog` later.
- **Won't Do** — ticket moves to `## Won't Do`. Terminal, but can be revived.

### Transition Quick Reference

| From | To | What must be true |
|------|----|-------------------|
| `proposed` | `specified` | Has description + at least one acceptance criterion |
| `specified` | `ready` | Criteria are testable, dependencies identified and met |
| `ready` | `in-progress` | Picked up for work, ticket moved to `## WIP` |
| `in-progress` | `for-review` | Code addresses all criteria, ticket moved to `## For Review` |
| `for-review` | `done` | Review passed, `/dashboard accept` run |
| `for-review` | `rework` | Review found issues, ticket moved back to `## WIP` |
| Any | `icebox` | Parked for later, ticket moved to `## Icebox` |
| Any | `wont-do` | Decided against, ticket moved to `## Won't Do` |

For the complete transition spec including bug workflows and acceptance summaries, see [`docs/LIFECYCLE.md`](docs/LIFECYCLE.md).

## File Layout

```
{project}/
  PRODUCT_BACKLOG.md              # Active work
  PRODUCT_SPECIFICATION.md        # Shipped features (permanent record)
  docs/
    sdlc-dashboard.html           # Generated output (open in browser)
    features/{ID}/                # Per-feature working files (ephemeral)

~/.claude/dashboard/
  registry.json                   # Which projects to track
  generate.py                     # Generator script
```

**Authoritative spec:** See [`docs/LIFECYCLE.md`](docs/LIFECYCLE.md) for the complete feature lifecycle specification. The generate script, SKILL.md, and this README all derive from it.

## Ticket Format

Each ticket in `PRODUCT_BACKLOG.md` follows this exact format:

```markdown
### {ID}: {Title}
Priority: {priority} | Complexity: {complexity} | Status: {status}
{Description text — one or more lines}
- [ ] {Acceptance criterion 1}
- [ ] {Acceptance criterion 2}
- [x] {Completed criterion}
```

### Example

```markdown
### B-01: Contact Panel + Worker Info
Priority: high | Complexity: M | Status: specified
Left-side contact panel with worker communication links.
- [ ] Phone, email, Slack links per worker
- [ ] Collapsible left panel
- [ ] On-duty status indicators
```

## Fields

| Field | Format | Required | Values |
|-------|--------|----------|--------|
| **ID** | `{prefix}-{number}` | Yes | e.g., `B-01`, `R-16`, `I-03`, `W-01` |
| **Title** | Free text | Yes | Short descriptive name |
| **Priority** | Keyword | Yes (default: `medium`) | `high`, `medium`, `low` |
| **Complexity** | Size letter | Yes (default: `M`) | `S`, `M`, `L`, `XL` |
| **Status** | Keyword | Optional (defaults by section) | See status table below |
| **Description** | Free text lines | Optional | Lines after metadata, before checklist |
| **Acceptance Criteria** | `- [ ]` / `- [x]` lines | Optional | Checkbox items parsed as checklist |

### ID Prefixes

| Prefix | Meaning |
|--------|---------|
| `B-` | Backlog / active feature |
| `R-` | Released feature |
| `I-` | Idea |
| `W-` | Won't do |
| `Z-` | Icebox (parked) |
| `BUG-` | Bug report |

## Sections → Columns

The `## Section` headings in `PRODUCT_BACKLOG.md` map directly to dashboard columns:

### Kanban Columns (left to right)

| Section in Markdown | Dashboard Column | Color |
|---------------------|-----------------|-------|
| `## Ideas` | Ideas | Purple (`--status-idea`) |
| `## Backlog` | Backlog | Slate (`--status-backlog`) |
| `## WIP` | WIP | Blue (`--status-wip`) |
| `## For Review` | For Review | Amber (`--status-review`) |
### Below-Board Sections (collapsible rows, all start collapsed)

| Source | Dashboard Section | Color |
|--------|------------------|-------|
| `## Done` | Done | Green (`--status-done`) |
| `## Won't Do` | Won't Do | Dark gray (`--status-wontdo`) |
| `## Icebox` | Icebox | Cool gray (`--status-icebox`) |
| `## Bugs` | Bug Backlog | Red |

## Statuses

Each ticket has a `Status:` value providing detail within its column:

| Status | Meaning | Typical Column |
|--------|---------|---------------|
| `proposed` | Just an idea, no spec yet | Ideas, Backlog |
| `specified` | Has description + acceptance criteria written | Backlog |
| `ready` | Fully specced, ready to start building | Backlog |
| `in-progress` | Actively being worked on | WIP |
| `blocked` | Work started but stuck on something | WIP |
| `for-review` | Code complete, needs review | For Review |
| `rework` | Failed review, needs fixes | For Review |
| `done` | Reviewed and accepted | Done |
| `released` | Shipped in a version | Done |
| `wont-do` | Decided against building | Won't Do |
| `icebox` | Parked for later consideration | Icebox |
| `bug` | Active bug report | Bugs |
| `bug-fixed` | Bug has been fixed, awaiting verification | Bugs |

If `Status:` is omitted from a ticket, it defaults based on which section it's in:
- `## Ideas` → `proposed`
- `## Backlog` → `proposed`
- `## WIP` → `in-progress`
- `## For Review` → `for-review`
- `## Won't Do` → `wont-do`
- `## Icebox` → `icebox`

## Feature Lifecycle

```
proposed → specified → ready → in-progress → for-review → done/released
                                    ↑              |
                                    +--- rework ---+
```

**In terms of file moves:**
```
PRODUCT_BACKLOG.md                                    PRODUCT_SPECIFICATION.md
  ## Ideas → ## Backlog → ## WIP → ## For Review → ## Done  ──→  moved here when accepted
```

Features stay in `PRODUCT_BACKLOG.md` until accepted/shipped. Then they move to `PRODUCT_SPECIFICATION.md` as the permanent record. Status changes within the backlog = moving the `###` block between `##` sections.

## Dashboard Interactions

| Action | Behavior |
|--------|----------|
| **Single click** card | Expand/collapse — shows full description, acceptance criteria, complexity |
| **Double click** card | Copy `I want to work on {ID}: {Title}` to clipboard with green "Copied!" confirmation |
| **Filter buttons** | Filter cards by column (All, Ideas, Backlog, WIP, For Review, Done, Won't Do, Icebox) |
| **Search** | Real-time text search across titles and descriptions |
| **Won't Do / Icebox columns** | Start collapsed, click header to expand |
| **Bugs section** | Collapsible section below the board, click header to expand/collapse |

## Dashboard Layout

The board is designed to maximize visible content:

- **Header block** (scrolls away): project name, inline stats (Total/WIP/Review counts), info strip with version + code health badges
- **Filter bar** (sticky at top): status filter buttons + search — only persistent chrome
- **Kanban columns**: Ideas | Backlog | WIP | For Review | Done | Won't Do | Icebox — starts at viewport top on load
- **Bugs section**: collapsible below the board, starts collapsed, fully interactive cards

## Claude Code Integration

### Commands

| Command | What it does |
|---------|-------------|
| `/dashboard` | Parse backlog + spec → render HTML → open browser |
| `/dashboard status {project} {ID} {section}` | Move item between sections in the markdown |
| `/dashboard accept {project} {ID}` | Move item from backlog to PRODUCT_SPECIFICATION.md |
| `/dashboard add {project} "{title}"` | Add new entry to PRODUCT_BACKLOG.md |
| `/dashboard show` | Print summary table to terminal |

### Closed-Loop Workflow

Each project's `CLAUDE.md` includes rules ensuring the backlog stays current:

1. **Start work** → move item to `## WIP`, set `Status: in-progress`
2. **Blocked** → update to `Status: blocked` (stays in WIP)
3. **Code complete** → move to `## For Review`, set `Status: for-review`
4. **Accepted** → run `/dashboard accept` to move to PRODUCT_SPECIFICATION.md
5. **New feature** → add to `## Ideas` or `## Backlog`

This prevents drift between what's built and what the dashboard shows.

## Registry

`~/.claude/dashboard/registry.json` tracks which projects to include:

```json
{
  "projects": [
    {
      "id": "myproject",
      "name": "My Project",
      "path": "~/projects/myproject",
      "description": "What this project does",
      "active": true
    }
  ]
}
```

## PRODUCT_BACKLOG.md Template

```markdown
# Product Backlog — {Project Name}

## WIP

## For Review

## Backlog

## Ideas

## Bugs

## Icebox

## Done

## Won't Do
```
