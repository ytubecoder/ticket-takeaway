---
name: ticket-takeaway
description: Ticket Takeaway dashboard — track features from ideation to release. Generate visual dashboard, update item status in real-time, review completed features, add new items. Cross-project.
user_invocable: true
---

# Dashboard — Ticket Takeaway Feature Tracker

Track features across all projects from ideation through release. Generates a self-contained dark-theme HTML kanban dashboard.

**Architecture:** `SQLite DB` → `tickets-cli.py` → `PRODUCT_BACKLOG.md` + `HTML dashboard`

- **SQLite DB** at `~/.claude/ticket-takeaway/tickets.db` is the **source of truth** for all active work.
- **`tickets-cli.py`** is the CLI for all CRUD operations. Every write auto-syncs DB → PRODUCT_BACKLOG.md.
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
  Ideas → Backlog → WIP → For Review  ──→  accepted, moved here permanently
```

---

## PRODUCT_BACKLOG.md Format

Each project has a `PRODUCT_BACKLOG.md` at its root. Sections map directly to kanban columns:

```markdown
# Product Backlog — ProjectName

## WIP
### B-10: Landing Page Polish
Priority: medium | Status: in-progress
Landing page refinements — layout, branding, signup messaging.
- [ ] Hero section with clear value prop
- [ ] Signup CTA above the fold
- [ ] Mobile responsive layout
- [ ] Screenshot carousel of key features

### B-11: Geo-Located Landing Screenshots
Priority: low | Status: blocked
Automated geo-located screenshot generation — waiting on API access.
- [ ] Playwright script captures screenshots at 10+ city locations
- [ ] Screenshots auto-uploaded to asset directory

## For Review
### B-05: Security Hardening Initiative
Priority: high | Status: for-review
httpOnly cookies, CSP headers, rate limiting.
- [ ] All auth tokens in httpOnly cookies
- [ ] CSP headers configured
- [ ] Rate limiting on auth endpoints

## Backlog
### B-01: Contact Panel + Worker Info
Priority: high | Status: specified
Left-side contact panel with worker communication links.
- [ ] Phone, email, Slack links per worker
- [ ] Collapsible left panel
- [ ] On-duty status indicators

### B-02: Task Interactions
Priority: high | Status: proposed
Enhanced task interaction with context menus and floating cards.

## Ideas
### I-01: Kanban Drag-Drop Enhancement
Priority: low | Status: proposed
Enhanced kanban board with drag-drop reordering.

## Done
### R-01: Authentication & User Management
Priority: high | Status: released
Email+password auth, roles, onboarding. Released v1.0.2.

### R-02: Form Template System
Priority: high | Status: released
CRUD form templates with 17 field types. Released v1.0.2.

## Won't Do
### W-01: Native Mobile App
Priority: low | Status: wont-do
PWA approach chosen instead.

## Icebox
### Z-01: Offline Mode Support
Priority: low | Status: icebox
Service worker caching for offline use — revisit later.

## Bugs
### BUG-01: Form validation error on mobile
Priority: high | Status: in-progress
Submit button unresponsive on iOS Safari when keyboard is open.
- [ ] Reproduce on iOS 17 Safari
- [ ] Fix viewport handling during keyboard display
```

**Parsing rules:**
- `## WIP` → WIP column
- `## For Review` → For Review column
- `## Backlog` → Backlog column
- `## Ideas` → Ideas column
- `## Done` → Done column
- `## Won't Do` → Won't Do column (starts collapsed)
- `## Icebox` → Icebox column
- `## Bugs` → Collapsible section below the kanban board (starts collapsed)
- Each `###` heading is one item. Format: `### {ID}: {Title}`
- First line after heading: `Priority: {high|medium|low} | Status: {status}`
- Optional lines after metadata (each on its own line, before description):
  - `Parent: {parent-id}` — links sub-tickets to parent
  - `Rationale: {reason}` — captures "why" decisions (shown collapsed on card)
  - `Depends: {id1}, {id2}` — inter-ticket dependencies (blocked tickets dimmed on dashboard)
- Next paragraph (non-bullet lines) until a bullet list or next `###`/`##`: **description text**
- Lines starting with `- [ ]` or `- [x]`: **acceptance criteria** (rendered as checklist in expanded card view)
- If no priority line, default: `medium`
- If no status, defaults based on section: backlog→`proposed`, wip→`in-progress`, review→`for-review`, ideas→`proposed`

**Status is optional metadata within a section.** The section determines the column. Status provides finer detail (e.g., `blocked` within WIP, `specified` vs `proposed` within Backlog). If omitted, defaults to the section's primary status.

| Section | Example Statuses | Default |
|---------|-----------------|---------|
| Ideas | `proposed` | `proposed` |
| Backlog | `proposed`, `specified`, `ready` | `proposed` |
| WIP | `in-progress`, `blocked` | `in-progress` |
| For Review | `for-review`, `rework` | `for-review` |
| Done | `done`, `released` | `done` |
| Won't Do | `wont-do` | `wont-do` |
| Icebox | `icebox` | `icebox` |
| Bugs | `bug`, `bug-fixed` | `bug` |

---

## CLI Quick Reference

All ticket changes go through `tickets-cli.py`. The CLI shorthand used below:
```
CLI=~/.claude/ticket-takeaway/tickets-cli.py
```

### `move` vs `update` — IMPORTANT DISTINCTION

**`move`** changes the **section** (column on the board) AND sets the default status for that section. Use this for all section transitions.

**`update --status`** changes ONLY the **status label** within the current section. Use this for fine-grained status changes where the ticket stays in the same column (e.g., `in-progress` → `blocked` within WIP, or `proposed` → `specified` within Backlog).

**WRONG:** `python3 $CLI update goodform B-02 --status wontdo` — This changes the status text but leaves B-02 in its current section. The ticket disappears from the expected column.

**RIGHT:** `python3 $CLI move goodform B-02 wontdo` — This moves B-02 to the Won't Do section and sets status to `wontdo`.

### Common workflows with examples

**Start work on a ticket:**
```bash
python3 $CLI move goodform B-05 wip
# B-05 → WIP section, status = in-progress
```

**Ticket is blocked (stays in WIP):**
```bash
python3 $CLI update goodform B-05 --status blocked
# B-05 stays in WIP, status changes to blocked
```

**Unblock and resume:**
```bash
python3 $CLI update goodform B-05 --status in-progress
# B-05 stays in WIP, status changes back to in-progress
```

**Code complete — move to review:**
```bash
python3 $CLI move goodform B-05 review
# B-05 → For Review section, status = for-review
```

**Review found issues — send back to WIP for rework:**
```bash
python3 $CLI move goodform B-05 wip
python3 $CLI update goodform B-05 --status rework
# B-05 → WIP section, status = rework
```

**Accept a feature (moves to Done + appends to PRODUCT_SPECIFICATION.md):**
```bash
python3 $CLI accept goodform B-05
# B-05 → Done section, status = done, summary appended to PRODUCT_SPECIFICATION.md
```

**Shelve a ticket for later:**
```bash
python3 $CLI move goodform B-05 icebox
# B-05 → Icebox section, status = icebox
```

**Won't do this ticket:**
```bash
python3 $CLI move goodform B-05 wontdo
# B-05 → Won't Do section, status = wontdo
```

**Mark as done (without full acceptance flow):**
```bash
python3 $CLI move goodform B-05 done
# B-05 → Done section, status = done (no PRODUCT_SPECIFICATION.md entry — use /accept for that)
```

**Spec a ticket (update description and criteria within Backlog):**
```bash
python3 $CLI update goodform B-05 --description "New description text" --status specified
python3 $CLI update goodform B-05 --add-criteria "First acceptance criterion"
python3 $CLI update goodform B-05 --add-criteria "Second acceptance criterion"
# B-05 stays in current section, gets description + criteria + specified status
```

**Move from Ideas to Backlog:**
```bash
python3 $CLI move goodform I-03 backlog
python3 $CLI update goodform I-03 --status specified
# I-03 → Backlog section, status = specified (move sets default 'proposed', update overrides)
```

**Add a new ticket:**
```bash
python3 $CLI add goodform "New feature idea" --section ideas
python3 $CLI add goodform "Ready to build" --section backlog --priority high
python3 $CLI add goodform "Bug found" --section bugs --parent B-05 --description "Description of the bug"
```

**Create a bug sub-ticket linked to a parent:**
```bash
python3 $CLI add goodform "Login button broken on mobile" --section bugs --parent B-12 --priority high
# Creates BUG-XX in Bugs section, linked to parent B-12
```

**Check/uncheck acceptance criteria:**
```bash
python3 $CLI update goodform B-05 --check-criteria 1    # Check the 1st criterion
python3 $CLI update goodform B-05 --uncheck-criteria 2  # Uncheck the 2nd criterion
python3 $CLI update goodform B-05 --remove-criteria 3   # Remove the 3rd criterion
```

**Add/remove dependencies:**
```bash
python3 $CLI update goodform B-05 --add-depends B-03
python3 $CLI update goodform B-05 --remove-depends B-03
```

**List tickets:**
```bash
python3 $CLI list --project goodform                  # All tickets
python3 $CLI list --project goodform --section wip    # WIP only
python3 $CLI list --project goodform --status blocked  # Blocked tickets across all sections
```

**Watch for live dashboard updates (run in background):**
```bash
python3 $CLI watch --project goodform &
# Polls PRODUCT_BACKLOG.md every 2s, absorbs direct edits, regenerates dashboard
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

**Closed-loop requirement:** Each project's `CLAUDE.md` should include rules requiring that feature work updates the DB at every status transition.

---

## Registry Schema

`~/.claude/dashboard/registry.json`:

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

### Step 1: Read Registry

Read `~/.claude/dashboard/registry.json`. If it does not exist, report error and stop.

### Step 2: Run the generator script

```bash
python3 ~/.claude/dashboard/generate.py
```

This script parses `PRODUCT_BACKLOG.md` + `PRODUCT_SPECIFICATION.md` for each project, generates the HTML to `{project}/docs/sdlc-dashboard.html`, and opens it in the browser. It runs in under a second.

For programmatic/agent queries, use `--json` to get structured JSON output to stdout:
```bash
python3 ~/.claude/dashboard/generate.py --json
```

**Do NOT generate HTML manually.** Always use the script. The script is the source of truth for the HTML template.

If the script doesn't exist, report: `Generator script missing. Expected at ~/.claude/dashboard/generate.py`

### Step 3: HTML Reference (for understanding, not for generating)

Read ALL project JSON files from `~/.claude/dashboard/data/`. Generate a self-contained HTML file at `{project.path}/docs/sdlc-dashboard.html` for each project.

The HTML must be **completely self-contained** — all CSS and JS inline, no external dependencies, no CDN links.

#### HTML Structure — Compact Layout

The layout is designed so the kanban board is the primary content. All metadata is compressed into a thin header block that scrolls away, with the filter bar sticky at the top.

```
┌─ .header-block (scrolls away, NOT sticky) ──────────────────┐
│  .header-row: "Ticket Takeaway" | "Updated Mar 25" | [Total 24] [WIP 2] [Review 0] [Done 14] │
│  .tabs: [Project A] [Project B] [ALL]  (hidden when only 1 project)                          │
│  .info-strip: ProjectName v1.0.19 [●58%] [v1.0.12][v1.0.11]... | ≈sparkline≈ Files 463 LOC 151k │
├─ .filter-bar (position:sticky; top:0; z-index:100) ─────────┤
│  STATUS [All 24] [Backlog 8] [WIP 2] ... | [Search________] │
├─ .main ──────────────────────────────────────────────────────┤
│  .kanban: | Ideas | Backlog | WIP | For Review | Done | Won't Do | Icebox │
│  .bug-section (collapsible, starts collapsed)                │
└──────────────────────────────────────────────────────────────┘
```

On initial page load, auto-scroll so the filter bar is at the top of the viewport:
```js
requestAnimationFrame(function() {
  var filterEl = document.getElementById("filterBar");
  if (filterEl) window.scrollTo(0, filterEl.offsetTop);
});
```

```html
<body>
  <div class="header-block">
    <div class="header-row">
      <h1>Ticket Takeaway</h1>
      <span class="updated">Updated {date}</span>
      <div class="inline-stats"><!-- 4 small badges: Total, WIP, Review, Done --></div>
    </div>
    <div class="tabs"><!-- project tabs, hidden when only 1 project --></div>
    <div class="info-strip"><!-- project name+ver, progress ring(22px), release pills, sparkline(60x18), code health badges --></div>
  </div>
  <div class="filter-bar"><!-- status filter buttons + search input --></div>
  <div class="main">
    <div class="kanban"><!-- columns: Ideas, Backlog, WIP, For Review, Done, Won't Do, Icebox --></div>
    <div class="bug-section collapsed"><!-- Bugs, collapsible, starts collapsed, fully interactive cards --></div>
  </div>
  <script>/* All JS inline */</script>
</body>
```

**Key layout rules:**
- `.header-block` is NOT sticky — it scrolls away. Background: `--bg-surface`.
- `.filter-bar` IS sticky at `top: 0`, `z-index: 100`. It's the only persistent chrome.
- `.tabs` row is hidden (`display: none`) when there's only 1 project.
- Stats are inline badges in the header row (not large tiles). Format: `Total <span class="val">24</span>`.
- Info strip shows: project name + version, tiny progress ring (22px conic-gradient), up to 5 release pills, separator, sparkline SVG (60x18), and code health badges (Files, LOC, Deps, Commits, Releases).
- Padding is tight: header-row `6px 24px`, tabs `4px 24px`, info-strip `5px 24px`, filter-bar `6px 24px`.

#### CSS Variables (Dark Theme)

Use these exact CSS custom properties:

```css
:root {
  /* Backgrounds - 4 elevation levels */
  --bg-page: #0a0a0b;
  --bg-surface: #141417;
  --bg-card: #1a1a1f;
  --bg-hover: #222228;

  /* Borders - 3 weights */
  --border-subtle: #1e1e24;
  --border-default: #2a2a32;
  --border-strong: #3a3a44;

  /* Text - not pure white (reduces glare) */
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
  --status-icebox: #94a3b8;      /* cool gray */

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

  /* Typography */
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, "Cascadia Code", "Source Code Pro", Menlo, Consolas, "DejaVu Sans Mono", monospace;
}
```

#### Kanban Column Mapping

**Each markdown section maps 1:1 to a dashboard column.** The section name IS the column:

| Section in PRODUCT_BACKLOG.md | Dashboard Column | Color | Visual Treatment |
|---|---|---|---|
| `## Ideas` | Ideas | `--status-idea` (Purple) | Purple left border |
| `## Backlog` | Backlog | `--status-backlog` (Slate) | Default slate cards |
| `## WIP` | WIP | `--status-wip` (Blue) | Blue left border; blocked items get red dot |
| `## For Review` | For Review | `--status-review` (Amber) | Amber left border + subtle amber bg |
**Column order** (left to right): Ideas | Backlog | WIP | For Review

**Below-Board Sections (collapsible rows, all start collapsed):**

| Section in PRODUCT_BACKLOG.md | Dashboard Section | Color | Visual Treatment |
|---|---|---|---|
| `## Done` | Done | Green (`--status-done`) | Collapsible row, fully interactive cards |
| `## Won't Do` | Won't Do | Dark gray (`--status-wontdo`) | Collapsible row, fully interactive cards |
| `## Icebox` | Icebox | Cool gray (`--status-icebox`) | Collapsible row, fully interactive cards |
| `## Bugs` | Bug Backlog | Red (`--priority-high`) | Collapsible row, fully interactive cards |

#### Feature Card HTML

**Standard card (backlog, WIP, ideas):**

Cards show a short description (2-line clamp) by default. Single-click expands to show full description and acceptance criteria. Double-click still copies the work prompt.

```html
<div class="card" data-column="{colKey}" data-title="{title}" data-desc="{desc}" data-item-id="{id}">
  <div class="card-top">
    <span class="priority-dot {priority}"></span>
    <span class="card-title">{title}</span>
  </div>
  <div class="card-meta">
    <span class="card-id">{id}</span>
    <span class="status-badge {status}">{status}</span>
  </div>
  <div class="card-desc">{first line of description — 2-line clamp}</div>
  <div class="card-expand">
    <div class="card-desc-full">{full description text}</div>
    <div class="card-criteria">
      <div class="criteria-label">Acceptance Criteria</div>
      <ul>{criteria items as checkboxes}</ul>
    </div>
  </div>
</div>
```

**Card expand/collapse:** Single-click toggles `.expanded` class on the card. When expanded, `.card-expand` is visible and `.card-desc` is hidden.

```css
.card-desc { -webkit-line-clamp: 2; -webkit-box-orient: vertical; display: -webkit-box; overflow: hidden; }
.card-expand { display: none; }
.card.expanded .card-expand { display: block; }
.card.expanded .card-desc { display: none; }
.card-desc-full { font-size: 12px; color: var(--text-secondary); line-height: 1.5; margin-bottom: 8px; }
.card-criteria { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border-subtle); }
.criteria-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-tertiary); font-weight: 600; margin-bottom: 4px; }
.card-criteria ul { list-style: none; padding: 0; }
.card-criteria li { font-size: 11px; color: var(--text-secondary); padding: 2px 0; }
.card-criteria li::before { content: '☐ '; color: var(--text-tertiary); }
```

```js
// Single-click: expand/collapse
card.addEventListener("click", function(e) {
  if (e.detail === 1) { // single click only, not part of dblclick
    var self = this;
    this._clickTimer = setTimeout(function() { self.classList.toggle("expanded"); }, 200);
  }
});
// Double-click: copy prompt (cancel the single-click expand)
card.addEventListener("dblclick", function(e) {
  clearTimeout(this._clickTimer);
  // ... copy prompt logic ...
});
```

**Status badge CSS** — small pill with color matching the status:
```css
.status-badge {
  font-size: 9px; padding: 1px 6px; border-radius: 10px;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em;
}
.status-badge.proposed { background: var(--status-backlog-bg); color: var(--status-backlog); }
.status-badge.specified { background: rgba(99,102,241,0.12); color: #818cf8; }
.status-badge.ready { background: rgba(34,197,94,0.12); color: var(--status-done); }
.status-badge.in-progress { background: var(--status-wip-bg); color: var(--status-wip); }
.status-badge.blocked { background: rgba(239,68,68,0.12); color: var(--priority-high); }
.status-badge.for-review { background: var(--status-review-bg); color: var(--status-review); }
.status-badge.rework { background: rgba(239,68,68,0.12); color: var(--priority-high); }
```

**For Review card:** Same as standard but add `border-left: 3px solid var(--status-review)` and amber background tint.

**Won't Do cards** use gray styling, collapsed by default.

#### Double-Click to Copy Prompt

Every card supports double-click to copy a work prompt to clipboard. The prompt text is: `I want to work on {id}: {title}`. On double-click, show a green "Copied!" badge that fades out:

```js
card.addEventListener("dblclick", function(e) {
  e.preventDefault();
  var itemId = this.getAttribute("data-item-id");
  var item = items.find(function(i) { return i.id === itemId; });
  if (item && navigator.clipboard) {
    navigator.clipboard.writeText("I want to work on " + item.id + ": " + item.title);
    var badge = document.createElement("span");
    badge.className = "card-copied";
    badge.textContent = "Copied!";
    this.appendChild(badge);
    setTimeout(function() { badge.remove(); }, 1300);
  }
});
```

CSS for the copied badge:
```css
.card-copied {
  position: absolute; top: 6px; right: 8px;
  font-size: 10px; font-family: var(--font-mono); font-weight: 600;
  color: var(--status-done); background: rgba(34,197,94,0.15);
  padding: 2px 8px; border-radius: 4px; pointer-events: none;
  animation: fade-out 1.2s ease-out forwards;
}
@keyframes fade-out {
  0% { opacity: 1; } 70% { opacity: 1; } 100% { opacity: 0; }
}
```

#### Inline Stats (in header row)

Four small badges in the header row (not large tiles):
- **Total**, **WIP**, **Review**, **Done** — each as `<span class="inline-stat">Label <span class="val">N</span></span>`
- WIP val colored `--status-wip`, Review val colored `--status-review`, Done val colored `--status-done`

#### Info Strip Components

The info strip is a single flex row containing:
1. **Project name + version**: `<span class="info-project">Name <span class="ver">v1.0.19</span></span>`
2. **Progress ring** (22px): `conic-gradient(var(--status-done) {pct}%, var(--bg-hover) {pct}%)` with `data-pct` attribute
3. **Release pills**: up to 5 recent versions, first one gets `.current` class (green)
4. **Separator**: `<div class="info-sep">` (1px vertical line)
5. **Sparkline SVG** (60x18): commit activity from `commitSparkline` array
6. **Code health badges**: Files, LOC, Deps, Commits, Releases — each as `<span class="info-badge">Label <span class="val">N</span></span>`

#### Pulsing Status Dots (High Priority)

```css
.status-dot.high { position: relative; }
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

#### Card Hover and Filter Transitions

```css
.card { transition: opacity 0.2s, transform 0.2s; }
.card:hover { transform: translateY(-1px); box-shadow: 0 4px 12px #00000040; }
.card.hidden { opacity: 0; transform: scale(0.95); pointer-events: none; position: absolute; }
```

#### Print Styles

```css
@media print {
  :root {
    --bg-page: #fff; --bg-surface: #f8f8f8; --bg-card: #fff;
    --text-primary: #111; --text-secondary: #555;
    --border-default: #ddd;
  }
  .kanban { display: flex; flex-direction: column; }
  .filter-bar, .search-input, .tabs { display: none !important; }
  .header-block { position: static; }
}
```

#### JavaScript Features (inline)

- **Tab switching**: Show/hide project-specific data when clicking project tabs
- **Status filtering**: Click status badges in filter bar to show/hide columns
- **Module filtering**: Click module pills to filter cards by module
- **Search**: Real-time text search across card titles, descriptions, and IDs
- **Collapse/expand**: Won't Do and Icebox columns start collapsed; Bugs section below the board starts collapsed

### Step 4: Report

The script prints the summary and opens the browser automatically. **Relay the script output to the user**, including the file path(s) so they can open it manually if needed. Example:

```
Dashboard updated: 9 backlog, 2 WIP, 0 review, 16 done, 3 ideas, 0 icebox, 0 bugs
Output: ~/projects/myproject/docs/sdlc-dashboard.html
```

---

## Mode 2: status <project> <item-id> <new-section>

Moves an item between sections. See **CLI Quick Reference** above for the full `move` vs `update` distinction and examples.

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py move <project> <item-id> <new-section>
```

The CLI updates the DB, sets the default status, syncs PRODUCT_BACKLOG.md, and regenerates the HTML dashboard. The browser auto-refreshes within 2 seconds.

---

## Mode 3: add <project> "<title>" [options]

Adds a new ticket. See **CLI Quick Reference** above for examples.

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py add <project> "<title>" [--section S] [--priority P] [--parent ID] [--description D]
```

Auto-generates the ID (B- for backlog, I- for ideas, BUG- for bugs, etc.).

---

## Mode 5: show [project]

Prints a summary table to the terminal. See **CLI Quick Reference** above for filter examples.

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py list [--project <project>] [--section S] [--status S]
```

---

## Rules

- **Use `move` to change sections, `update --status` to change status within a section.** Never use `update --status` to move a ticket to a different column — it will disappear from the expected view.
- **SQLite DB is the source of truth** — `~/.claude/ticket-takeaway/tickets.db`. Preferred writes go through `tickets-cli.py`.
- **PRODUCT_BACKLOG.md is auto-generated** from the DB. You can edit it directly — the CLI absorbs changes via read-before-write sync — but the CLI is preferred.
- **The dashboard auto-refreshes** — the CLI regenerates HTML after every write, and the browser polls every 2 seconds.
- **Case-insensitive ID matching** — `b-01` matches `B-01`
- **Acceptance = move to PRODUCT_SPECIFICATION.md** — use `tickets-cli.py accept` for the full flow
- **If DB doesn't exist**, run `python3 ~/.claude/ticket-takeaway/tickets-cli.py seed` to create it from existing markdown
- **If registry doesn't exist**, report error: "No registry found. Create ~/.claude/ticket-takeaway/registry.json first."
