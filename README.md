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

This is a tool for people who like to be on the tools — talking directly to their models. If you work in Claude Code (or similar), prompting your way through features one at a time, Ticket Takeaway adds the order you need to scale that up. It gives you a lightweight process so you can go from one feature to ten without losing track of what's specced, what's in progress, and what's waiting for review.

Your project board lives in `PRODUCT_BACKLOG.md` — a plain markdown file in your project root. Ticket Takeaway renders it as a kanban dashboard. No database, no JSON intermediary. Double-click any ticket to copy a ready-made prompt, paste it into Claude Code, and take it away. Because it's just clipboard copy-paste, you can run the dashboard in a browser and have as many Claude Code windows open as you want — each one working on a different ticket. The board is the coordination layer.

Two skills drive the process forward: **`/spec`** takes raw ideas and walks them into specced, backlog-ready tickets with acceptance criteria. **`/review`** takes completed features through structured review, creates bug sub-tickets from feedback, and handles the acceptance flow. Between those two gates — specification and review — you're free to build however you want.

For things you don't want to be directly involved in — security reviews, marketing output, documentation updates, compliance checks — we intend to make this compatible with agent orchestrators like [Paperclip](https://github.com/anthropics/claude-code/blob/main/AGENTS.md) so those tasks can run autonomously while you stay hands-on with the parts that matter.

<img width="1507" alt="Ticket Takeaway dashboard rendered in a browser" src="https://github.com/user-attachments/assets/7a10b450-9f84-4c4b-9481-515d448cbe2f" />

### Stages and States

The board uses a **stage-and-state** model — a pattern from Kanban methodology where workflow columns contain multiple work-item states.

**Stages** are the columns on the board (Ideas, Backlog, WIP, For Review, Done). They're defined by the `## Section` headings in your `PRODUCT_BACKLOG.md` file. A stage tells you *where* a ticket sits in the overall workflow.

**States** are the `Status:` values on individual tickets. They tell you *what needs to happen next* to move the ticket forward within its current stage. Multiple states live in the same stage — three tickets in Backlog might respectively be `proposed` (needs spec work), `specified` (has criteria but waiting on dependencies), and `ready` (good to go). Same column, different next actions.

If you've used JIRA, this is the same distinction between board columns and workflow statuses. GitHub Projects has a similar split between column placement and the status field. The section heading is the lane; the status is the position within it.

| Stage (Column) | States within it | What this stage means |
|----------------|-----------------|----------------------|
| **Ideas** | `proposed` | Unvetted — just a title or rough notion |
| **Backlog** | `proposed`, `specified`, `ready` | Being specced and queued for work |
| **WIP** | `in-progress`, `blocked` | Actively being built |
| **For Review** | `for-review`, `rework` | Code complete, awaiting sign-off |
| **Done** | `done`, `released` | Accepted or shipped |

---

# Part 1: Using Ticket Takeaway

## Getting Started

### Prerequisites

- Python 3.10+
- [Claude Code](https://claude.ai/code) CLI installed
- Git (for code stats in the dashboard header)

### Install

```bash
# Copy the generator + skills into Claude Code's config directory
mkdir -p ~/.claude/dashboard ~/.claude/skills/{dashboard,review,spec}
cp src/generate.py ~/.claude/ticket-takeaway/generate.py
cp src/skills/ticket-takeaway/SKILL.md ~/.claude/skills/ticket-takeaway/SKILL.md
cp src/skills/review/SKILL.md ~/.claude/skills/review/SKILL.md
cp src/skills/spec/SKILL.md ~/.claude/skills/spec/SKILL.md
cp src/registry.example.json ~/.claude/ticket-takeaway/registry.json
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

**Step 3 — Register your project.** Edit `~/.claude/ticket-takeaway/registry.json` — three fields are required:

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

**Step 4 — Generate and verify.** Run `/dashboard` from your project directory (or `python3 ~/.claude/ticket-takeaway/generate.py`). The dashboard should open in your browser with your tickets visible on the board.

### Minimum Requirements

The system needs two things to operate:

1. A `PRODUCT_BACKLOG.md` in your project root with at least one `###` ticket under a `##` section
2. A registry entry in `~/.claude/ticket-takeaway/registry.json` with `id`, `name`, and `path`

Everything else — priority, complexity, status, description, acceptance criteria — is optional and has defaults. But the more you fill in, the more useful the board becomes. Acceptance criteria (checkbox items) are what drive the review and acceptance workflow, so you'll want those before a ticket moves to WIP.

### Wiring It Into Your Other Projects

Once Ticket Takeaway is installed, you need to tell Claude Code to follow the process in each project you track. Add this to your project's `CLAUDE.md`:

```markdown
## Product Backlog Rules

`PRODUCT_BACKLOG.md` is the single source of truth for all active feature work.
The Ticket Takeaway dashboard (`/dashboard`) reads directly from this file.

**Closed-loop workflow — every feature status change must update PRODUCT_BACKLOG.md:**

1. **Starting work on a feature:** Move the item from `## Backlog` to `## WIP`, set `Status: in-progress`
2. **Feature blocked:** Update status to `Status: blocked` (stays in `## WIP`)
3. **Code complete, ready for review:** Move from `## WIP` to `## For Review`, set `Status: for-review`
4. **Feature accepted:** Run `/dashboard accept {project} {ID}` — moves item to `PRODUCT_SPECIFICATION.md`
5. **New feature idea:** Add to `## Ideas` or `## Backlog` (or use `/dashboard add`)

**This is mandatory.** Do not complete feature work without updating the backlog file.
```

This ensures that when Claude Code works on your project, it keeps the board in sync with what's actually being built. Without this, tickets will go stale.

## The Process

The core of Ticket Takeaway is a gated workflow. Every ticket progresses through stages, and each stage has requirements that must be met before a ticket can advance. This keeps you from starting work on something that isn't specced, or shipping something that hasn't been reviewed.

### How a Ticket Progresses

```
  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
  │          │    │          │    │          │    │          │    │          │
  │  IDEAS   │───▶│ BACKLOG  │───▶│   WIP    │───▶│  REVIEW  │───▶│   DONE   │
  │          │    │          │    │          │    │          │    │          │
  └──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘
                                        │              │
   States:         States:              │   States:    │          States:
   proposed        proposed          ┌──┘   for-review └──┐      done
                   specified         │      rework        │      released
                   ready             ▼                    ▼
                                  blocked             back to
                                  (stays               WIP
                                   in WIP)

  ┌──────────┐    ┌──────────┐    ┌──────────┐
  │  ICEBOX  │    │   BUGS   │    │ WON'T DO │    ◀── side lanes, reachable from any stage
  └──────────┘    └──────────┘    └──────────┘
```

### Gates: What's Required at Each Transition

Each transition has a gate — a condition that should be true before a ticket advances. These aren't enforced by software (it's your markdown file, you can do what you want), but they're what makes the process useful:

| Step | Transition | Gate |
|------|-----------|------|
| **Specify** | `proposed` → `specified` | Ticket has a description and at least one acceptance criterion (`- [ ]` item) |
| **Ready** | `specified` → `ready` | Criteria are specific enough to be testable, dependencies are identified |
| **Start** | `ready` → `in-progress` | Ticket is picked up for work, moved to `## WIP` |
| **Complete** | `in-progress` → `for-review` | Code addresses every acceptance criterion, moved to `## For Review` |
| **Accept** | `for-review` → `done` | Review passes, all criteria verified, `/dashboard accept` run |
| **Rework** | `for-review` → `rework` | Review found issues — ticket moves back to `## WIP` for fixes |

**How changes are made in the file:**
- **Stage change** = cut the entire `###` ticket block and paste it under a different `##` heading
- **State change** = edit the `Status:` value on the ticket's metadata line

### The Full Walk-Through

**1. Idea enters the board.** Someone adds a ticket to `## Ideas` or `## Backlog`. At this point it only needs an ID and title. Status defaults to `proposed`.

**2. Idea gets specified.** Run **`/spec`** (or `/spec {ID}` for a specific idea). The skill walks you through writing a description and acceptance criteria — checkboxes that define what "done" looks like. It'll suggest test cases you can use later with `/tdd`. Status becomes `specified`. The ticket moves to Backlog. Double-clicking an idea card on the dashboard copies `/spec {ID}` to your clipboard.

**3. Ticket becomes ready.** Dependencies are identified and met. Criteria are actionable. Status becomes `ready`. Still in Backlog — queued, not started.

**4. Work begins.** Double-click a card in Backlog or WIP to copy a build prompt. Paste it into Claude Code — or into multiple Claude Code windows if you're working on several tickets at once. The ticket moves from `## Backlog` to `## WIP`. Status becomes `in-progress`.

**5. Work completes.** All acceptance criteria addressed. The ticket moves from `## WIP` to `## For Review`. Status becomes `for-review`.

**6. Review and acceptance.** Run **`/review`** (or `/review {ID}`). The skill walks through criteria verification, creates bug sub-tickets from feedback, and handles acceptance. Double-clicking the For Review column header copies `/review` to your clipboard. Everything passes? `/dashboard accept` moves the ticket to `PRODUCT_SPECIFICATION.md` as a permanent record.

**Side paths:** `blocked` (stays in WIP, waiting on something), `icebox` (parked from any stage, can return later), `wont-do` (decided against, terminal but revivable).

For the complete transition spec including bug workflows and acceptance summaries, see [`docs/LIFECYCLE.md`](docs/LIFECYCLE.md).

## The Review Process

Review is its own workflow, not just a checkbox. When tickets land in `## For Review`, you use the `/review` skill to walk through them:

1. **Batch review.** `/review` reads all items in `## For Review`, groups related tickets, and presents them oldest-first. You walk through each batch with the model.

2. **Verify against criteria.** For each ticket, the acceptance criteria (`- [ ]` items) are the checklist. The reviewer opens the dashboard (or uses Chrome DevTools MCP tools like `take_snapshot` and `take_screenshot` to inspect the running app) and checks that each criterion is met.

3. **Feedback creates bug tickets.** If something's wrong, review feedback becomes a `BUG-` sub-ticket in `## Bugs` linked to the parent feature via a `Parent:` field. The parent ticket stays in For Review with status `rework`.

4. **Bug resolution cycle.** Bug tickets get picked up like any other ticket — double-click to copy the prompt, fix it, mark `bug-fixed`. When all child bugs are resolved, the parent feature is ready for re-review.

5. **Acceptance.** When review passes, run `/dashboard accept {project} {ID}`. This moves the ticket to `PRODUCT_SPECIFICATION.md` as a permanent record, summarizes development notes (bug count, key decisions), and cleans up the per-feature working files in `docs/features/{ID}/`.

For the full review spec including batching rules and test integration, see [`docs/REVIEW_PROCESS.md`](docs/REVIEW_PROCESS.md).

## The Dashboard

The dashboard is the main interface — a self-contained HTML file generated from your markdown. You'll use it to scan progress, pick tickets, and stay oriented.

```
PRODUCT_BACKLOG.md  ──┐
                      ├──→  generate.py  ──→  sdlc-dashboard.html  ──→  browser
PRODUCT_SPECIFICATION.md ─┘
```

- **PRODUCT_BACKLOG.md** in each project root tracks active work (ideas, backlog, WIP, for review, done, won't do, icebox, bugs)
- **PRODUCT_SPECIFICATION.md** tracks shipped features (shown in a collapsible "Done" section)
- The generator script parses both files and renders a self-contained HTML dashboard
- No drift — the files ARE the board

### Interactions

| Action | What happens |
|--------|-------------|
| **Single click** a card | Expand/collapse — shows full description, acceptance criteria, complexity |
| **Double click** a card | Copies `I want to work on {ID}: {Title}` to your clipboard — paste into Claude Code |
| **Filter buttons** | Filter cards by column (All, Ideas, Backlog, WIP, For Review, Done, Won't Do, Icebox) |
| **Search** | Real-time text search across titles and descriptions |
| **Bottom sections** | Done, Won't Do, Icebox, and Bugs start collapsed — click to expand |

### Layout

- **Header** (scrolls away): project name, inline stats (Total/WIP/Review counts), version + code health badges
- **Filter bar** (sticky): status filters + search — the only persistent chrome
- **Kanban columns**: Ideas | Backlog | WIP | For Review — starts at viewport top on load
- **Bugs section**: collapsible below the board

### Commands

| Command | What it does |
|---------|-------------|
| `/spec` | Walk through Ideas, write descriptions + acceptance criteria, move to Backlog |
| `/spec {ID}` | Specify a single idea by ID |
| `/review` | Walk through For Review tickets — verify, give feedback, or accept |
| `/review {ID}` | Review a single ticket by ID |
| `/dashboard` | Parse backlog + spec → render HTML → open browser |
| `/dashboard status {project} {ID} {section}` | Move item between sections in the markdown |
| `/dashboard accept {project} {ID}` | Move item from backlog to PRODUCT_SPECIFICATION.md |
| `/dashboard add {project} "{title}"` | Add new entry to PRODUCT_BACKLOG.md |
| `/dashboard show` | Print summary table to terminal |

### Keeping the Board Current

Each project's `CLAUDE.md` includes rules ensuring the backlog stays in sync with actual work:

1. **Start work** → move item to `## WIP`, set `Status: in-progress`
2. **Blocked** → update to `Status: blocked` (stays in WIP)
3. **Code complete** → move to `## For Review`, set `Status: for-review`
4. **Accepted** → run `/dashboard accept` to move to PRODUCT_SPECIFICATION.md
5. **New feature** → add to `## Ideas` or `## Backlog`

This prevents drift between what's built and what the dashboard shows.

---

# Part 2: Reference

## Project Structure

```
ticket-takeaway/
├── README.md                          # This file
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

## File Layout

```
{project}/
  PRODUCT_BACKLOG.md              # Active work
  PRODUCT_SPECIFICATION.md        # Shipped features (permanent record)
  docs/
    sdlc-dashboard.html           # Generated output (open in browser)
    features/{ID}/                # Per-feature working files (ephemeral)

~/.claude/ticket-takeaway/
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

## Registry

`~/.claude/ticket-takeaway/registry.json` tracks which projects to include:

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
