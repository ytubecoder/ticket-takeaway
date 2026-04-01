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

## Archive

_Retired features are summarized here. See git history for full details._
