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

## 3b. The Three-Layer Hierarchy

Every ticket is described by three independent layers, each answering a different question:

| Layer | Code Name | Field | Question It Answers |
|-------|-----------|-------|---------------------|
| **Section** | `section` / `column` | `Ticket.section`, `Ticket.column` | **Where is the work?** — which kanban column (Ideas → Backlog → WIP → For Review → Done) |
| **Status** | `status` | `Ticket.status` | **How is the work going?** — lifecycle state within a column (proposed, in-progress, blocked, rework, etc.) |
| **Readiness Flags** | `readiness_flags` | `Ticket.readiness_flags` | **What's been done?** — workflow checkpoints tracking completeness (D C L, plus `spec` / `verified`) |

These layers are orthogonal: a ticket in WIP (section) can be `blocked` (status) with some readiness flags filled.

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
| `rework` | Failed review, needs fixes before re-review | WIP |
| `done` | Reviewed and accepted, not yet in a release | Done |
| `released` | Shipped in a tagged version | Done |
| `wont-do` | Decided against building | Won't Do |
| `icebox` | Parked for later — not rejected, not active | Icebox |
| `bug` | Active bug report | Bugs |
| `bug-fixed` | Bug has been fixed, awaiting verification | Bugs |

---

## 4b. Readiness Flags (D C L)

Readiness flags are workflow checkpoints displayed as dots on each card. Each flag tracks whether a specific aspect of the feature has been addressed. Flags have associated **content** — the actual text/notes for that checkpoint — stored in the database.

> **Migration 15 collapsed Tests (T) and Smoke (S) into acceptance criteria.** The D-C-T-R-S model described by earlier versions of this document no longer exists, and neither does the `docs/features/{ID}/TESTS.md` content source it referenced. The `R` pane is stored under the legacy DB flag name `reviewed` and is surfaced as **L (Learnings)** in the UI.

### The Flags

| Letter | DB flag | Type | Content Source | Where Content Lives |
|--------|---------|------|---------------|---------------------|
| **D** | *(none — computed)* | Auto-computed | Ticket description field | DB `tickets.description` + `PRODUCT_BACKLOG.md` (inline) |
| **C** | *(none — computed)* | Auto-computed | Acceptance criteria list | DB `acceptance_criteria` + `PRODUCT_BACKLOG.md` (checkboxes) |
| **L** | `reviewed` | Manual + content | Collective review output | DB `readiness_flags.content` + `.feedbacks/{ID}/` |

Two further flags are written by the spec lifecycle (§4c) rather than by hand:

| DB flag | Markdown label | Written by | Content |
|---------|---------------|------------|---------|
| `spec` | `Spec:` | `tickets-cli.py spec` | `<lane>:<change-name>`, e.g. `A:b-44-knowledge-ingestion-pipeline` |
| `verified` | `Verified:` | `tickets-cli.py verify` | `exit=<n> commit=<sha> at=<iso> cmd=<command>` + indented output tail |

**Adding a flag is a three-place change, not one.** The DB accepts any flag name, so the constraints are elsewhere: `constants.READINESS_FLAG_LABELS` is the single registry that (a) both surfaces validate against and (b) supplies the `PRODUCT_BACKLOG.md` line prefix for both the writer and the parser. A flag missing from it is accepted into the DB and then silently dropped on the next markdown regeneration.

### Auto vs Manual

- **D and C** are auto-computed: the dot fills when the ticket has a description or criteria. No explicit toggle needed.
- **L** is a manual flag with content: the dot auto-fills when content is saved, auto-empties when content is cleared.
- **`spec` and `verified`** are written by the CLI only. Hand-editing them defeats the point — `verified` in particular is meant to be evidence, not an assertion.

### Review (L) — Definition

"Reviewed" is a qualitative checkpoint, distinct from the mechanical/repeatable evidence in the `verified` flag. A reviewed feature has been through a structured process capturing:

- **`/sync` output** — session learnings, decisions made during development
- **Bugs found** — and their resolution status
- **Feature implications** — what else this change affects
- **Architectural decisions** — trade-offs and rationale
- **Feedback sessions** — visual feedback via `/feedbacks` if used

The R flag content aggregates these into a single record. The `/review` skill orchestrates this process.

### Markdown Format

In `PRODUCT_BACKLOG.md`, readiness content appears as labeled lines after acceptance criteria:

```markdown
### B-05: Feature Title
Priority: high | Complexity: L | Status: in-progress
Description text here.
- [x] Criterion 1
- [ ] Criterion 2
Reviewed: Review notes and decisions
Spec: A:b-05-feature-title
Verified: exit=0 commit=abc1234 at=2026-07-23T02:00:00 cmd=tests/run-tests.sh
    Ran 278 tests in 22.387s
    OK
```

Multi-line content uses 4-space indented continuation:

```
Reviewed: First line of review content
    Continuation line 2
    Continuation line 3
```

Legacy `Tests:` / `Smoke:` lines in older markdown are ignored on ingest and dropped by the next sync, along with their indented continuations.

---

## 4c. The Spec Lifecycle (OpenSpec)

Ticket Takeaway describes work through [OpenSpec](https://openspec.dev) change proposals and closes it through one uniform gate. OpenSpec supplies the artifact lifecycle (proposal → delta → `archive` merges the delta into canonical `openspec/specs/`); Ticket Takeaway supplies the review gate OpenSpec deliberately does not have.

**Pinned to `@fission-ai/openspec@1.6.0`.** Not `latest`, not `@next` (stale at 0.3.0), and never the bare `openspec` npm name — that is a dead 2019 squat with no `bin`. 1.6.0 is the first release where `archive` and `validate` return reliable exit codes; earlier versions exited 0 even when validation failed and nothing was archived. Every invocation goes through `src/openspec_adapter.py`, which pins the version and sets `OPENSPEC_TELEMETRY=0`. Nothing else in the codebase shells out to `openspec`, so a version bump has one blast radius.

### Lanes — chosen by intent, not size

| Lane | When | Artifacts up front |
|---|---|---|
| **A — Spec'd** | You intend to hand this to agents and run it to production | `proposal.md` + `specs/<cap>/spec.md` delta + `design.md` + `tasks.md` |
| **B — Interviewed** | You know the rough shape; good questions would make it spec-able | `proposal.md` + spec delta only |
| **C — Direct** | Drip-fed, worked out as you go, rename, dep bump | none |

Lane C is not an escape hatch from rigour — it acknowledges that some work can't be described before it's done. Its obligation is *retroactive*: at close, if observable behaviour changed, a delta is written **from the diff**; if nothing observable changed, no spec is touched, but the claim must be recorded with a reason.

Change names are deterministic — `<ticket-id-lowercased>-<kebab-slug>` — so the ticket ↔ change mapping works in both directions with no join table.

### The close — identical in all three lanes

Enforced in `actions.accept_ticket()`:

1. Run the project's verify command. Real output recorded. Non-zero → refuse.
2. Check each obligation against the diff — lane A/B against the requirement scenarios in the spec delta, lane C against the ticket's acceptance criteria. Same rigour, same evidence standard, different source.
3. Behaviour changed but no delta exists (lane C) → write one now, from the diff.
4. `openspec validate <change> --strict` → must pass.
5. `openspec archive <change> -y` → merges the delta into `openspec/specs/`.
6. Commit the archive diff with the accept commit.
7. Write `PRODUCT_SPECIFICATION.md` / mark Done.

Archiving lands at step 5, *before* the commit, deliberately: archiving after a merge strands a second diff needing its own PR.

### The entry gate — no dispatch without a declared lane

The close gate has an entry-side counterpart: `_ticket_eligibility` and the seeded `Backlog → WIP` trigger both require `spec_linked`, so the Kitchen will not hand a ticket to an implementing agent on free text alone. A justified lane-C declaration satisfies it — the question at entry is "has intent been declared", not "is there a delta". This is safe to enforce against old tickets because eligibility already requires `automation_mode='auto'` (per-ticket opt-in, default manual): declaring a lane is simply part of switching a ticket on. `tests/test_tdd_engine_parity.py` enforces that the two paths agree.

So the automation pipeline is gated at three points:

| Transition | Gate |
|---|---|
| Backlog → WIP (dispatch) | `spec_linked` — a lane is declared |
| WIP → For Review | `commit_hash` present + `verify_passed` |
| For Review → Done (accept) | `verify_passed` at HEAD + `spec_validates` + archive |

### Where the rules live

In the **shared core** — `conditions.py` predicates and `actions.py` enforcement — which both surfaces already call. Consequences:

- An agent running headless through `tickets-cli.py` is bound by exactly the same gates as a click in the dashboard. Obligated by default, GUI never required.
- The skills (`/spec`, `/accept`) are **thin callers**. Rules are not restated as prose across `SKILL.md` files, where they would be advisory and bypassable.
- A future MCP server inherits the gates for free, with no rule duplication.

The dashboard's role is **cockpit, not intake form**: display which artifacts exist, what's blocking, and launch or resume the conversation. It never collects the spec.

### Declaring the verify command

Per project, in `WORKFLOW.toml` at the project root (read by `workflow_config.py`, which deep-merges and preserves unknown keys):

```toml
[verify]
command = "tests/run-tests.sh"
timeout_ms = 600000
```

Fallback when absent: `tests/run-tests.sh` → `package.json` `test` script → `pytest` → ask once and write the answer into `WORKFLOW.toml`.

### Predicates

| Kind | Passes when |
|---|---|
| `spec_linked` | A lane is declared: an OpenSpec change, or an explicit-and-justified lane-C claim |
| `spec_validates` | `openspec validate <change> --strict` exits 0 (vacuously true for a justified lane-C no-delta) |
| `verify_passed` | A `verified` flag exists, exited 0, and was recorded against current HEAD |
| `tests_covered` | Any of: `verify_passed`, a linked journey that compiles+validates, or explicit `no_test_required` with a rationale |

`tests_covered` gained the verify path because it was a live trap: six projects already carried it in their `Backlog → WIP` triggers while `journey_tickets` was empty and nothing set `no_test_required`, so those tickets were unsatisfiable.

### The override

```bash
python3 $CLI accept <project> <ID> --force "<reason>"
```

Records the reason on the ticket's `spec` flag, as a `gate_override` activity event, and in `PRODUCT_SPECIFICATION.md`. There is no silent bypass.

### Regenerating the adapter fixtures

`tests/fixtures/openspec/*.json` are real payloads pinning the JSON shapes the adapter parses — OpenSpec ships ~2 releases/month and self-reports inconsistent key casing. After a deliberate version bump, recapture them in a scratch project:

```bash
openspec init --tools none
openspec new change b-1-sample-capability
# write proposal.md, tasks.md, and specs/sample/spec.md
openspec status   --change b-1-sample-capability --json > status.json
openspec validate --all --strict --json --no-interactive > validate-{ok,fail}.json
openspec archive  b-1-sample-capability -y --json > archive-{ok,refused}.json
```

Then normalise absolute paths to `<PROJECT_ROOT>` and zero out `durationMs`.

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
| `for-review` → `rework` | WIP | Review found issues, ticket moves back to `## WIP` | Developer |
| `rework` → `in-progress` | WIP | Fixes started, move back to `## WIP` | Claude |
| `for-review` → `done` | Done | Review passed → `/sync` → summarize → `/accept` → clean up working files | Developer |
| `done` → `released` | Done | Shipped in a version tag | Developer |
| Any → `icebox` | Icebox | Parked for later, move to `## Icebox` | Developer |
| Any → `wont-do` | Won't Do | Decided against, move to `## Won't Do` | Developer |
| `icebox` → `proposed` | Backlog | Revived, move back to `## Backlog` | Developer |
| `bug` → `bug-fixed` | Bugs | Fix implemented | Claude |
| `bug-fixed` (all siblings done) | Parent auto-moves WIP → For Review | All bugs for parent are `bug-fixed` | Auto (dashboard skill) |
| `bug-fixed` → removed | — | Verified fixed, remove from backlog | Developer |

### Auto-Promote: Parent Returns to For Review

When a bug sub-ticket is marked `bug-fixed`, the bug stays in `## Bugs` under its parent. But if **all** bug sub-tickets sharing that parent are now `bug-fixed`, the parent ticket automatically moves from `## WIP` (Status: rework) back to `## For Review` (Status: for-review).

This ensures the parent doesn't sit in WIP after all its rework is done — it flows back to review automatically.

```
BUG-01 (Parent: B-05) → bug-fixed  ✓
BUG-02 (Parent: B-05) → bug-fixed  ✓
  → All bugs for B-05 fixed → B-05 auto-moves: WIP (rework) → For Review (for-review)
```

**Rules:**
- Only triggers when the parent is in `## WIP` with `Status: rework`
- Only triggers when ALL sibling bugs (same parent) have `Status: bug-fixed`
- Bug sub-tickets remain in `## Bugs` — they don't move with the parent

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
| `/accept {ID}` | Run `/sync` first, then `verify`, then `accept` — which gates on verify + spec validation, archives the OpenSpec change, and writes PRODUCT_SPECIFICATION.md — then clean up `docs/features/{ID}/` |
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
