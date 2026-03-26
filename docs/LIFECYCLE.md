# Ticket Takeaway — Feature Lifecycle Specification

This document defines the complete ticket lifecycle, all statuses, file structures, and conventions. It is the authoritative reference — the generate script, SKILL.md, and README all derive from this.

---

## 1. Core Files

| File | Location | Purpose | Owned By |
|------|----------|---------|----------|
| `PRODUCT_BACKLOG.md` | `{project}/` | Active work: all tickets not yet accepted | Developer + Claude |
| `PRODUCT_SPECIFICATION.md` | `{project}/` | Permanent record of accepted/shipped features | Developer |
| `docs/sdlc-dashboard.html` | `{project}/docs/` | Generated dashboard (do not edit manually) | `generate.py` |
| `docs/features/{ID}/` | `{project}/docs/features/` | Per-feature working files during development | Claude (cleaned up on acceptance) |
| `registry.json` | `~/.claude/ticket-takeaway/` | Project list for the generator | Developer |
| `generate.py` | `~/.claude/ticket-takeaway/` | Dashboard generator script | Developer |
| `SKILL.md` | `~/.claude/skills/ticket-takeaway/` | Claude Code skill instructions | Developer |

---

## 2. Ticket Format

Every ticket in `PRODUCT_BACKLOG.md` uses this exact format:

```markdown
### {ID}: {Title}
Priority: {priority} | Complexity: {complexity} | Status: {status}
{Description — one or more lines of free text}
- [ ] {Acceptance criterion 1}
- [ ] {Acceptance criterion 2}
- [x] {Completed criterion}
```

### Field Definitions

| Field | Required | Format | Valid Values |
|-------|----------|--------|-------------|
| **ID** | Yes | `{prefix}-{number}` | `B-01`, `R-16`, `I-03`, `W-01`, `BUG-01`, `Z-01` |
| **Title** | Yes | Free text | Short descriptive name |
| **Priority** | Yes (default: `medium`) | Keyword | `high`, `medium`, `low` |
| **Complexity** | Yes (default: `M`) | Size letter | `S`, `M`, `L`, `XL` |
| **Status** | Optional (defaults by section) | Keyword | See status table below |
| **Description** | Optional | Free text lines (before checkboxes) | Multi-line description |
| **Acceptance Criteria** | Optional | `- [ ]` / `- [x]` lines | Checkbox items |

### ID Prefixes

| Prefix | Meaning | Typical Section |
|--------|---------|-----------------|
| `B-` | Backlog / active feature | Backlog, WIP, For Review |
| `R-` | Released feature | Done |
| `I-` | Idea | Ideas |
| `W-` | Won't do | Won't Do |
| `Z-` | Icebox (parked) | Icebox |
| `BUG-` | Bug report | Bugs |

---

## 3. Sections (in PRODUCT_BACKLOG.md)

Sections are `##` headings that determine where a ticket appears on the dashboard.

### Kanban Columns (visible on the board, left to right)

| Section | Column | Color | Default Status |
|---------|--------|-------|---------------|
| `## Ideas` | Ideas | Purple (`#8b5cf6`) | `proposed` |
| `## Backlog` | Backlog | Slate (`#6b7280`) | `proposed` |
| `## WIP` | WIP | Blue (`#3b82f6`) | `in-progress` |
| `## For Review` | For Review | Amber (`#f59e0b`) | `for-review` |

### Collapsible Rows (below the board, all start collapsed)

| Section | Row | Color | Default Status |
|---------|-----|-------|---------------|
| `## Bugs` | Bug Backlog | Red (`#ef4444`) | `bug` |
| `## Icebox` | Icebox | Cool gray (`#94a3b8`) | `icebox` |
| `## Done` | Done | Green (`#22c55e`) | `done` |
| `## Won't Do` | Won't Do | Dark gray (`#4b5563`) | `wont-do` |

### Section Order in PRODUCT_BACKLOG.md

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

Active work sections (WIP, For Review, Backlog) appear first. Archival sections (Done, Won't Do) appear last.

---

## 4. Statuses

Each ticket has a `Status:` value. The section determines the dashboard column; the status provides finer detail within that column.

| Status | Meaning | Valid In Sections |
|--------|---------|-------------------|
| `proposed` | Just an idea, no spec written yet | Ideas, Backlog |
| `specified` | Has description + acceptance criteria | Backlog |
| `ready` | Fully specced, dependencies met, ready to build | Backlog |
| `in-progress` | Actively being worked on | WIP |
| `blocked` | Work started but stuck on a dependency or issue | WIP |
| `for-review` | Code complete, ready for review | For Review |
| `rework` | Failed review, needs fixes before re-review | For Review |
| `done` | Reviewed and accepted, not yet in a release | Done |
| `released` | Shipped in a tagged version | Done |
| `wont-do` | Decided against building | Won't Do |
| `icebox` | Parked for later — not rejected, not active | Icebox |
| `bug` | Active bug report | Bugs |
| `bug-fixed` | Bug has been fixed, awaiting verification | Bugs |

---

## 5. Feature Lifecycle

### State Transitions

```
                    ┌─────────────────────────────────────────┐
                    │                                         │
  proposed ──→ specified ──→ ready ──→ in-progress ──→ for-review ──→ done ──→ released
                                          │                │
                                          │                ↓
                                       blocked          rework ──→ (back to in-progress)
```

### In Terms of File Moves

```
PRODUCT_BACKLOG.md                                        PRODUCT_SPECIFICATION.md
┌──────────────────────────────────────────────┐          ┌─────────────────────────┐
│ ## Ideas → ## Backlog → ## WIP → ## For Review │ ──→ │ ## Done → accepted here   │
│                                                │          │ (permanent record)        │
│ ## Bugs (can move to WIP when being fixed)     │          └─────────────────────────┘
│ ## Icebox (can move back to Backlog anytime)   │
│ ## Won't Do (terminal, can be revived)         │
└──────────────────────────────────────────────┘
```

### Transition Rules

| From | To | Trigger | Who |
|------|----|---------|-----|
| `proposed` → `specified` | Backlog | Feature gets description + acceptance criteria written | Developer/Claude |
| `specified` → `ready` | Backlog | Dependencies met, ready to start | Developer |
| `ready` → `in-progress` | WIP | Work begins, move to `## WIP` | Claude (auto on feature start) |
| `in-progress` → `blocked` | WIP | Stuck on dependency | Claude/Developer |
| `blocked` → `in-progress` | WIP | Blocker resolved | Claude/Developer |
| `in-progress` → `for-review` | For Review | Code complete, move to `## For Review` | Claude (auto on feature complete) |
| `for-review` → `rework` | For Review | Review found issues | Developer |
| `rework` → `in-progress` | WIP | Fixes started, move back to `## WIP` | Claude |
| `for-review` → `done` | Done | Review passed → `/sync` → summarize → `/accept` → clean up working files | Developer |
| `done` → `released` | Done | Shipped in a version tag | Developer |
| Any → `icebox` | Icebox | Parked for later, move to `## Icebox` | Developer |
| Any → `wont-do` | Won't Do | Decided against, move to `## Won't Do` | Developer |
| `icebox` → `proposed` | Backlog | Revived, move back to `## Backlog` | Developer |
| `bug` → `bug-fixed` | Bugs | Fix implemented | Claude |
| `bug-fixed` → removed | — | Verified fixed, remove from backlog | Developer |

---

## 6. Per-Feature Working Files

During development, each feature gets a working directory for ephemeral development artifacts:

```
{project}/docs/features/{ID}/
  PLAN.md              # Implementation plan (created by Claude during planning)
  NOTES.md             # Development notes, decisions, dead ends
  BUGS.md              # Bugs discovered during development of THIS feature
  TESTS.md             # Test plan, manual test results
  REVIEW.md            # Review checklist, review notes
```

### Rules

- **Created**: When a feature moves to `## WIP` (status: `in-progress`)
- **Active during**: Development and review phases
- **Cleaned up**: When the feature is accepted (`/accept`), following this sequence:
  1. **Run `/sync`** first — this extracts learnings, decisions, and stable patterns from the working files into session logs and CLAUDE.md. This ensures nothing valuable is lost.
  2. **Summarize into PRODUCT_SPECIFICATION.md** — key findings, bug count, notable decisions get written into the accepted feature entry
  3. **Delete `docs/features/{ID}/`** — the working directory is removed. The knowledge has been preserved in the spec and session logs.
- **Not committed to git**: Add `docs/features/*/` to `.gitignore` if desired (these are working files, not permanent docs)

**Important**: Never delete working files without running `/sync` first. The sync step is what prevents knowledge loss — it pulls out anything that should survive into the right permanent locations.

### What Goes Where

| Artifact | Goes In | Why |
|----------|---------|-----|
| Implementation plan | `docs/features/{ID}/PLAN.md` | Ephemeral — the code IS the plan once built |
| Bug found during dev | `docs/features/{ID}/BUGS.md` | Ephemeral — bugs are fixed, not documented forever |
| Test results | `docs/features/{ID}/TESTS.md` | Ephemeral — test code lives in the codebase |
| Design decisions | `docs/features/{ID}/NOTES.md` | Key ones get summarized into spec on acceptance |
| Review feedback | `docs/features/{ID}/REVIEW.md` | Ephemeral — addressed and done |
| Feature description | `PRODUCT_BACKLOG.md` | Source of truth while active |
| Accepted feature record | `PRODUCT_SPECIFICATION.md` | Permanent record |

### Acceptance Summary Format

When a feature is accepted, add a summary to `PRODUCT_SPECIFICATION.md`:

```markdown
### {ID}: {Title}
Priority: {priority} | Complexity: {complexity} | Status: released
Released: v{version} | Date: {date}
{Description}

Development notes:
- {N} bugs found and fixed during development
- Key decision: {notable architectural decision, if any}
- {Any other relevant context for future reference}
```

---

## 7. Dashboard Interactions

| Action | Behavior |
|--------|----------|
| **Single click** card | Expand/collapse — shows full description, acceptance criteria, complexity |
| **Double click** card | Copy `I want to work on {ID}: {Title}` to clipboard with green "Copied!" toast |
| **Filter buttons** | Filter kanban cards by column |
| **Search** | Real-time text search across titles and descriptions |
| **Bottom sections** | Click header to expand/collapse (all start collapsed) |

---

## 8. Dashboard Commands

| Command | What It Does |
|---------|-------------|
| `/dashboard` | Run `generate.py` → render HTML → open browser |
| `/dashboard status {project} {ID} {section}` | Move ticket between sections in PRODUCT_BACKLOG.md |
| `/accept {ID}` | Run `/sync` first, then move ticket to PRODUCT_SPECIFICATION.md with summary, then clean up `docs/features/{ID}/` |
| `/dashboard add {project} "{title}"` | Add new ticket to PRODUCT_BACKLOG.md |
| `/dashboard show` | Print summary table to terminal |

---

## 9. Closed-Loop Guarantee

To prevent drift between actual work and the dashboard:

1. **Each project's CLAUDE.md** must include the Product Backlog Rules section requiring updates at every status transition
2. **Feature development** must update `PRODUCT_BACKLOG.md` when:
   - Starting work (→ WIP)
   - Completing code (→ For Review)
   - Getting blocked (update status)
3. **The dashboard reads markdown directly** — no JSON intermediary, no sync step, no cache
4. **The generate script** runs in <1 second — just parse markdown + render HTML
5. **Per-feature working files** are ephemeral and cleaned up on acceptance — they don't accumulate

---

## 10. File Structure Summary

```
{project}/
├── PRODUCT_BACKLOG.md          # Active tickets (source of truth)
├── PRODUCT_SPECIFICATION.md    # Accepted features (permanent record)
├── CLAUDE.md                   # Includes backlog rules
├── docs/
│   ├── sdlc-dashboard.html     # Generated dashboard (do not edit)
│   └── features/               # Per-feature working files (ephemeral)
│       ├── B-01/
│       │   ├── PLAN.md
│       │   ├── NOTES.md
│       │   ├── BUGS.md
│       │   └── TESTS.md
│       └── B-02/
│           └── ...

~/.claude/ticket-takeaway/
├── registry.json               # Project list
└── generate.py                 # Generator script

~/.claude/skills/ticket-takeaway/
└── SKILL.md                    # Claude Code skill
```
