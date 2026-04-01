# Product Backlog — Ticket Takeaway

## WIP

### I-07: UI Inline Editing with Field-Level Updates
Priority: medium | Complexity: M | Status: in-progress
Add inline editing to dashboard cards via a local HTTP server (stdlib http.server, zero deps). Three tiers: (1) Quick-edit on collapsed cards — click priority dot to cycle, click status badge for dropdown, click criteria checkbox to toggle. (2) Expand-to-edit — pencil icon on expanded cards transforms text into form fields, per-field auto-save on blur. (3) Creation — plus button per column header for new tickets. Server (serve.py) imports DB helpers from tickets-cli.py, exposes REST API: GET/PUT/POST. Live-update poll skips cards with data-editing=true to prevent overwriting in-progress edits. No framework — stay vanilla JS. No build step. File:// mode stays read-only (no regressions).
- [ ] Phase 1: serve.py HTTP server + quick-edit (priority cycle, status dropdown, criteria checkbox)
- [ ] Phase 2: full expand-to-edit with form rendering for all 12 editable fields
- [ ] Phase 3: new ticket creation + drag-and-drop column moves
- [ ] data-editing guard in patchCards() prevents poll from overwriting edits
- [ ] file:// mode still works read-only — editing requires serve.py

## For Review

### B-05: Real-Time Dashboard Updates with Animations
Priority: high | Complexity: M | Status: for-review
Commit: 7abdf41
Replace the full-page-reload polling with in-place DOM diffing. When the dashboard HTML file changes, fetch the new version, extract the changed cards, and patch the live DOM without reloading. Moved cards get a brief highlight indicator. New cards fade in. Removed cards fade out. Scroll position, expanded cards, search/filter state, and keyboard focus are all preserved across updates.
- [ ] Polling detects file changes without triggering a full page reload
- [ ] Changed cards are patched in-place (moved between columns, status updated, criteria toggled)
- [ ] Cards that moved between columns get a 1.5s colored border/glow indicator (reuse existing .just-moved keyframe)
- [ ] New cards fade in over 0.3s (opacity 0→1)
- [ ] Removed/archived cards fade out over 0.3s then are removed from DOM
- [ ] Scroll position is preserved across updates
- [ ] Expanded card state (clicked cards showing details) is preserved across updates
- [ ] Search filter text and active filter buttons are preserved across updates
- [ ] Summary counters (WIP count, Backlog count, etc.) update in place
- [ ] Progress bars update width smoothly (existing 0.3s transition suffices)
- [ ] No visible flicker or layout shift during normal updates
- [ ] Falls back to full reload if DOM structure changes drastically (major generator version bump)

## Backlog

### B-02: Per-Feature Working Files
Priority: medium | Complexity: M | Status: proposed
Depends: B-01
Auto-create docs/features/{ID}/ directory when a feature moves to WIP. Include PLAN.md, NOTES.md, BUGS.md, TESTS.md, REVIEW.md templates. Clean up on acceptance after /sync.
- [ ] Create directory structure on WIP transition
- [ ] Template files with section headers
- [ ] Cleanup step integrated into /accept
- [ ] /sync integration before cleanup

### B-04: Registry Auto-Discovery
Priority: low | Complexity: S | Status: proposed
Auto-detect projects that have PRODUCT_BACKLOG.md instead of requiring manual registry.json setup. Fall back to registry.json if auto-discovery finds nothing.

## Ideas

### I-04: Persist filter and search state in localStorage
Priority: medium | Complexity: S | Status: proposed
Filter tab selection and search input reset to defaults on every page reload. Save the active filter tab and search query to localStorage so the user returns to the same view. On filter click store to localStorage, on search input debounce-store, on page load read and apply before first paint. Clear search persistence if stored query no longer matches any cards. Inspired by cline/kanban UI state persistence.
- [ ] Active filter tab persists across page reload
- [ ] Search query persists across page reload
- [ ] Persisted filter applied before first visible paint (no flash of All then switch)
- [ ] Clearing the search box also clears the stored value
- [ ] Works correctly with auto-refresh polling (does not reset on poll)

### I-05: Slide-out detail panel replacing inline card expansion
Priority: medium | Complexity: L | Status: proposed
Currently clicking a card expands it inline, pushing other cards down. Replace with a slide-out detail panel (like Linear) that appears on the right side of the screen. Panel shows full card detail (description, acceptance criteria, rationale, linked children) without disrupting board layout. Fixed-position panel with slide-in animation, ESC/outside-click to close, card highlight while panel open. Double-click clipboard behavior unchanged. Inspired by cline/kanban side panel and Linear detail view.
- [ ] Single-click on card opens slide-out panel on right side
- [ ] Panel shows all card details: title, ID, status, priority, complexity, description, criteria, rationale, deps, children
- [ ] Panel slides in with CSS transition animation
- [ ] ESC key closes the panel
- [ ] Clicking outside the panel closes it
- [ ] Source card is visually highlighted while panel is open
- [ ] Double-click still copies prompt to clipboard (no regression)
- [ ] Board layout does not shift when panel opens
- [ ] Bottom list rows also open the detail panel
- [ ] Panel works correctly with filtered/searched views

### I-06: 3-line truncated description preview on collapsed cards
Priority: medium | Complexity: M | Status: proposed
Collapsed cards show only title, ID, status badge, and metadata — no description preview. Add a 3-line truncated description preview visible on collapsed cards with CSS line-clamp for truncation. Uses secondary text color for visual hierarchy, hidden when card is expanded (full description shown instead). Only rendered if description exists. Inspired by cline/kanban card information hierarchy: status dot, title, truncated description, activity, metadata.
- [ ] Collapsed cards show first 3 lines of description text
- [ ] Text is visually truncated with CSS line-clamp (ellipsis at end)
- [ ] Preview uses secondary text color for visual hierarchy
- [ ] Preview hidden when card is expanded (full description shown instead)
- [ ] Cards with no description show no preview element
- [ ] Preview text is selectable but not interactive
- [ ] Works for both kanban cards and bottom list rows

### I-03: Dependency visualization — SVG overlay lines between linked cards
Priority: medium | Complexity: L | Status: proposed
Render SVG connector lines between cards that have Depends: relationships. Currently dependencies show as text but there is no visual graph. An SVG overlay layer drawn on top of the kanban board would connect dependent cards with directional lines/arrows, making blocking relationships instantly visible. Inspired by cline/kanban DependencyOverlay component. Implementation: absolutely-positioned SVG element covering the kanban container, line drawing via getBoundingClientRect(), dashed lines for resolved deps, solid red for blocking deps, toggle button in filter bar, recalculate on resize/filter/expand.
- [ ] SVG overlay renders directional lines between cards with Depends: relationships
- [ ] Blocking deps shown as solid red lines; resolved deps as dashed gray
- [ ] Lines recalculate on card expand/collapse, filter change, and window resize
- [ ] Toggle button in filter bar to show/hide dependency overlay
- [ ] Lines work across columns (e.g. WIP card depending on Backlog card)
- [ ] No lines rendered when dependency target card is hidden by filter

## Bugs

## Icebox

## Done

### R-01: Dashboard Concept & Spec
Priority: high | Complexity: M | Status: released
Initial dashboard specification document defining the kanban layout, dark theme, card interactions, and data flow from markdown to HTML.

### R-02: PRODUCT_BACKLOG.md Format
Priority: high | Complexity: M | Status: released
Defined the markdown format for tickets: sections map to columns, ### headings are tickets, metadata line with Priority/Complexity/Status, acceptance criteria as checkboxes.

### R-03: Lifecycle Specification
Priority: high | Complexity: L | Status: released
Comprehensive lifecycle document (docs/LIFECYCLE.md) defining all 13 statuses, transition rules, per-feature working files, and closed-loop guarantee.

### B-01: Python Generator Script
Priority: high | Complexity: L | Status: done
Self-contained Python script that parses PRODUCT_BACKLOG.md + PRODUCT_SPECIFICATION.md and generates a dark-theme HTML kanban dashboard. Runs in <1 second, outputs to {project}/docs/.
- [x] Parse PRODUCT_BACKLOG.md sections into tickets
- [x] Parse PRODUCT_SPECIFICATION.md for done items
- [x] Generate self-contained HTML with inline CSS/JS
- [x] Dark theme with CSS variables
- [x] Kanban columns: Ideas, Backlog, WIP, For Review
- [x] Collapsible bottom sections: Bugs, Icebox, Done, Won't Do
- [x] Single-click expand cards, double-click copy prompt
- [x] Sticky filter bar with search
- [x] Auto-scroll to board on load
- [x] Collect git/code stats for header
- [x] Multi-project support (tabs when >1 project)
- [x] Handle edge cases in markdown parsing

### B-03: Dashboard Skill Simplification
Priority: medium | Complexity: S | Status: done
Rationale: Original SKILL.md included HTML template details that are now in generate.py, causing unnecessary context bloat
Simplify the SKILL.md now that generate.py handles HTML. The skill should focus on: when to run the script, how to edit PRODUCT_BACKLOG.md, and the status/add/accept/show commands.
- [ ] Remove HTML template details from SKILL.md
- [ ] Keep only: mode detection, markdown editing rules, script invocation
- [ ] Reduce skill file size for faster loading

### I-01: Live File Watcher
Priority: low | Complexity: M | Status: done
File watcher that auto-regenerates the dashboard when PRODUCT_BACKLOG.md changes. Could use inotifywait or Python watchdog.

### I-02: CLI Direct Invocation
Priority: low | Complexity: S | Status: done
Allow running `python3 generate.py --project myproject` directly without the Claude skill wrapper.

### R-04: Nested Parent-Child Tickets
Priority: high | Complexity: L | Status: done
Generalized parent-child ticket relationships. Child tickets with Parent: field render nested inside parent cards. Smart labels (bugs vs sub-tickets). Individually clickable child cards with context-aware clipboard prompts.

### R-05: List-View Bottom Sections
Priority: medium | Complexity: M | Status: done
Bottom sections (Done, Bugs, Icebox, Won't Do) render as compact list rows instead of card grids. Same click/dblclick behavior, distinct visual style.

### R-06: Auto-Promotion of Parent Tickets
Priority: medium | Complexity: S | Status: done
Parents auto-promote to For Review when all child tickets are in resolved statuses (for-review, bug-fixed, done).

### R-07: SQLite as Source of Truth
Priority: high | Complexity: XL | Status: done
Migrated from markdown-as-source-of-truth to SQLite. tickets.db stores all ticket data. PRODUCT_BACKLOG.md is auto-generated. Read-before-write sync absorbs direct markdown edits.

### R-08: tickets-cli.py CRUD CLI
Priority: high | Complexity: L | Status: done
Full CLI with seed, list, add, update, move, accept, sync, watch subcommands. Auto-syncs DB to markdown and regenerates HTML after every write.

### R-09: install.py Installer
Priority: medium | Complexity: M | Status: done
Commit: c2b84a6
Release: v0.1.0
One-command install/upgrade/register. Copies CLI, generator, and skills to runtime locations. Registers projects and seeds DB from existing markdown.

### R-10: Live Dashboard Auto-Refresh
Priority: medium | Complexity: S | Status: done
Commit: 05b7730
CLI regenerates HTML after every write. File watcher (watch command) detects direct markdown edits. Browser polls every 2s and auto-reloads with card-moved highlighting.

## Won't Do

