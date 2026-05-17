# Product Specification — Ticket Takeaway

Accepted and shipped features for the Ticket Takeaway tool.

---

### R-01: Dashboard Concept & Spec
Priority: high | Complexity: M | Status: released
Released: v0.1 | Date: 2026-03-26

Initial dashboard specification defining the kanban-style feature tracker. Dark theme HTML dashboard generated from markdown files, with card interactions (click to expand, double-click to copy work prompt), sticky filter bar, and collapsible sections.

Development notes:
- Iterated through several layout approaches before settling on compact header + sticky filter bar
- Originally used JSON as intermediary data format, later simplified to parse markdown directly

---

### R-02: PRODUCT_BACKLOG.md Format
Priority: high | Complexity: M | Status: released
Released: v0.1 | Date: 2026-03-26

Defined the canonical markdown format for tickets. Sections (`## WIP`, `## Backlog`, etc.) map directly to dashboard columns. Each `###` heading is a ticket with a metadata line (`Priority: X | Complexity: Y | Status: Z`) and optional acceptance criteria (`- [ ]` checkboxes).

Development notes:
- Replaced earlier JSON-based data model to eliminate drift between source and display
- Key decision: section heading IS the column — no separate status-to-column mapping needed
- Added 6 ID prefixes (B-, R-, I-, W-, Z-, BUG-) for clear ticket categorization

---

### R-03: Lifecycle Specification
Priority: high | Complexity: L | Status: released
Released: v0.1 | Date: 2026-03-26

Comprehensive lifecycle document (`docs/LIFECYCLE.md`) serving as the authoritative reference for the entire system. Defines 13 statuses, state transition diagram, per-feature working files, acceptance process (including /sync before cleanup), and closed-loop guarantee.

Development notes:
- Per-feature working files concept added to support ephemeral dev artifacts without polluting permanent docs
- /sync step before cleanup ensures learnings are extracted before deletion

---


### B-05: Real-Time Dashboard Updates with Animations
Priority: high | Complexity: M | Status: released
Released: 2026-04-01 | Commit: 7abdf41
Replace the full-page-reload polling with in-place DOM diffing. When the dashboard HTML file changes, fetch the new version, extract the changed cards, and patch the live DOM without reloading. Moved cards get a brief highlight indicator. New cards fade in. Removed cards fade out. Scroll position, expanded cards, search/filter state, and keyboard focus are all preserved across updates.


### B-06: serve.py HTTP Server + Quick-Edit Controls
Priority: medium | Complexity: M | Status: released
Released: 2026-04-02 | Commit: a7ba01f
Phase 1 of I-07. Local HTTP server (stdlib http.server, zero deps) serves dashboard with REST API. Quick-edit: click priority dot to cycle, click status badge for dropdown, click criteria checkbox to toggle. data-editing guard in patchCards(). file:// mode still read-only.


### I-12: Gate panel: Claude CLI round-trip with diff-style merge UI
Priority: high | Complexity: M | Status: released
Released: 2026-04-02 | Commit: 6b49669
When creating new content or reviewing existing content in the gate panel (e.g. editing description, criteria, review notes), the edited text should be sent to Claude CLI for enrichment/validation and returned. The UI should show a diff-style display comparing current vs suggested, with point-by-point accept/reject for each change. Use a pattern that doesn't clobber existing content — insertions and updates are presented as discrete merge operations the user controls.


### I-13: Gate panel: verify all edits persist to DB and sync to markdown
Priority: medium | Complexity: M | Status: released
Released: 2026-04-02 | Commit: 6b49669
Verify that all edits made through the gate panel (description changes, new criteria, flag toggles, review notes) are persisted to the SQLite database and then synced to PRODUCT_BACKLOG.md. This is a verification/hardening sub-ticket, not new functionality.


### I-14: Gate panel: show output directory path with click-to-copy
Priority: low | Complexity: M | Status: released
Released: 2026-04-02 | Commit: 6b49669
The gate panel should display the ticket's output directory path (e.g. docs/features/B-05/) in the panel. Clicking it copies the path to clipboard and shows a 'Path copied' acknowledgment toast.


### I-15: Gate panel: DCTRS icons and expanded action buttons
Priority: medium | Complexity: M | Status: released
Released: 2026-04-02 | Commit: 6b49669
Replace the plain D/C/T/R/S letter dots in the gate panel category rows with meaningful icons (e.g. document icon for D, checklist for C, flask for T, eye for R, smoke/cloud for S). When expanded, show full action buttons for each category (e.g. 'Write Description', 'Add Criteria', 'Run Tests', 'Start Review', 'Run Smoke Test') that trigger the appropriate workflow.


### B-09: Column Move Gate Check — AI-Powered Readiness Analysis
Priority: high | Complexity: M | Status: released
Released: 2026-04-02 | Commit: 6b49669
When a ticket is moved to a top kanban column (Ideas, Backlog, WIP, For Review, Done), the move is intercepted and a Claude CLI agent analyzes the ticket's DCTRS readiness. Results are shown in an expandable panel with per-section editable fields and independent Save buttons. Users can edit suggestions, save per-section, then Confirm Move or Cancel. Bottom sections (Bugs, Icebox, Won't Do) remain ungated.


### I-07: UI Inline Editing with Field-Level Updates
Priority: medium | Complexity: M | Status: released
Released: 2026-04-03 | Commit: 91f188e
Add inline editing to dashboard cards via a local HTTP server (stdlib http.server, zero deps). Three tiers: (1) Quick-edit on collapsed cards — click priority dot to cycle, click status badge for dropdown, click criteria checkbox to toggle. (2) Expand-to-edit — pencil icon on expanded cards transforms text into form fields, per-field auto-save on blur. (3) Creation — plus button per column header for new tickets. Server (serve.py) imports DB helpers from tickets-cli.py, exposes REST API: GET/PUT/POST. Live-update poll skips cards with data-editing=true to prevent overwriting in-progress edits. No framework — stay vanilla JS. No build step. File:// mode stays read-only (no regressions).


### I-16: e2e-lifecycle-1775151721
Priority: medium | Complexity: M | Status: released
Released: 2026-04-03 | Commit: c33135a


### B-16: Test Framework — Smoke, E2E Journey, TDD
Priority: high | Complexity: L | Status: released
Released: 2026-04-03 | Commit: 64aece0
Three-category test framework: (1) Smoke tests — click everything, verify every UI element renders/responds/persists, organized per page (kanban, expanded card, detail overlay, gate panel). (2) E2E Journey tests — multi-step user workflows (ticket lifecycle, bug workflow, gate check, quick edit, external edit). (3) TDD tests — written before implementing complex new logic (business rules, status validation, compute_status_on_move). Smoke + E2E written against refactored actions.py API. Separate worktree recommended.


### I-18: e2e-lifecycle-1775447438
Priority: medium | Complexity: M | Status: released
Released: 2026-04-06 | Commit: 7e8e1aa


### B-19: Cache AI assessment responses
Priority: high | Complexity: M | Status: released
Released: 2026-04-06 | Commit: baf1818
Add _assessCache JS object keyed by ticketId+section/cat. Check cache before fetch in startGateCheck and runCategoryAssess. Cache hit shows results instantly. Invalidate on populate() when ticket data changes.


### I-20: e2e-lifecycle-1775598720
Priority: medium | Complexity: M | Status: released
Released: 2026-04-08 | Commit: 2546bef


### I-20: e2e-lifecycle-1775600110
Priority: medium | Complexity: M | Status: released
Released: 2026-04-08 | Commit: 8a111ec


### I-20: e2e-lifecycle-1775601928
Priority: medium | Complexity: M | Status: released
Released: 2026-04-08 | Commit: bbd6ebf


### B-42: Dual-backend scenario runner (Playwright + CDP)
Priority: medium | Complexity: M | Status: released
Released: 2026-04-16 | Commit: 015ee52


### B-1: X
Priority: medium | Complexity: M | Status: released
Released: 2026-05-04
desc


### I-31: e2e-lifecycle-1777885690
Priority: medium | Status: released
Released: 2026-05-04 | Commit: 7eb9b06


### I-43: Bookmarks and Recents in the left nav
Priority: medium | Status: released
Released: 2026-05-17 | Commit: b998a25
Add Bookmarks and Recents collapsible sections to the left nav rail. Users can star tickets to bookmark them; opening any ticket adds it to recents. Per-project, DB-backed. Star toggle visible on kanban cards and in the ticket overlay header. Move Settings to the bottom of the rail.


### B-69: Bookmarks/recents UX fixes (toggle, exclude, inline)
Priority: medium | Status: released
Released: 2026-05-17 | Commit: b998a25

## Archive

_Retired features are summarized here. See git history for full details._
