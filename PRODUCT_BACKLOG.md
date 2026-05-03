# Product Backlog — Ticket Takeaway

## WIP

### B-17: Ticket Screen AI and Layout Cleanup
Priority: high | Complexity: XL | Status: in-progress
Fix the ticket detail/gate screen UX: instant open, cached AI, formatted fields, keyboard shortcuts, editable AI suggestions. 6 child tickets covering 8 requirements.
- [ ] B-18: Overlay opens instantly, AI loads in background
- [ ] B-19: Cached AI responses shown on reopen
- [ ] B-20: T/S as list items, Review renamed to Learnings
- [ ] B-21: Re-assess always visible with loading text
- [ ] B-22: Ctrl+Enter saves, Escape cancels without closing overlay
- [ ] B-23: AI suggestion text editable before applying

### B-18: Instant open with loading indicator
Priority: high | Complexity: M | Status: in-progress
Parent: B-17
Open ticket overlay INSTANTLY when triggered. Move openDetailOverlay() call before apiGateCheck() in startGateCheck(). Show loading spinner in gate banner while AI works. One fast GET for ticket data, async AI call in background.
- [ ] Overlay opens within 200ms of drag-drop (one fast GET)
- [ ] Gate banner shows loading spinner while AI works
- [ ] AI results populate into overlay when ready
- [ ] Confirm Move button disabled during loading

### B-20: Section format cleanup — list-style T/S + learnings rename
Priority: medium | Complexity: M | Status: in-progress
Parent: B-17
Convert Tests and Smoke from textarea to list-style items (like Criteria). Each line becomes an editable item with delete button. Add populateListField() function. Rename Review section to Learnings/Sync. Keep Description and Learnings as prose textarea.

### B-22: Keyboard shortcuts — Ctrl+Enter save, Escape cancel
Priority: medium | Complexity: S | Status: in-progress
Parent: B-17
Add Ctrl+Enter as save shortcut in all textareas (triggers blur-save). Escape in textarea reverts to original value and blurs without saving. Escape in textarea does NOT close overlay (stopPropagation). Escape in criteria input clears and blurs.
- [ ] Ctrl+Enter in textarea triggers blur-save
- [ ] Escape in textarea reverts and blurs without saving
- [ ] Escape in textarea does NOT close overlay
- [ ] Escape in criteria input clears and unfocuses

## For Review

### B-07: Expand-to-Edit: Full Form Editing for All Fields
Priority: medium | Complexity: L | Status: done
Parent: I-07
Phase 2 of I-07. Click any text field on an expanded kanban card to edit it in-place — title becomes input, description becomes textarea, rationale becomes textarea, etc. Single-click transforms the element, blur saves via apiPut(). No pencil icon, no edit mode toggle. Each field is independently clickable and editable. Select dropdowns for enums (priority, complexity, status, section). Criteria rows: click text to edit, click checkbox to toggle, plus add/remove buttons. Parent and depends fields get autocomplete from existing ticket IDs. Keyboard: ESC reverts, Tab moves to next field. Only in server mode (gated behind EDIT_API check).

### I-08: Undo System — Toast Countdown + Ctrl+Z
Priority: medium | Complexity: M | Status: done
Parent: I-07
Phase 3 of I-07. Undo system: toast with countdown after each edit (5s window to revert), plus Ctrl+Z to reverse the last action.
- [ ] Toast bar appears at bottom-center after every edit action
- [ ] Toast shows what changed (e.g. 'B-05 priority → high') with 5s countdown
- [ ] Clicking Undo button reverts the change via API
- [ ] Ctrl+Z / Cmd+Z reverts the last action within the 5s window
- [ ] New edit replaces previous undo opportunity (depth of 1)
- [ ] Toast disappears after 5s if no undo clicked
- [ ] Undo works for: priority, status, complexity, criteria toggle, text edits, moves
- [ ] Ctrl+Z does not fire when focused on input/textarea fields

### B-11: Security fixes — CORS, threading, content-length
Priority: high | Complexity: S | Status: for-review
Parent: B-10
Pre-refactor security fixes. (1) Lock CORS to localhost origin — critical, 5min. (2) ThreadingHTTPServer — low, 15min. (3) Content-Length cap 1MB — low, 5min. (4) Wrap _get_ticket_json in _db_lock for write-path callers — medium, 1hr. (5) Atomic spec file append under _db_lock — medium, 30min. (6) Prompt injection: XML data tags in claude -p calls — high, 2-4hr.

### B-12: Branch 1: DB cleanup — drop column, dedup, migrations
Priority: high | Complexity: M | Status: for-review
Parent: B-10
Drop redundant column field (~154 references), derive from section. Dedup 28 duplicate DB rows. Add _migrations table for version tracking. Merge FIRST — touches most lines.

### B-13: Branch 2: Status foundation — constants.py, fix move, decompose accept
Priority: high | Complexity: L | Status: for-review
Parent: B-10
Create src/constants.py (canonical STATUSES, VALID_STATUSES_BY_SECTION). Fix move logic: compute_status_on_move() preserves valid statuses. Emit STATUSES to JS. Create src/db.py and src/actions.py. Decompose cmd_accept into atomic functions + /api/tickets/<id>/accept endpoint. Merge SECOND.

### B-14: Branch 3: Rules engine — hooks, scheduled events, initial rules
Priority: medium | Complexity: L | Status: for-review
Parent: B-10
Post-change hooks in actions.py (_after_move, _after_status_change). Scheduled events table + 30s poller thread in serve.py. Ship initial rules: auto-promote parent when all children done, delayed auto-accept from For Review. Merge LAST.

### B-15: Markdown watcher — hash-based external edit detection
Priority: medium | Complexity: M | Status: for-review
Parent: B-10
Replace read-before-write ingest_markdown with hash-based change detection. Store SHA256 of generated markdown in _sync_state table. Watcher thread detects external edits (LLM/human), diffs against last-generated version, imports only deltas into DB. Keeps LLM markdown editing working without race conditions.

### B-10: Data Model Refactor + Business Rules Engine
Priority: high | Complexity: XL | Status: in-progress
Refactor ticket system from generated-HTML-with-scattered-logic to clean app architecture. DB is truth, markdown is output. Three branches: (1) db-cleanup: drop column field, dedup rows, migration tracking. (2) status-foundation: constants.py, fix move logic (guided not forced), decompose accept, unify JS dropdowns. (3) rules-engine: post-change hooks in actions.py, scheduled events poller, auto-promote parent, delayed auto-accept. New files: constants.py, db.py, actions.py. Security fixes first (CORS wildcard, prompt injection, thread safety). See plan: ~/.claude/plans/dapper-mapping-unicorn.md
- [ ] B-11: Security fixes applied
- [ ] B-12: column field dropped, duplicates cleaned
- [ ] B-13: constants.py, actions.py, db.py created; move logic fixed; accept decomposed
- [ ] B-14: Post-change hooks working; auto-promote and delayed auto-accept shipping
- [ ] B-15: External markdown edits detected and imported without clobbering
- [ ] CLI commands still work identically
- [ ] Dashboard renders correctly throughout

### B-24: Draft ticket concept — boolean property, grayed rendering, confirm/reject
Priority: high | Complexity: M | Status: for-review
Parent: B-32

### B-25: Attachments data model — generic table, feedbacks sessions as first type
Priority: high | Complexity: M | Status: for-review
Parent: B-32
Generic ticket_attachments table in SQLite. Feedbacks sessions as first attachment_type. Metadata JSON blob for type-specific data (hero_image, duration, counts). Migration in db.py alongside draft column. Depends: B-24.

### B-27: Settings panel — gear icon, drawer, feedbacks toggle/path/status
Priority: medium | Complexity: M | Status: for-review
Parent: B-32

### B-28: Feedbacks detection — probe server, check filesystem, cache status 30s
Priority: medium | Complexity: M | Status: for-review
Parent: B-32

### B-30: Feedbacks status indicator — green/gray dot, click-to-start server
Priority: medium | Complexity: M | Status: for-review
Parent: B-32

### B-26: Attachments UI — compact rows in ticket detail, player.html link, badge on cards
Priority: high | Complexity: M | Status: for-review
Parent: B-32
Compact rows in ticket detail overlay below DCSTL. Hero thumbnail, AI summary, metadata line. Click opens player.html in new tab. + Link button for picker. Attachment count badge on kanban cards. Depends: B-25, B-28.

### B-29: Record flow — popup capture, callback endpoint, auto-attach to ticket
Priority: high | Complexity: M | Status: for-review
Parent: B-32
POST /api/tickets/{id}/record returns feedbacks URL. window.open() popup. POST /api/feedbacks/callback receives session-complete. Auto-creates attachment. Depends: B-26, B-28, B-30, B-32 (feedbacks side).

### B-32: Feedbacks integration brief — spec for feedbacks team (compact mode, callback, autostart)
Priority: medium | Complexity: M | Status: for-review
Spec for feedbacks team: compact recorder widget (?mode=recorder), callback on session complete, auto-close popup. See docs/superpowers/specs/2026-04-05-feedbacks-recorder-widget.md

### B-43: GitHub branch awareness — link branches to tickets, scan from git/gh, branches dropdown panel
Priority: high | Complexity: M | Status: for-review

## Backlog

### B-23: Editable AI suggestions before accepting
Priority: medium | Complexity: M | Status: proposed
Parent: B-17
Make .diff-hunk-new contentEditable in renderDiffUI. User can edit AI suggestion text before clicking Apply. Mutate hunk.suggested in-place on input event. _applyDiffHunks reads edited value automatically. Add focus CSS for editable hunks.

### B-31: AI triage pipeline — auto-trigger, Claude CLI, draft child ticket creation
Priority: high | Complexity: M | Status: proposed
Auto-triggers when session attaches. Reads session.md + summary.json + ticket context. Claude CLI with structured prompt. Creates draft child tickets under parent. Re-triage button. 90s timeout. Depends: B-24, B-25, B-29.

### I-18: Add auto-start recording setting for feedbacks widget
Priority: medium | Complexity: S | Status: proposed
Add feedbacks.autostart boolean setting so Record button opens widget with ?autostart=1, skipping the Start Recording click. Feedbacks already supports the param — just append it to the URL in serve.py and add a toggle in the settings UI.
- [ ] New Auto-start recording toggle in Settings > Feedbacks Integration
- [ ] When enabled, recorder URL includes &autostart=1
- [ ] When disabled (default), widget opens with Start Recording button as before
- [ ] Setting persists across page reloads

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

### B-44: Add 'Ready' pill to auto+eligible kanban cards
Priority: medium | Complexity: S | Status: proposed
When a ticket is in auto mode AND eligibility checks pass, show a green 'Ready' pill in the card meta row. Today the only signal is the subtle gray kitchen-badge dot, which triggers off automation_mode alone — not strict eligibility — so a ticket marked auto but missing acceptance criteria still shows the dot. The Eligible filter chip in the top bar is currently the only place that reflects real eligibility, which is too easy to miss.
- [ ] Cards with automation_mode=auto AND automation_eligible=true render a green 'Ready' pill in the meta row (next to the status badge)
- [ ] Pill hidden on manual-mode tickets
- [ ] Pill hidden on auto-mode tickets that fail eligibility
- [ ] Pill replaced by the run-state indicator when a run is active (queued/running/needs-input/failed)
- [ ] Hover tooltip reads 'Eligible — would dispatch on next tick'

### B-21: Re-assess button — always visible with loading feedback
Priority: medium | Complexity: S | Status: proposed
Parent: B-17
Make assess/re-assess button permanently visible (not just on hover). Set loading text dynamically per field name. Force-refresh param bypasses cache.

### B-45: Show eligibility reasons in ticket detail overlay when not ready
Priority: medium | Complexity: S | Status: proposed
When a ticket is auto-mode but not eligible, show why on the detail overlay. The reasons are already returned in the ticket JSON as automation_eligibility_reasons but never displayed anywhere. PMs and devs currently have no way to see why Kitchen isn't picking up a ticket without inspecting the API directly.
- [ ] Detail overlay shows a 'Not ready' section when automation_mode=auto AND automation_eligible=false
- [ ] Section lists each failure reason as a bullet (e.g., 'No acceptance criteria', 'Blocked by B-12 (status: in-progress)')
- [ ] Section hidden when ticket is eligible OR when mode is manual/held
- [ ] Reason text is human-readable, not enum codes
- [ ] Updates live when eligibility state changes (e.g., adding criteria removes that reason on the next refresh)

### B-46: Add inline 'why not ready' indicator on auto kanban cards
Priority: medium | Complexity: M | Status: proposed
Depends: B-44
Auto-mode cards that fail eligibility need a quick on-card view of why. Today scanning the board for blockers requires opening each ticket's detail overlay just to see what's missing — too much friction. A small info icon with a popover keeps the diagnostic on the board itself.
- [ ] Auto-mode cards that fail eligibility show a small info icon next to the kitchen-badge dot
- [ ] Hovering or clicking the icon opens a popover listing the top reasons (cap at 3, with '+N more' if longer)
- [ ] No icon shown on manual-mode cards
- [ ] No icon shown when the ticket is fully eligible (the Ready pill from ticket #1 takes over)
- [ ] Reasons sourced from automation_eligibility_reasons already in the ticket JSON — no extra API call

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

### I-03: Dependency visualization — SVG overlay lines between linked cards
Priority: medium | Complexity: L | Status: proposed
Render SVG connector lines between cards that have Depends: relationships. Currently dependencies show as text but there is no visual graph. An SVG overlay layer drawn on top of the kanban board would connect dependent cards with directional lines/arrows, making blocking relationships instantly visible. Inspired by cline/kanban DependencyOverlay component. Implementation: absolutely-positioned SVG element covering the kanban container, line drawing via getBoundingClientRect(), dashed lines for resolved deps, solid red for blocking deps, toggle button in filter bar, recalculate on resize/filter/expand.
- [ ] SVG overlay renders directional lines between cards with Depends: relationships
- [ ] Blocking deps shown as solid red lines; resolved deps as dashed gray
- [ ] Lines recalculate on card expand/collapse, filter change, and window resize
- [ ] Toggle button in filter bar to show/hide dependency overlay
- [ ] Lines work across columns (e.g. WIP card depending on Backlog card)
- [ ] No lines rendered when dependency target card is hidden by filter

### B-02: Per-Feature Working Files
Priority: medium | Complexity: M | Status: proposed
Depends: B-01
Auto-create docs/features/{ID}/ directory when a feature moves to WIP. Include PLAN.md, NOTES.md, BUGS.md, TESTS.md, REVIEW.md templates. Clean up on acceptance after /sync.
- [ ] Create directory structure on WIP transition
- [ ] Template files with section headers
- [ ] Cleanup step integrated into /accept
- [ ] /sync integration before cleanup
- [ ] Test criterion from gate panel

### B-04: Registry Auto-Discovery
Priority: low | Complexity: S | Status: proposed
Auto-detect projects that have PRODUCT_BACKLOG.md instead of requiring manual registry.json setup. Fall back to registry.json if auto-discovery finds nothing.

### I-16: MCP server for LLM tool integration
Priority: medium | Complexity: M | Status: proposed

### I-19: make a 'bounce' sequence feature i.e. architecture codex > claude > codex > claude = filter decisions
Priority: medium | Complexity: M | Status: proposed

### I-29: Journey: Run from here — partial execution from a specific step
Priority: medium | Complexity: M | Status: proposed
Allow users to click a step in the journey timeline and run from that point forward. Requires backend support for partial manifest execution (skip steps before the selected index). UI: play button on each step or URL group header.

### I-30: Journey: Add ticket per URL/screen with URL stored on creation
Priority: medium | Complexity: M | Status: proposed
Add a button on each URL group header in the journey timeline to create a ticket linked to that screen URL. The ticket should store the URL so it's associated with the page/screen where the issue was found.

### I-10: test test ticket
Priority: medium | Complexity: M | Status: proposed
test pet
- [ ] integration-test criterion 1775120298
- [ ] markdown-criteria-test 1775120298
- [ ] integration-test criterion 1775127571
- [ ] markdown-criteria-test 1775127571

## Bugs

### BUG-01: Sample bug — placeholder for Live tab demo
Priority: high | Complexity: M | Status: bug

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
Rationale: Original SKILL.md included HTML template details that are now in generate.py, causing unnecessary context bloat Simplify the SKILL.md now that generate.py handles HTML. The skill should focus on: when to run the script, how to edit PRODUCT_BACKLOG.md, and the status/add/accept/show commands.
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

### B-08: Readiness Detail View — click D/C/T/R/S to edit section content
Priority: high | Complexity: L | Status: done
Clicking any readiness dot (D/C/T/R/S) opens a full ticket detail overlay with all 5 sections as navigable tabs. The clicked dot's section is auto-focused. Each section shows editable text content with 'Create New' and 'Review Existing' clipboard buttons that copy customized prompts for Claude Code CLI. Requires: DB content column on readiness_flags, new PUT API endpoint, detail overlay UI in generate.py.
- [ ] Click any readiness dot opens full ticket detail overlay
- [ ] Overlay has 5 navigable tabs (D C T R S) with auto-scroll to clicked section
- [ ] D tab edits ticket description
- [ ] C tab edits acceptance criteria with checkboxes
- [ ] T/R/S tabs edit new content field stored in readiness_flags DB table
- [ ] Auto-fill: saving content fills dot, clearing empties it
- [ ] Create New clipboard button copies customized prompt per flag type
- [ ] Review Existing clipboard button copies prompt with existing content
- [ ] Content syncs to PRODUCT_BACKLOG.md as Tests:/Reviewed:/Smoke: lines
- [ ] Roundtrip: seed from markdown preserves readiness content

### B-05: Real-Time Dashboard Updates with Animations
Priority: high | Complexity: M | Status: done
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

### I-11: Gate panel: unique URLs per screen/state
Priority: medium | Complexity: M | Status: done
Parent: B-09
Commit: fbf8e4e
Each gate-check state should have a unique URL fragment (e.g. #gate/B-05/review) so the user can share or bookmark a specific gate-check screen. Browser back/forward should navigate between gate states. URL should encode ticket ID, target column, and panel state.
- [ ] Gate-check panel sets URL hash when opened (e.g. #gate/B-05/WIP)
- [ ] Navigating to a gate URL re-opens that gate-check panel
- [ ] Browser back button closes the panel / returns to previous state
- [ ] Multiple gate panels don't clobber each other's URL state
Tests: When the readiness gating screen (ill call it ticket view) is open, the URL should change in the browser to reflect the exact screen. There should be a URL always in the browser so that bookmarks and back and forward navigation works in the browser and the user knows where they are.

### B-06: serve.py HTTP Server + Quick-Edit Controls
Priority: medium | Complexity: M | Status: done
Parent: I-07
Commit: a7ba01f
Phase 1 of I-07. Local HTTP server (stdlib http.server, zero deps) serves dashboard with REST API. Quick-edit: click priority dot to cycle, click status badge for dropdown, click criteria checkbox to toggle. data-editing guard in patchCards(). file:// mode still read-only.
- [ ] serve.py starts and serves dashboard at localhost:8787
- [ ] GET /api/tickets returns JSON ticket data
- [ ] PUT /api/tickets/<id> updates individual fields in DB
- [ ] POST /api/tickets/<id>/move moves ticket between sections
- [ ] Click priority dot cycles high/medium/low and persists
- [ ] Click status badge shows dropdown, selection persists
- [ ] Click acceptance criteria checkbox toggles and persists
- [ ] data-editing guard in patchCards() skips cards being edited
- [ ] file:// mode works read-only with no edit controls

### I-12: Gate panel: Claude CLI round-trip with diff-style merge UI
Priority: high | Complexity: M | Status: done
Parent: B-09
Commit: 6b49669
When creating new content or reviewing existing content in the gate panel (e.g. editing description, criteria, review notes), the edited text should be sent to Claude CLI for enrichment/validation and returned. The UI should show a diff-style display comparing current vs suggested, with point-by-point accept/reject for each change. Use a pattern that doesn't clobber existing content — insertions and updates are presented as discrete merge operations the user controls.
- [ ] Edited text can be sent to Claude CLI for enrichment via a 'Review with AI' button
- [ ] Response is shown as a diff: current content vs suggested content
- [ ] Each change (addition, modification, deletion) is individually accept/reject-able
- [ ] Accepted changes merge into the field without clobbering unmodified content
- [ ] User can accept all or reject all in bulk
- [ ] Works for description, criteria, and any future DCTRS category content

### I-13: Gate panel: verify all edits persist to DB and sync to markdown
Priority: medium | Complexity: M | Status: done
Parent: B-09
Commit: 6b49669
Verify that all edits made through the gate panel (description changes, new criteria, flag toggles, review notes) are persisted to the SQLite database and then synced to PRODUCT_BACKLOG.md. This is a verification/hardening sub-ticket, not new functionality.
- [ ] Description edits via gate panel Save are in tickets table
- [ ] New criteria via gate panel Save are in acceptance_criteria table
- [ ] Flag toggles during gate review are in readiness_flags table
- [ ] All DB changes trigger sync_to_markdown and regenerate_dashboard
- [ ] PRODUCT_BACKLOG.md reflects all gate panel edits after sync

### I-14: Gate panel: show output directory path with click-to-copy
Priority: low | Complexity: M | Status: done
Parent: B-09
Commit: 6b49669
The gate panel should display the ticket's output directory path (e.g. docs/features/B-05/) in the panel. Clicking it copies the path to clipboard and shows a 'Path copied' acknowledgment toast.
- [x] Output directory path is visible in the gate panel
- [x] Clicking the path copies it to clipboard
- [x] Toast shows 'Path copied' on successful copy
- [x] Path is derived from ticket ID using the project's docs/features/ convention

### I-15: Gate panel: DCTRS icons and expanded action buttons
Priority: medium | Complexity: M | Status: done
Parent: B-09
Commit: 6b49669
Replace the plain D/C/T/R/S letter dots in the gate panel category rows with meaningful icons (e.g. document icon for D, checklist for C, flask for T, eye for R, smoke/cloud for S). When expanded, show full action buttons for each category (e.g. 'Write Description', 'Add Criteria', 'Run Tests', 'Start Review', 'Run Smoke Test') that trigger the appropriate workflow.
- [x] Each DCTRS category has a recognizable icon (not just a letter)
- [ ] Icons are consistent between the readiness row on cards and the gate panel
- [ ] Expanded gate panel shows contextual action buttons per category
- [ ] Action buttons trigger appropriate workflows (clipboard prompts or direct actions)
- [ ] Icons work in both light and dark themes
- [ ] Icons degrade gracefully if custom font/SVG fails to load (fallback to letter)

### B-09: Column Move Gate Check — AI-Powered Readiness Analysis
Priority: high | Complexity: M | Status: done
Commit: 6b49669
When a ticket is moved to a top kanban column (Ideas, Backlog, WIP, For Review, Done), the move is intercepted and a Claude CLI agent analyzes the ticket's DCTRS readiness. Results are shown in an expandable panel with per-section editable fields and independent Save buttons. Users can edit suggestions, save per-section, then Confirm Move or Cancel. Bottom sections (Bugs, Icebox, Won't Do) remain ungated.
- [ ] POST /api/tickets/{id}/gate-check endpoint spawns Claude CLI and returns structured JSON
- [ ] Drag-drop moves to top columns are intercepted (not immediate)
- [ ] Action button moves to top columns are intercepted (not immediate)
- [ ] Card shows pulsing state while agent is thinking
- [ ] Gate panel renders with verdict badge and per-DCTRS category rows
- [ ] Per-section Save buttons persist edits independently of move decision
- [ ] Confirm Move executes the column move
- [ ] Cancel dismisses panel without moving, saved edits persist
- [ ] Moves to Bugs/Icebox/Won't Do bypass the gate (immediate)
- [ ] add_criteria PUT support for saving suggested new criteria

### I-07: UI Inline Editing with Field-Level Updates
Priority: medium | Complexity: M | Status: done
Commit: 91f188e
Add inline editing to dashboard cards via a local HTTP server (stdlib http.server, zero deps). Three tiers: (1) Quick-edit on collapsed cards — click priority dot to cycle, click status badge for dropdown, click criteria checkbox to toggle. (2) Expand-to-edit — pencil icon on expanded cards transforms text into form fields, per-field auto-save on blur. (3) Creation — plus button per column header for new tickets. Server (serve.py) imports DB helpers from tickets-cli.py, exposes REST API: GET/PUT/POST. Live-update poll skips cards with data-editing=true to prevent overwriting in-progress edits. No framework — stay vanilla JS. No build step. File:// mode stays read-only (no regressions).
- [ ] Phase 2: full expand-to-edit with form rendering for all 12 editable fields
- [ ] Phase 3: new ticket creation + drag-and-drop column moves
- [ ] data-editing guard in patchCards() prevents poll from overwriting edits
- [ ] file:// mode still works read-only — editing requires serve.py
- [ ] B-06: serve.py HTTP server + quick-edit controls (Phase 1)
- [ ] B-07: expand-to-edit with form rendering for all fields (Phase 2)
- [ ] I-08: new ticket creation + drag-and-drop (Phase 3)

### B-16: Test Framework — Smoke, E2E Journey, TDD
Priority: high | Complexity: L | Status: done
Commit: 64aece0
Rationale: We need to define 3 types of tests that are "available" to tickets. It's likely that for the best process we want to use TDD first but it doesnt make sense in every case due to efficiency so sometimes we will 'take a shortcut' and go with e2e/smoke tests. There may also be more fuzzy acceptance criteria. I think by default we would want a bare minimum of testable human readable items that may then inspire more technical level unit/tdd type tests. Three-category test framework: (1) Smoke tests — click everything, verify every UI element renders/responds/persists, organized per page (kanban, expanded card, detail overlay, gate panel). (2) E2E Journey tests — multi-step user workflows (ticket lifecycle, bug workflow, gate check, quick edit, external edit). (3) TDD tests — written before implementing complex new logic (business rules, status validation, compute_status_on_move). Smoke + E2E written against refactored actions.py API. Separate worktree recommended.
- [ ] Smoke tests: every API endpoint returns expected response
- [ ] Smoke tests: every UI interactive element responds to click
- [ ] E2E: ticket lifecycle journey (create → backlog → WIP → review → accept → Done)
- [ ] E2E: bug workflow journey (create bug → fix → parent auto-promotes)
- [ ] E2E: quick edit journey (click → edit → save → undo → redo)
- [ ] TDD: compute_status_on_move edge cases
- [ ] TDD: auto-promote parent logic
- [ ] Tests runnable via python3 -m pytest or similar

### B-19: Cache AI assessment responses
Priority: high | Complexity: M | Status: done
Parent: B-17
Commit: baf1818
Add _assessCache JS object keyed by ticketId+section/cat. Check cache before fetch in startGateCheck and runCategoryAssess. Cache hit shows results instantly. Invalidate on populate() when ticket data changes.

### B-42: Dual-backend scenario runner (Playwright + CDP)
Priority: medium | Complexity: M | Status: done
Commit: 015ee52

## Won't Do

### I-09: test ticket with nothing on it
Priority: medium | Complexity: M | Status: wontdo
This is just a test but the feature should be to add a text file called hello.txt

### I-17: CLI draft test
Priority: medium | Complexity: M | Status: wontdo

### I-27: e2e-draft-test-1776005127
Priority: medium | Complexity: M | Status: wontdo

### I-28: e2e-draft-test-1776006567
Priority: medium | Complexity: M | Status: wontdo

### B-36: My first ticket
Priority: medium | Complexity: M | Status: wontdo

### B-40: My first ticket
Priority: medium | Complexity: M | Status: wontdo
======= >>>>>>> scenario-runner

### B-41: Quick edit test ticket
Priority: medium | Complexity: M | Status: wontdo
Original description text

