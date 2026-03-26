# Software Development Lifecycle (SDLC) Methodology — Ticket Takeaway

> Canonical reference for how features move from idea to production across all projects.
> This document defines the process. The dashboard (`sdlc-dashboard-spec.md`) is the tool that visualizes it.
> Project-agnostic — applies to any current and future project.

## How to Pick Up This Work

1. **This doc defines the process** — backlog groups, workflow statuses, ticket lifecycle, planning pipeline, close-out workflow, wave execution
2. **The dashboard spec** (`sdlc-dashboard-spec.md`) defines the visualization tool
3. **Implementation plan**: `~/.claude/plans/moonlit-watching-aho.md`

---

## Philosophy: Feature-First, Version-Later

Features are documented, prioritized, and implemented independently of version numbers. Version numbers are assigned only when code is ready to release. This avoids:
- Version gaps (planned v1.0.6 while v1.0.8 is released)
- Pressure to implement features in a specific order
- Confusion about what's "current" vs "future"

---

## Backlog Groups

Where the item lives in the product hierarchy. An item's group determines its visibility and priority tier.

| Group | Description | WIP Limit |
|-------|-------------|-----------|
| **Backlog** | Prioritized features ready for implementation. NO version numbers assigned | — |
| **Icebox** | Deprioritized ideas. Reviewed quarterly. May be promoted to Backlog | — |
| **WIP** | Actively being implemented | 1-2 features at a time |
| **Released** | Shipped to production with git tag. In CHANGELOG.md and Version History | — |
| **Won't Do** | Explicitly rejected with documented rationale | — |

### Backlog Management Rules

**Adding Features:**
- Document in PRODUCT_SPECIFICATION.md under "Feature Backlog"
- Include: description, rationale, success criteria, estimated complexity
- Do NOT assign version numbers

**Prioritization:**
- Order by business priority (top = highest)
- Consider efficiency gains: if Feature A is in progress and Feature B shares code/patterns, suggest implementing together
- Review and re-order periodically

**Icebox Management:**
- Not forgotten, just deprioritized
- Review quarterly to see if priorities have changed
- To promote: move from Icebox to Backlog with updated priority

---

## Workflow Statuses

The lifecycle step an item is currently at. Orthogonal to backlog group — an item in the Icebox can still be `specified` if someone wrote it up.

| Status | Description | Set By | Pipeline Step |
|--------|-------------|--------|---------------|
| `proposed` | Captured but not yet fleshed out | Initial state / voice dictation | Planning step 1 |
| `specified` | Requirements, success criteria, complexity defined | After feature fleshing out | Planning step 3 |
| `ready` | Reviewed by planner/architect/security, execution-ready | After multi-agent review | Planning step 5 |
| `in-progress` | Actively being coded by Claude or developer | `/dashboard status <id> in-progress` | Execution |
| `done` | Coding complete, needs review | `/dashboard status <id> done` | Execution |
| `for-review` | Awaiting live demo and sign-off by requestor | `/dashboard review <id> start` | Close-out |
| `reviewed` | Demoed, accepted, and verified | `/dashboard review <id> pass` | Close-out |
| `released` | Shipped in a tagged version | At release time (git tag) | Release |
| `blocked` | Cannot proceed — dependency or external blocker | `/dashboard status <id> blocked` | Any active stage |
| `rework` | Failed review, needs fixes | `/dashboard review <id> fail` | Close-out |

### Ticket Lifecycle Flow

```
proposed → specified → ready → in-progress → done → for-review → reviewed → released
                                    ↑                      |
                                    └──── rework ←─────────┘

             (blocked can occur at any active stage)
```

### Kanban Column Mapping

The dashboard groups statuses into visual columns:

| Column | Statuses Included | Visual Treatment |
|--------|-------------------|------------------|
| **Backlog** | `proposed`, `specified`, `ready` | Default slate |
| **WIP** | `in-progress`, `blocked` | Blue, blocked items get red indicator |
| **For Review** | `done`, `for-review`, `rework` | Amber highlight |
| **Done** | `reviewed`, `released` | Green, compact cards |
| **Ideas** | Items from Icebox group (any status) | Purple |
| **Won't Do** | Items from Won't Do group | Gray, collapsed by default |

---

## Feature Planning Pipeline

Repeatable process for turning verbal feature dictation into a fully planned, reviewed, and parallelized backlog.

### When to Use
- Product owner has a batch of feature ideas to capture
- Starting a new planning cycle / sprint planning
- Any time features need to go from idea -> defined -> reviewed -> execution-ready

### Step 1: Capture Features (Voice/Text Dictation) — status: `proposed`

- Product owner dictates features grouped by area
- Claude captures verbatim — no interpretation, no reading back
- Group under headings as directed
- Print full summary only when asked
- Dashboard: items created with status `proposed`

### Step 2: Cross-Reference Against Codebase

- Launch 3 Explore agents in parallel to understand current state
- Compare features against actual codebase
- Note what already exists vs. what's genuinely new

### Step 3: Flesh Out Each Feature (One Question at a Time) — status: `specified`

- Go through every feature sequentially
- Ask ONE question per interaction (never bundle multiple)
- Capture decisions inline in the plan document
- Flag open questions for grooming
- Note dependencies between features
- Dashboard: items move to `specified`

### Step 4: Multi-Agent Review (3 Parallel Agents)

- **Planner Agent**: Optimize for parallel execution with worktrees
  - Identify execution waves (what can run simultaneously)
  - Define worktree branch names
  - Size each item (S/M/L/XL)
  - Identify merge conflict hotspots
  - Design merge order per wave
- **Architect Agent**: Technical feasibility and design
  - Schema changes needed per epic
  - Pre-requisite refactors
  - Reuse opportunities across features
  - Key technical risks
- **Security Agent**: Security review
  - Risk assessment per epic (Critical/High/Medium/Low)
  - Required mitigations and security gates
  - RLS implications
  - Authentication/authorization gaps

### Step 5: Consolidate Reviews Into Plan — status: `ready`

- Update the plan document with all three agent outputs
- Organize into: execution waves, architecture review, security review
- Walk through architectural decisions with product owner
- Finalize execution order
- Dashboard: items move to `ready`

### Step 6: Save and Begin Execution — status: `in-progress`

- Save plan to `~/.claude/plans/` or `docs/tickets/`
- Create worktrees for Wave 0
- Launch parallel agents for each track
- Merge in order per wave plan
- Dashboard: items move to `in-progress` as work begins

### Pipeline Rules
- The plan document should contain BOTH the feature specs AND the execution strategy
- Security gates are blocking — features cannot ship without resolving their security findings
- Wave merges happen in defined order to minimize conflicts
- Each wave's tracks should not touch the same files

---

## Wave Execution Methodology

How implementation work is parallelized using Claude Code's multi-agent capabilities.

### Principles

1. **Dependency-first grouping** — Map all task dependencies, then group independent tasks into waves
2. **Maximum parallelism** — Everything that can run simultaneously, does
3. **Exit gates** — Each wave has a defined completion gate before the next wave starts
4. **Minimal critical path** — The longest sequential chain determines total time; minimize it

### Wave Structure

```
Wave 0  [N parallel agents]  — All tasks with zero inbound dependencies
Wave 1  [M parallel agents]  — Tasks that depend only on Wave 0 outputs
Wave 2  [P parallel agents]  — Tasks that depend on Wave 0+1 outputs
  ...
Wave N  [verification]       — Test and validate all outputs
```

### How to Build a Wave Plan

1. List all tasks and their output files
2. For each task, identify which files it needs as input
3. Draw the dependency graph
4. Topologically sort into layers (waves)
5. Within each wave, assign one agent per independent task
6. Define exit gates: which outputs from this wave are needed by the next

### Agent Context Rules

Each parallel agent receives:
- **Files to read** — specific paths, not "the whole project"
- **Files to write** — exactly one output per agent to avoid conflicts
- **Reference patterns** — existing files to follow for format/conventions
- **Spec section** — the relevant portion of the design spec, not the whole thing

### Merge Conflict Prevention

- Each wave's agents must not touch the same files
- If two tasks need to modify the same file, they go in sequential waves
- Worktree-based agents get their own copy of the repo

---

## Feature Close-Out Workflow

The process for reviewing completed features before they're considered "done."

### Why Close-Out Exists

Code being written is not the same as code being verified. Close-out ensures:
- The requestor has seen the feature working
- Success criteria have been checked against the live app
- Issues are caught while the feature is fresh in memory

### Close-Out Process

1. **Feature coding completes** — Claude runs `/dashboard status <project> <id> done`
2. **Ready for demo** — `/dashboard review <project> <id> start` moves to `for-review`
3. **Dashboard shows For Review column** — Amber-highlighted cards with:
   - Feature title and description
   - Rationale / why this was built
   - Success criteria as a visible checklist
   - Dependencies
   - Copyable command to mark reviewed
4. **Live demo** — Open dashboard in browser + Chrome DevTools on the actual app
   - Walk through each success criterion
   - Verify on relevant device sizes
   - Check edge cases mentioned in the spec
5. **Mark reviewed** — `/dashboard review <project> <id> pass`
6. **Needs rework** — `/dashboard review <project> <id> fail "reason"` — item stays in For Review with the note visible

### Close-Out Scope

Ideally a full click-through demo. At minimum:
- Rundown of the ticket description/rationale/dependencies
- Visual confirmation the feature exists in the UI
- Key happy-path verified

---

## Release Process

When features are reviewed and ready to ship.

### Version Numbering (Semantic Versioning)
- **MAJOR** (2.0.0): Breaking changes, major rewrites
- **MINOR** (1.1.0): New features, backwards compatible
- **PATCH** (1.0.9): Bug fixes, minor improvements

### Release Steps

1. Select reviewed features for the release
2. Determine version number using semver
3. Update CHANGELOG.md with release notes
4. Update Version History in PRODUCT_SPECIFICATION.md
5. Create git tag
6. Move feature documentation from WIP to "Implemented Features"
7. Dashboard: items move to `released`

---

## Real-Time Tracking Convention

Claude Code updates the dashboard as it works. This is enforced via CLAUDE.md:

```markdown
## Dashboard Tracking

When working on a feature that maps to a dashboard item:
1. At start of work: /dashboard status <project> <id> in-progress
2. When coding is complete: /dashboard status <project> <id> done
3. The dashboard HTML regenerates after each status change.

If the feature doesn't have a dashboard item yet, create one:
  /dashboard add <project> "<title>" --status in-progress --module <module>

To see current status without opening browser:
  /dashboard show <project>
```

### /sync Reconciliation

At `/sync` time, the dashboard does a full reconciliation:
1. Re-parses PRODUCT_SPECIFICATION.md for new items
2. MERGES with existing JSON — preserves current workflow status and review metadata
3. Collects fresh code stats (git log, file counts, etc.)
4. Regenerates the HTML dashboard
5. Reports summary counts

**Critical**: /sync must NOT overwrite status changes made during the session. The JSON owns status. The markdown owns feature definitions.

---

## Documents in This Project

| Document | Purpose |
|----------|---------|
| `docs/sdlc-methodology.md` | This file — the full SDLC process |
| `docs/sdlc-dashboard-spec.md` | Dashboard tool specification (data schema, layout, CSS, skill definition) |
| `~/.claude/plans/moonlit-watching-aho.md` | Current implementation plan with wave execution |

---

## Planned Enhancements (v2)

### Per-Ticket Markdown Files
Each ticket should also be a markdown file stored within a feature set/section directory. This file stores the full spec, design decisions, and can collect feedback before the ticket is accepted. The JSON dashboard data references these files.

### Automated Feature Review Step
Before human review, features go through an automated review step:
- Browser verification (screenshot, console errors, key UI elements)
- Test suite execution (if tests exist)
- Lint/typecheck pass
- Success criteria automated check where possible

The lifecycle becomes: `done` → `auto-review` → `for-review` → `reviewed`

The dashboard tracks the auto-review step and shows its results (pass/fail with evidence) on the For Review card, so the human reviewer has context before their demo session.
