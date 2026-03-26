# Ticket Takeaway — Tool Specification

> Technical spec for the dashboard that visualizes the SDLC process.
> For the process itself (backlog groups, workflow statuses, planning pipeline, close-out), see `sdlc-methodology.md`.
> This spec is self-contained for building the tool — a fresh session can implement from these two documents alone.

## How to Pick Up This Work

1. **Read the methodology first**: `sdlc-methodology.md` — defines the process the dashboard visualizes
2. **Read this spec** — data schema, layout, CSS, skill definitions
3. **Check what's been built**: `ls ~/.claude/dashboard/ ~/.claude/skills/dashboard/ ~/.claude/commands/dashboard.md`
4. **Implementation plan**: `~/.claude/plans/moonlit-watching-aho.md` (wave-based parallel execution)
5. **Key source file to parse**: `{project-path}/PRODUCT_SPECIFICATION.md`
6. **Existing patterns to follow**: `~/.claude/skills/sync/SKILL.md` (skill format), `~/.claude/commands/tdd.md` (command format)

---

## Purpose

The dashboard is the visual layer for the SDLC methodology defined in `sdlc-methodology.md`. It:

1. Shows the full backlog grouped by workflow status columns (Backlog, WIP, For Review, Done, Idea, Won't Do)
2. Supports feature close-out with demo checklists and review commands
3. Works across ALL projects — project-agnostic
4. Updates in real-time as Claude works (via `/dashboard status`) and at `/sync` time
5. Includes app overview, module map, and features grouped by release
6. Shows code-level health metrics (commit activity, codebase stats, hotspots)
7. Dark theme with status animations, sparklines, progress indicators

---

## Architecture

### Three-Layer Data Flow

```
PRODUCT_SPECIFICATION.md  ──parse──>  JSON data files  ──render──>  HTML dashboard
     (product spec)                (live status tracker)          (visual output)
                                        ^
                                        |
                              Claude updates status
                              in real-time as it works
```

- **Markdown is the product spec** — defines what features exist, their priority, complexity, and acceptance criteria
- **JSON is the live status tracker** — owns the current status of each item, plus review metadata, timestamps, and notes. Updated in real-time by Claude as work progresses.
- **HTML is regenerated** after every status change and at `/sync`

### Real-Time Status Tracking

The JSON data files are the single source of truth for **item status**. Claude Code updates them as it works:

- **Starting a feature**: `/dashboard status myproject B-01 wip`
- **Feature complete**: `/dashboard status myproject B-01 done`
- **Ready for demo**: `/dashboard review myproject B-01 start`
- **Reviewed and accepted**: `/dashboard review myproject B-01 pass`

This is enforced via a convention in each project's CLAUDE.md and the global CLAUDE.md:

```markdown
## Dashboard Tracking

When working on a feature that maps to a dashboard item:
1. At start: `/dashboard status <project> <id> wip`
2. At completion: `/dashboard status <project> <id> done`
3. The dashboard HTML regenerates after each status change.

If the feature doesn't have a dashboard item yet, create one:
  /dashboard add <project> "<title>" --status wip --module <module>
```

The `/sync` skill still does a full reconciliation (re-parsing PRODUCT_SPECIFICATION.md to pick up new items, collecting code stats), but between syncs, status changes are live.

### File Layout

```
~/projects/ticket-takeaway/
  docs/
    sdlc-dashboard-spec.md     # This spec (canonical reference)

~/.claude/
  skills/
    dashboard/
      SKILL.md                 # /dashboard skill (parser + generator + review workflow)
  commands/
    dashboard.md               # /dashboard command (shortcut)
  dashboard/
    registry.json              # Which projects to track
    sdlc-dashboard.html        # Generated output (open in browser)
    data/
      myproject.json            # Parsed backlog for My Project
      another-project.json     # Parsed backlog for another project (etc.)
```

---

## Data Schema

### Project Registry (`~/.claude/dashboard/registry.json`)

```json
{
  "projects": [
    {
      "id": "myproject",
      "name": "My Project",
      "path": "~/projects/myproject",
      "specFile": "PRODUCT_SPECIFICATION.md",
      "description": "What this project does",
      "active": true
    }
  ],
  "lastUpdated": null
}
```

### Per-Project Data (`~/.claude/dashboard/data/{id}.json`)

```json
{
  "project": "myproject",
  "version": "1.0.0",
  "lastParsed": "2026-03-25T16:00:00Z",
  "overview": {
    "description": "Example project description",
    "tech": "Your tech stack here",
    "modules": [
      { "name": "Module A", "path": "/module-a", "description": "First module" },
      { "name": "Module B", "path": "/module-b", "description": "Second module" },
      { "name": "Module C", "path": "/module-c", "description": "Third module" }
    ]
  },
  "releases": [
    { "version": "1.0.0", "name": "Initial Release", "date": "2026-03", "current": true }
  ],
  "items": [
    {
      "id": "B-01",
      "title": "Example Feature",
      "group": "backlog",
      "status": "specified",
      "priority": "high",
      "complexity": "medium",
      "module": "module-a",
      "description": "Example feature description",
      "useCases": ["Example use case"],
      "dependencies": [],
      "reviewedBy": null,
      "reviewedAt": null,
      "releasedIn": null,
      "notes": "",
      "statusHistory": [
        { "status": "proposed", "at": "2026-03-20T10:00:00Z" },
        { "status": "specified", "at": "2026-03-21T14:00:00Z" }
      ]
    }
  ],
  "bugs": [],
  "codeStats": {
    "commitSparkline": [3, 7, 12, 5, 8, 2, 0, 4, 9, 6, 11, 3],
    "totalFiles": 342,
    "totalLOC": 45200,
    "dependencies": 48,
    "devDependencies": 22,
    "lastCommitAge": "2h",
    "releaseCount": 19,
    "releaseCadencyDays": 4.2,
    "hotspots": [
      { "file": "src/lib/database/index.ts", "changes": 47 },
      { "file": "src/components/Dashboard/DashboardShell.tsx", "changes": 38 }
    ],
    "fileTypes": { ".tsx": 142, ".ts": 98, ".css": 12, ".md": 24, ".sql": 55 },
    "busFactor": [
      { "file": "src/db/migrations/001_initial.sql", "contributors": 1 }
    ]
  }
}
```

### Two Dimensions: Backlog Group + Workflow Status

Items have two orthogonal attributes:

**Backlog Group** — where the item lives in the product hierarchy (from `feature-management.md`):

| Group | Description | Source |
|-------|-------------|--------|
| `backlog` | Prioritized features ready for implementation, no version assigned | Feature Backlog section |
| `idea` | Icebox / Might Do — deprioritized, reviewed quarterly | Icebox, Might Do sections |
| `released` | Shipped to production with git tag | Version History, IMPLEMENTED items |
| `wont-do` | Explicitly rejected with rationale documented | Won't Do section |

**Workflow Status** — the lifecycle step the item is currently at:

| Status | Description | Set By | Pipeline Step |
|--------|-------------|--------|---------------|
| `proposed` | Captured but not yet fleshed out | Initial state / voice dictation | Planning step 1 |
| `specified` | Requirements, success criteria, complexity defined | After fleshing out | Planning step 3 |
| `ready` | Reviewed by planner/architect/security, execution-ready | After multi-agent review | Planning step 5 |
| `in-progress` | Actively being coded by Claude or developer | `/dashboard status <id> in-progress` | Planning step 6 |
| `done` | Coding complete, needs review | `/dashboard status <id> done` | — |
| `for-review` | Awaiting live demo and sign-off by requestor | `/dashboard review <id> start` | Close-out |
| `reviewed` | Demoed, accepted, and verified | `/dashboard review <id> pass` | Close-out |
| `released` | Shipped in a tagged version | At release time (git tag) | Release |
| `blocked` | Cannot proceed — dependency or external blocker | `/dashboard status <id> blocked` | Any active stage |
| `rework` | Failed review, needs fixes | `/dashboard review <id> fail` | Close-out |

**Ticket Lifecycle Flow:**

```
proposed → specified → ready → in-progress → done → for-review → reviewed → released
                                  ↑                      |
                                  └──── rework ←─────────┘

           (blocked can occur at any active stage)
```

**Kanban columns map to workflow status**, grouped visually:
- **Backlog** column: `proposed` + `specified` + `ready` items (from backlog group)
- **WIP** column: `in-progress` + `blocked` items
- **For Review** column: `done` + `for-review` + `rework` items
- **Done** column: `reviewed` + `released` items
- **Ideas** column: items from `idea` backlog group (any workflow status)
- **Won't Do** column: items from `wont-do` group (collapsed by default)

### Parser Mapping

| PRODUCT_SPECIFICATION.md Section | Backlog Group | Workflow Status |
|---|---|---|
| `## Current Focus (WIP)` + `COMPLETED` | `backlog` | `for-review` (new) or `reviewed` (grandfathered) |
| `## Current Focus (WIP)` without COMPLETED | `backlog` | `in-progress` |
| `## Feature Backlog` with full spec | `backlog` | `specified` |
| `## Feature Backlog` without spec | `backlog` | `proposed` |
| `## Icebox` / `## Might Do` | `idea` | `proposed` |
| `## Won't Do` | `wont-do` | — |
| Items with `IMPLEMENTED vX.X.X` | `released` | `released` |

---

## Dashboard Layout

### Overall Structure

```
+------------------------------------------------------------------+
| Ticket Takeaway                             Last updated: [date] |
+------------------------------------------------------------------+
|                                                                    |
| [PROJECT TABS: My Project | Another Project | ALL]               |
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
| STATS ROW (4 metric tiles)                                       |
| [Total: 47]  [WIP: 3]  [For Review: 5]  [Done: 28]             |
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
| APP OVERVIEW PANEL                                                |
| My Project v1.0.0                                                 |
| Example project description | Your tech stack here              |
| [Progress ring: 60% complete]                                     |
|                                                                    |
| Modules: [CC] [Tasks] [Forms] [Reports] [Processes] [Security]  |
| Releases: v1.0.19 (current) > v1.0.12 > v1.0.11 > v1.0.10      |
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
| CODE HEALTH STRIP                                                 |
| [Commit sparkline 90d] | Files: 342 | LOC: 45.2k | Deps: 48    |
| [Last commit: 2h ago]  | Release cadence: 4.2d | Hotspot: db/idx|
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
| FILTER BAR                                                        |
| Status: [All] [Backlog:12] [WIP:3] [Review:5] [Done:28] ...    |
| Module: [All] [CC] [Tasks] [Forms] [Reports] ...                |
| Search: [______________________________]                          |
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
| KANBAN COLUMNS (horizontally scrollable)                          |
|                                                                    |
| | Backlog    | WIP        | For Review | Done       | Idea     | |
| | [Card]     | [Card]     | [Card]     | [Card]     | [Card]   | |
| | [Card]     | [Card]     | [Card]     | [Card]     |          | |
| | [Card]     |            |            | [Card]     |          | |
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
| BUG BACKLOG (collapsible)                                         |
| CRITICAL: 0 | HIGH: 1 | MEDIUM: 3 | LOW: 2                      |
| [bug cards in compact list view]                                  |
|                                                                    |
+------------------------------------------------------------------+
```

### Stats Row — Metric Tiles

Inspired by Linear's dashboard metric blocks. Four tiles across the top:

```
+------------+  +------------+  +--------------+  +------------+
| Total      |  | In Progress|  | For Review   |  | Shipped    |
|    47      |  |     3      |  |      5       |  |    28      |
| items      |  | features   |  | need demo    |  | reviewed   |
+------------+  +------------+  +--------------+  +------------+
```

Each tile: large number, label below, subtle background tint matching status color.

### App Overview Panel

Shows per-project context:
- Project name, version, one-line description, tech stack
- **Progress ring** (CSS conic-gradient donut): % of items in done/reviewed vs total
- **Module pills**: clickable to filter kanban by module
- **Release timeline**: horizontal list of version badges, current highlighted

### Code Health Strip

Git-derived metrics computed at `/sync` time:

| Metric | Source | Visual |
|--------|--------|--------|
| Commit activity (90 days) | `git log --since="90 days ago"` | Inline SVG sparkline |
| Total files | `find src -type f \| wc -l` | Badge |
| Lines of code | `find src -type f -name "*.ts*" \| xargs wc -l` | Badge (e.g., "45.2k") |
| Dependencies | `package.json` deps + devDeps count | Badge |
| Last commit age | `git log -1 --format=%aI` | Relative time ("2h ago") |
| Release cadence | Average days between `git tag` dates | Badge ("ships every 4.2d") |
| Top hotspot | Most-changed file in last 30 days | File path link |
| File type distribution | File extension counts | Mini horizontal stacked bar |

### Feature Cards

**Standard card:**
```
+-----------------------------------+
| [red dot] Example Feature     H   |
| #B-01  [module-a]                |
|                                   |
| Example feature with a short      |
| two-line description here         |
|                                   |
| Complexity: Medium                |
| Deps: 0  |  Criteria: 3          |
+-----------------------------------+
```

- Priority dot: pulsing for high, static for medium/low (Linear-inspired status icons)
- Module tag: colored pill
- Complexity badge: small pill
- Two-line description with overflow ellipsis
- Footer: dependency count + success criteria count

**For Review card (amber highlight):**
```
+-----------------------------------+
| [amber glow] Landing Page Polish  |
| #B-05  [FOR REVIEW]              |
|                                   |
| Rationale: UX improvements for    |
| landing page conversion           |
|                                   |
| Demo Checklist:                   |
|  [ ] Verify layout on mobile      |
|  [ ] Check signup flow            |
|  [ ] Review trust logos           |
|                                   |
| Deps: none                        |
|                                   |
| /dashboard review myproject       |
|   B-05 pass                       |
+-----------------------------------+
```

- Amber left border + subtle amber background tint
- Success criteria displayed as a checklist (visual only — not interactive)
- Copyable command at bottom for marking reviewed
- Dependencies listed explicitly

**Reviewed card (green, compact):**
```
+-----------------------------------+
| [green check] Landing Page Polish |
| #B-05  Reviewed 2026-03-15       |
+-----------------------------------+
```

Compact — collapsed by default in Done column. Click to expand.

### Kanban Columns

- CSS Grid with `grid-auto-flow: column`, `grid-auto-columns: minmax(280px, 1fr)`
- Horizontal `overflow-x: auto` with `scroll-snap-type: x mandatory`
- Column headers show count badge
- Column ordering: Backlog | WIP | For Review | Done | Idea | Won't Do
- "For Review" column has subtle amber background to draw attention
- Cards stack vertically within columns

### Bug Backlog Section

Collapsible panel below kanban. Shows bugs grouped by severity:
- Color-coded severity badges (CRITICAL=red, HIGH=orange, MEDIUM=amber, LOW=blue)
- Compact list view (one line per bug): severity badge + title + status
- Fixed bugs shown as strikethrough with version badge

---

## Visual Design

### Dark Theme

Inspired by Vercel Geist, Linear, and Grafana:

```css
:root {
  /* Backgrounds — 4 elevation levels */
  --bg-page: #0a0a0b;
  --bg-surface: #141417;
  --bg-card: #1a1a1f;
  --bg-hover: #222228;

  /* Borders — 3 weights */
  --border-subtle: #1e1e24;
  --border-default: #2a2a32;
  --border-strong: #3a3a44;

  /* Text — not pure white (reduces glare) */
  --text-primary: #ededef;
  --text-secondary: #a0a0ab;
  --text-tertiary: #6b6b76;

  /* Accent */
  --accent: #3b82f6;

  /* Status colors */
  --status-backlog: #6b7280;     /* slate */
  --status-wip: #3b82f6;         /* blue */
  --status-review: #f59e0b;      /* amber */
  --status-done: #22c55e;        /* green */
  --status-idea: #8b5cf6;        /* purple */
  --status-wontdo: #4b5563;      /* dark gray */

  /* Priority */
  --priority-high: #ef4444;      /* red */
  --priority-medium: #f59e0b;    /* amber */
  --priority-low: #3b82f6;       /* blue */

  /* Status background tints (for badges) */
  --status-backlog-bg: #6b728015;
  --status-wip-bg: #3b82f615;
  --status-review-bg: #f59e0b15;
  --status-done-bg: #22c55e15;
  --status-idea-bg: #8b5cf615;
}
```

### Typography

System font stacks — zero external loading:

```css
--font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
--font-mono: ui-monospace, "Cascadia Code", "Source Code Pro", Menlo, Consolas, "DejaVu Sans Mono", monospace;
```

- Sans for labels, descriptions, headings
- **Mono for all numbers** — IDs, counts, versions, dates, commands (equal-width characters make numbers scannable)

### Micro-Interactions

**Pulsing status dots** (high priority items):
```css
.status-dot.high::before {
  content: '';
  position: absolute;
  width: 100%; height: 100%;
  border-radius: 50%;
  background: var(--priority-high);
  animation: pulse 2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { transform: scale(1); opacity: 0.4; }
  50% { transform: scale(2.5); opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .status-dot.high::before { animation: none; }
}
```

**Progress ring** (CSS-only):
```css
.progress-ring {
  width: 64px; height: 64px;
  border-radius: 50%;
  background: conic-gradient(var(--status-done) calc(var(--pct) * 1%), var(--bg-surface) 0);
  display: grid; place-items: center;
}
.progress-ring::after {
  content: attr(data-pct) '%';
  width: 48px; height: 48px;
  border-radius: 50%;
  background: var(--bg-card);
  display: grid; place-items: center;
  font-family: var(--font-mono);
  color: var(--text-primary);
}
```

**SVG sparklines** (commit activity):
```html
<svg viewBox="0 0 120 30" class="sparkline">
  <polyline points="..." fill="none" stroke="var(--accent)" stroke-width="1.5" />
</svg>
```

**Card hover lift:**
```css
.card:hover { transform: translateY(-1px); box-shadow: 0 4px 12px #00000040; }
```

**Filter transitions:**
```css
.card { transition: opacity 0.2s, transform 0.2s; }
.card.hidden { opacity: 0; transform: scale(0.95); pointer-events: none; position: absolute; }
```

### Print-Friendly

Override CSS variables to light palette in `@media print`:
```css
@media print {
  :root {
    --bg-page: #fff; --bg-surface: #f8f8f8; --bg-card: #fff;
    --text-primary: #111; --text-secondary: #555;
    --border-default: #ddd;
  }
  .kanban { flex-direction: column; }
  .filter-bar, .search, .tabs { display: none; }
}
```

---

## Skill Definition

### `/dashboard` Skill Modes

**Mode 1: `generate` (default)**
1. Read `~/.claude/dashboard/registry.json`
2. For each active project with a `specFile`:
   a. Parse PRODUCT_SPECIFICATION.md sections into structured items
   b. Merge with existing JSON data (preserving current status, review state, notes — spec only adds NEW items)
   c. Run git commands to collect code stats (sparkline, hotspots, LOC, etc.)
   d. Read `package.json` for dependency counts
   e. Write `~/.claude/dashboard/data/{id}.json`
3. Aggregate all project JSON files
4. Render `~/.claude/dashboard/sdlc-dashboard.html` with inline CSS/JS
5. Report: "Dashboard updated: X backlog, Y WIP, Z for-review, W done"

**Mode 2: `status <project> <item-id> <new-status>`**
- Updates the item's `status` field in the project's JSON data file
- Valid statuses: `backlog`, `idea`, `wip`, `done`, `for-review`, `reviewed`, `wont-do`, `cancelled`
- Records timestamp of change
- Regenerates HTML immediately
- Example: `/dashboard status myproject B-01 wip`

**Mode 3: `review <project> <item-id> <pass|fail> [notes]`**
- `pass` — moves item to `reviewed`, records reviewer and timestamp
- `fail "reason"` — keeps in `for-review`, adds note to card
- Regenerates HTML after update
- Example: `/dashboard review myproject B-01 pass`

**Mode 4: `add <project> "<title>" [--status <status>] [--module <module>] [--priority <priority>]`**
- Adds a new item to the project's JSON data file
- Auto-generates an ID (e.g., GF-B10)
- Defaults: status=backlog, priority=medium
- Regenerates HTML
- Example: `/dashboard add myproject "New Feature" --status wip --module module-a`

**Mode 5: `show [project]`**
- Prints a summary table of all items and their statuses to the terminal
- If project specified, shows only that project
- Quick view without opening browser

### `/sync` Integration

Add step 2.7 to `~/.claude/skills/sync/SKILL.md` between steps 2.6 and 3:

```markdown
## 2.7 Update Ticket Takeaway

If `~/.claude/dashboard/registry.json` exists:
1. Check if the current project is registered (match by working directory path)
2. If registered and has a specFile:
   a. Parse PRODUCT_SPECIFICATION.md for feature definitions
   b. MERGE with existing JSON — preserve current status, review state, and notes for known items; only ADD items that are new in the spec
   c. Collect fresh code stats via git log / find / package.json
   d. Write to `~/.claude/dashboard/data/{project-id}.json`
3. Regenerate `~/.claude/dashboard/sdlc-dashboard.html`
4. Report: "Dashboard updated: X backlog, Y WIP, Z for-review, W done"

If the current project is NOT registered, skip silently.

IMPORTANT: /sync must NOT overwrite status changes made during the session.
The JSON data file owns status. The markdown spec owns feature definitions.
```

---

## Feature Close-Out Workflow

1. **Feature coding completes** — Claude runs `/dashboard status myproject B-01 done`
2. **Ready for demo** — `/dashboard review myproject B-01 start` moves to `for-review`
3. **Dashboard shows For Review column** — Amber-highlighted cards with rationale, success criteria checklist, dependencies
4. **Live demo** — Open dashboard in browser + Chrome DevTools on the actual app. Walk through each success criterion.
5. **Mark reviewed** — `/dashboard review myproject B-01 pass`
6. **Needs rework** — `/dashboard review myproject B-01 fail "Button misaligned on mobile"`

### Grandfathering

Items already marked `IMPLEMENTED` before the dashboard existed import as `reviewed` (assumed accepted).

### CLAUDE.md Convention

Add to global `~/projects/CLAUDE.md` (applies to all projects):

```markdown
## Dashboard Tracking

When working on a feature that maps to a dashboard item:
1. At start of work: `/dashboard status <project> <id> wip`
2. When coding is complete: `/dashboard status <project> <id> done`
3. The dashboard HTML regenerates after each status change.

If the feature doesn't have a dashboard item yet, create one:
  /dashboard add <project> "<title>" --status wip --module <module>

To see current status without opening browser:
  /dashboard show <project>
```

---

## Code Health Metrics

Collected at `/sync` time from git and filesystem. All computable in <5 seconds.

| Metric | Command | Visual |
|--------|---------|--------|
| Commit activity (90d) | `git log --since="90 days ago" --format=%aI` | SVG sparkline |
| Total files | `find src -type f \| wc -l` | Badge |
| Lines of code | `find src -name "*.ts*" \| xargs wc -l` | Badge ("45.2k") |
| Dependencies | `jq '.dependencies \| length' package.json` | Badge |
| Last commit age | `git log -1 --format=%aI` | Relative time |
| Release cadence | Average days between `git tag` dates | Badge ("4.2d") |
| Top 5 hotspots | `git log --since="30 days" --name-only` + count | File list |
| File type breakdown | `find src -type f` + extension count | Stacked bar |
| Bus factor | Files with only 1 contributor in git log | Warning list |

---

> **Process reference**: Backlog groups, workflow statuses, planning pipeline, wave execution, close-out workflow, and release process are defined in `sdlc-methodology.md`. The sections below are dashboard-tool-specific.

---

## Implementation Sequence

| Step | What | Files Created/Modified |
|------|------|----------------------|
| 1 | Create directory structure | `~/.claude/dashboard/`, `~/.claude/dashboard/data/` |
| 2 | Write registry.json with your project | `~/.claude/dashboard/registry.json` |
| 3 | Create /dashboard skill | `~/.claude/skills/dashboard/SKILL.md` |
| 4 | Create /dashboard command | `~/.claude/commands/dashboard.md` |
| 5 | Parse project PRODUCT_SPECIFICATION.md | `~/.claude/dashboard/data/myproject.json` |
| 6 | Collect project code stats | Updates `myproject.json` |
| 7 | Generate HTML dashboard | `~/.claude/dashboard/sdlc-dashboard.html` |
| 8 | Verify: open in browser | (verification) |
| 9 | Extend /sync with step 2.7 | `~/.claude/skills/sync/SKILL.md` |
| 10 | Test close-out workflow | (verification) |

## Verification

1. `open ~/.claude/dashboard/sdlc-dashboard.html` — renders dark kanban with project data
2. Spot-check 3-5 items match PRODUCT_SPECIFICATION.md
3. Filters work: click status badges, module pills, search box
4. Code stats strip shows sparkline and badges
5. `/dashboard review myproject <id> pass` updates JSON and HTML
6. Run `/sync` in a tracked project — dashboard regenerates with fresh timestamp

---

## Design Inspiration Sources

- **Linear**: Animated status icons, RAG health badges, staleness indicators, metric tiles
- **Vercel**: Monochrome + accent color, deployment-as-status with screenshot thumbnails
- **GitHub Projects**: Roadmap timeline with "today" marker, burn-up charts
- **Jira**: Sprint health stacked bars, velocity sparklines, scope change detection
- **Notion**: Rollup progress bars, emoji project icons, pastel tag pills
- **Grafana**: Dark theme palette, information density, time-series sparklines
