# Product Backlog — Ticket Takeaway

## WIP

### B-01: Python Generator Script
Priority: high | Complexity: L | Status: in-progress
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

## For Review

## Backlog

### B-02: Per-Feature Working Files
Priority: medium | Complexity: M | Status: specified
Depends: B-01
Auto-create docs/features/{ID}/ directory when a feature moves to WIP. Include PLAN.md, NOTES.md, BUGS.md, TESTS.md, REVIEW.md templates. Clean up on acceptance after /sync.
- [ ] Create directory structure on WIP transition
- [ ] Template files with section headers
- [ ] Cleanup step integrated into /accept
- [ ] /sync integration before cleanup

### B-03: Dashboard Skill Simplification
Priority: medium | Complexity: S | Status: specified
Rationale: Original SKILL.md included HTML template details that are now in generate.py, causing unnecessary context bloat
Simplify the SKILL.md now that generate.py handles HTML. The skill should focus on: when to run the script, how to edit PRODUCT_BACKLOG.md, and the status/add/accept/show commands.
- [ ] Remove HTML template details from SKILL.md
- [ ] Keep only: mode detection, markdown editing rules, script invocation
- [ ] Reduce skill file size for faster loading

### B-04: Registry Auto-Discovery
Priority: low | Complexity: S | Status: proposed
Auto-detect projects that have PRODUCT_BACKLOG.md instead of requiring manual registry.json setup. Fall back to registry.json if auto-discovery finds nothing.

## Ideas

### I-01: Live File Watcher
Priority: low | Complexity: M | Status: proposed
File watcher that auto-regenerates the dashboard when PRODUCT_BACKLOG.md changes. Could use inotifywait or Python watchdog.

### I-02: CLI Direct Invocation
Priority: low | Complexity: S | Status: proposed
Allow running `python3 generate.py --project myproject` directly without the Claude skill wrapper.

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

## Won't Do
