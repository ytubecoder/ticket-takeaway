# Session Log

## 2026-04-04 — B-17 ticket screen AI cleanup + papercut fixes

### Summary
- Implemented B-17 (Ticket Screen AI and Layout Cleanup) — 6 child tickets across 2 phases: instant overlay open, AI response caching, DCSTL field reorder, list-style Tests/Smoke, keyboard shortcuts, editable AI diff suggestions
- Fixed papercuts: ↗ open button on kanban cards, DCSTL reorder in overlay + readiness row, removed rationale field entirely, fixed browser opening on every API write
- Fixed critical bug: `regenerate_dashboard()` was calling `generate.py` without `--no-open`, causing browser to open a new tab on every status change via the UI

### Lessons Learned
- **Accepted:** Parallel agents editing different zones of the same file works if zones are well-separated (3 agents on generate.py simultaneously — card template, overlay HTML, JS handlers)
- **Accepted:** `pushUndo` must be synchronous (before API call, not in `.then()`) — otherwise Ctrl+Z doesn't work because the undo isn't registered until the async response returns
- **Gotcha:** Python f-string `{}` vs JS `{}` — `_assessCache = {}` broke the f-string parse. Must be `{{}}` for empty JS objects inside f-strings
- **Gotcha:** `regenerate_dashboard()` shells out to `generate.py` which always opens the browser — needed `--no-open` flag to prevent this in API context
- **Rejected:** Using agents to implement small JS edits — they returned plans instead of making edits. Better to implement directly for surgical changes

### Decisions
- Rationale field permanently removed (DB column preserved inert, all code paths stripped)
- DCTRS reordered to DCSTL: Description, Criteria, Smoke, Tests, Learnings
- "Review" renamed to "Learnings / Sync" throughout — data key `reviewed` unchanged
- AI assessment cache is JS in-memory (not DB-persisted) — invalidated on ticket data change, force-refreshable via Re-assess button

## 2026-04-03 — B-16 review, fix post-refactor test imports, accept

### Summary
- Reviewed B-16 (Test Framework) — verified all 8 acceptance criteria, ran 72 tests green
- Fixed conftest.py: added `src/` to sys.path so `from constants import ...` works after B-12 module extraction
- Fixed test_tdd_helpers.py: import `auto_generate_id` from `actions` directly (no longer on `tickets-cli.py`)
- Accepted B-16 → Done
- Updated `/sync` skill with context scan step for capturing product decisions and learnings

### Lessons Learned
- **Gotcha:** After B-12 extracted `constants.py`/`actions.py`/`db.py` from monolithic `tickets-cli.py`, test conftest broke because importlib-loaded modules don't inherit the `sys.path` manipulation that `tickets-cli.py` does internally. Fix: add `src/` to `sys.path` in conftest before loading modules.
- **Gotcha:** Functions that move between modules during refactors (e.g. `auto_generate_id` from `tickets-cli.py` → `actions.py`) may still appear to exist via re-export but actually don't — `tickets-cli.py` imports `move_ticket` from `actions` but not `auto_generate_id`, so it's not available on the loaded module.
- **Accepted:** Review workflow caught real breakage from a prior refactor (B-12) — tests that passed when written had silently broken due to module restructuring. The `/review` process surfaced this before acceptance.

### Decisions
- Undo/redo criterion in B-16 accepted as skip — covered by separate I-08 ticket, not a gap in test framework
- `/sync` skill updated with explicit "context scan" step to systematically mine conversations for decisions, learnings, corrections, and architectural insights before writing session log

## 2026-04-03 — Accept I-07, unify column/section into section only

### Summary
- Accepted I-07 (UI Inline Editing with Field-Level Updates) — all 3 phases delivered via sub-tickets B-06, B-07, I-08
- Eliminated `column` as a separate concept — `section` is now the single term for kanban placement
- Renamed constants: `SECTION_TO_COLUMN` → `SECTION_SLUGS`, `COLUMN_TO_SECTION` → `SLUG_TO_SECTION`, `CARD_CLASS_BY_COLUMN` → `CARD_CLASS_BY_SLUG`
- Removed `column` field from Ticket dataclass (replaced with `slug` property), DB schema (migration 2), and API response
- Updated all HTML/JS from `data-column`/`dataset.column` to `data-section`/`dataset.section`
- Updated `auto_promote_parents()` to use section names instead of slugs as dict keys

### Lessons Learned
- **Accepted:** Using `replace_all=true` on Edit for simple renames (e.g. `dataset.column` → `dataset.section`) was efficient but missed cases where the surrounding context differed slightly — always follow up with a grep sweep
- **Gotcha:** Python f-string references to renamed parameters inside multi-line f-strings are easy to miss — the `_render_list_rows` child rendering had a stale `column` reference that only showed up at runtime, not in grep for `t.column`
- **Accepted:** SQLite 3.35+ supports `ALTER TABLE DROP COLUMN` directly — no need for the create-copy-drop-rename dance

### Decisions
- CSS class `.column` (kanban layout term) stays unchanged — it's a visual/layout concept, not data model
- `by_column` dict renamed to `by_section` with section name keys ("WIP", "For Review") instead of slug keys ("wip", "review") — more consistent with the single-term philosophy
- Slugs remain as utilities (CLI aliases, CSS class lookups, HTML data attributes) but renamed from "column" to "slug"

## 2026-04-02 — B-16 test framework: smoke, E2E journey, TDD

### Summary
- Built 3-category test framework with 72 tests across 8 new files (48 TDD, 18 smoke, 6 E2E)
- Extracted `auto_promote_parents()` from `generate.py` inline code into standalone testable function
- Extended `conftest.py` with CLI/generate module imports via importlib, `live_page` fixture, shared API helpers
- Discovered card single-click expands in-place; detail overlay requires `window.openDetailOverlay()` or double-click

### Lessons Learned
- **Accepted:** Importing `tickets-cli.py` (hyphenated filename) via importlib works cleanly for TDD — no need to refactor filenames. Pattern already used in `serve.py`.
- **Accepted:** Testing `auto_promote_parents` required extracting it from the 3000-line `generate_html()` function. The extraction was trivial (pure function, no side effects beyond list mutation) and improved code structure.
- **Gotcha:** Card click opens in-place expansion (200ms debounce timer), NOT the detail overlay. Detail overlay opens via `window.openDetailOverlay(tid)` or double-click on card ID. Browser tests that need the overlay must use the JS API, not `card.click()`.
- **Gotcha:** Detail overlay description textarea is not visible until the overlay fetches ticket data async — must `wait_for_function` on `ta.value !== ''` before asserting content.
- **Gotcha:** 5 pre-existing failures in `test_gate_hash_state.py` — tests reference tab-based hash routing (`detail-tab.active`) that was replaced by single-card overlay design. Not regressions.

### Decisions
- AI-dependent endpoints (gate-check, assess, enrich) excluded from smoke tests — require API keys
- Undo/redo E2E test marked as skip — not implemented in detail overlay yet
- Browser edit test changed to API-edit-then-verify-in-dashboard pattern (more reliable than manipulating hidden textareas)

## 2026-04-02 — Redesign ticket detail overlay: tabs → single scrollable card

### Summary
- Replaced 6-tab ticket detail overlay with a single scrollable card layout — all DCTRS sections + rationale visible at once
- Added meta strip with clickable chips (priority cycles, status dropdown, complexity cycles, parent inline-edit, column badge)
- Made title contenteditable in header, DCTRS dots scroll to sections, all textareas auto-save on blur
- Removed all Save buttons, tab switching, Properties form, and Create New/Review Existing button pairs
- Replaced criteria checkboxes with bullet + inline-editable text + × delete button + Enter-to-add input
- Collapsed two assess buttons per section into single "Assess"/"Re-assess" ghost button (visible on hover)
- Accepted B-09 (Column Move Gate Check) + all 4 children (I-12, I-13, I-14, I-15)
- Fixed bottom list sections (Done, Bugs, Icebox, Won't Do) to show newest tickets first

### Lessons Learned
- **Accepted:** Auto-save on blur eliminates Save buttons entirely — reduces mouse clicks and cognitive load. Toast confirms save happened.
- **Accepted:** Single "Assess" button that auto-detects create vs review (based on content presence) removes a decision the user shouldn't need to make.
- **Accepted:** Criteria are a spec (accept/reject/edit), not a todo list (check/uncheck). Checkboxes were the wrong affordance.
- **Gotcha:** When replacing tabs with stacked sections, `scrollToSection()` needs to account for the fixed header offset — using `el.offsetTop - body.offsetTop` for correct scroll position.

### Decisions
- Criteria checkboxes removed per user feedback — criteria are specification items, not completion trackers
- Shift+click on Assess copies prompt to clipboard (power-user fallback) — preserved from old UI but hidden
- Hash routing changed from `#ticket/{id}/{tabName}` to `#ticket/{id}/{flagLetter}` with backward compat map

## 2026-04-02 — Fix drag-and-drop column highlighting

### Summary
- Fixed drag-drop highlight to cover entire column (header + body + empty space), not just the card container area
- Moved drag event listeners from `.column-body` to `.column` element and updated section lookup logic
- Changed kanban container from `align-items: flex-start` to `align-items: stretch` so columns fill full available height

### Lessons Learned
- **Gotcha:** `align-items: flex-start` on the kanban flex container made columns only as tall as their content — dead space below cards wasn't part of any element, so drag events couldn't fire there. `stretch` makes columns fill the container height.
- **Accepted:** Attaching drag listeners to the `.column` wrapper (not `.column-body`) means the header is part of the drop zone too, giving a much clearer visual indication of the target.

### Decisions
- Column highlight uses accent border-color + box-shadow glow (consistent with card drag-target style) rather than dashed border — looks cleaner on the full column

## 2026-04-02 — New ticket creation UI + overlay panel fix

### Summary
- Added "+ New" button to filter bar (edit-mode only) that opens an inline creation panel
- Panel has title input, section dropdown (Idea/Backlog/WIP/Bug), and Create button
- Added expandable "Full ticket form" toggle with "Coming soon" placeholder
- Fixed panel to use `position: absolute; top: 100%` overlay instead of pushing kanban down (user feedback: layout shift felt jarring)

### Lessons Learned
- **Accepted:** Overlay panels (absolute positioning) are better than inline panels for transient UI that shouldn't shift the main content — user called out the "jilting" layout shift immediately
- **Accepted:** Nesting the absolute panel inside the sticky filter-bar div gives correct anchoring without needing a separate wrapper element

### Decisions
- "Full ticket form" shows "Coming soon" — user will define the full ticket creation screen next
- Enter key in title input triggers create (keyboard-friendly)

## 2026-04-02 — Codify 3-layer hierarchy + readiness detail view (B-08)

### Summary
- Formalized the 3-layer hierarchy (Section/Status/Readiness Flags) in `docs/LIFECYCLE.md` Sections 3b and 4b
- Implemented readiness content storage: added `content` column to `readiness_flags` DB table, with markdown sync (`Tests:`/`Reviewed:`/`Smoke:` labeled lines)
- Built full ticket detail overlay — clicking any D/C/T/R/S dot opens a tabbed editor with Save buttons and clipboard prompt buttons (Create New / Review Existing)
- Added `PUT /api/tickets/<id>/readiness/<flag>` endpoint; auto-fill semantics (non-empty content fills dot, empty clears it)
- Updated `_get_ticket_json` to return readiness_flags as `{flag: content}` dict + `criteria_text`

### Lessons Learned
- **Accepted:** All 5 readiness dots now have `data-flag` attributes (not just T/R/S) — consistent click handling opens the detail overlay for any flag
- **Accepted:** Using `ON CONFLICT ... DO UPDATE` for readiness content upserts keeps the toggle and content-save paths unified
- **Gotcha:** The `cmd_seed` function had its own insert loop separate from `_ingest_markdown_changes` — readiness content roundtrip required adding insert logic to both paths

### Decisions
- Review (R) defined as a qualitative checkpoint: collective /sync output, decisions, bugs, feature implications — distinct from mechanical T and S flags
- Readiness content stored in same `readiness_flags` table (added `content` column) rather than a separate table — keeps schema simple
- Detail overlay uses a separate `<script>` block with its own `EDIT_API` lookup, independent from the main script — avoids coupling with existing click handlers

## 2026-04-02 — Redesign dashboard filter bar (cross-cutting multi-select filters)

### Summary
- Replaced redundant column-mirroring filters (All/Backlog/WIP/Review/Ideas) with cross-cutting filters: Status (Proposed/In Progress/For Review), Type (Bug), Size (S/M/L)
- Implemented multi-select filter logic: OR within groups, AND between groups, composable with search
- Added `data-status`, `data-complexity`, `data-is-bug` attributes to all card rendering locations (kanban cards, parent list rows, child list rows)
- Updated live-update state save/restore to handle multiple active filters

### Lessons Learned
- **Accepted:** Card-level filtering (show/hide individual cards) is more flexible than column-level filtering (show/hide columns) — all columns stay visible so the kanban layout context is preserved
- **Gotcha:** `generate.py --all` required to regenerate dashboards for all registered projects; default auto-detects from cwd and only generates for one project. serve.py defaults to first registry entry (GoodForm), so both projects need regeneration for changes to be visible.

### Decisions
- Filter groups use visual dividers (1px lines) between them rather than labeled sections — keeps the bar compact
- "All" button auto-activates when no other filters selected; clicking it clears all active filters
- Search composes as an additional AND constraint on top of filter state

## 2026-04-01 — Cross-package feedbacks integration (ticket-takeaway + feedbacks repos)

### Summary
- Designed and implemented cross-package integration between ticket-takeaway and feedbacks
- Moved feedbacks skill into feedbacks repo (`skills/feedbacks/SKILL.md`) so it ships from the package
- Created wrapper skill in ticket-takeaway (`src/skills/feedbacks/SKILL.md`) — superset that adds ticket-linked output dirs and context push
- Updated `/review` skill with formalized feedbacks steps (4a: check prior sessions, 1b: offer visual capture)
- Updated `install.py` to deploy the wrapper skill alongside existing skills

### Lessons Learned
- **Accepted:** One-way dependency pattern (orchestrator → tool, never reverse) keeps both packages independently usable. Feedbacks has zero awareness of ticket-takeaway.
- **Accepted:** Wrapper-as-superset pattern for skills — when two packages ship to the same skill path, the more feature-rich one overwrites the base. Avoids cross-skill invocation complexity.
- **Accepted:** Feedbacks owns its output location; ticket-takeaway controls where sessions land only when it initiates the capture (via `FEEDBACKS_OUTPUT_DIR`). Standalone captures stay in feedbacks' native `sessions/` dir.

### Decisions
- Integration is one-way: ticket-takeaway → feedbacks, never the reverse
- `/feedbacks` standalone does NOT auto-create tickets — it pushes context and the agent decides
- `/review` is the orchestrator for ticket-linked sessions (sets output dir, analyzes, enriches bug sub-tickets)
- Both repos deploy to `~/.claude/skills/feedbacks/` — ticket-takeaway's wrapper wins when both installed
- Design spec saved to `docs/superpowers/specs/2026-04-01-feedbacks-integration-design.md`

## 2026-03-31 — B-05 live dashboard updates + DB single source of truth fix

### Summary
- Implemented B-05: replaced full-page-reload polling with in-place DOM diffing in generate.py (~130 lines JS)
- Added card-enter (fade-in), card-exit (fade-out), content-flash, and just-moved animations
- Fixed critical data integrity issue: generator no longer merges PRODUCT_SPECIFICATION.md tickets into dashboard
- Fixed `ingest_markdown` deleting DB-only tickets during sync (was silently dropping records)
- Seeded 9 missing GoodForm tickets (R-17–R-24, BUG-09) into DB from PRODUCT_SPECIFICATION.md

### Lessons Learned
- **Gotcha:** `ingest_markdown` had a "delete DB tickets missing from markdown" step (line 527-531) that destroyed DB-only records on every sync. When DB is the source of truth, the deletion direction must be reversed — only the CLI should delete tickets.
- **Gotcha:** `generate.py` was merging PRODUCT_SPECIFICATION.md items into the ticket list, creating phantom tickets not tracked in the DB. This caused count mismatches (36 in DB vs 46 in dashboard).
- **Accepted:** Using `_bound` flag on DOM elements to prevent double-binding event listeners after DOM patching. Cards that get content replaced reset `_bound = false` so `rebindCardListeners()` re-attaches handlers.
- **Accepted:** `textContent` comparison (not innerHTML) for change detection avoids false positives from HTML serialization differences.

### Decisions
- DB is the single source of truth. PRODUCT_SPECIFICATION.md is write-only output (from /accept), never read by the generator.
- Skipped R-25 (duplicate of R-24) when seeding missing tickets.
- Schema-version meta tag bumped to "2" — old dashboards fall back to full reload once, then get the new diffing behavior.

## 2026-03-30 — Optional feedbacks integration for /review skill

### Summary
- Added feedbacks integration hooks to the `/review` skill (step 4a: surface prior sessions, step 1b: offer visual capture)
- Updated CLAUDE.md with Feedbacks Integration section documenting conventions
- Added `.feedbacks/` to .gitignore

### Lessons Learned
- **Accepted:** Detection-not-dependency pattern — check for `~/projects/feedbacks/start.sh` and silently skip if absent. Keeps both projects fully standalone.
- **Accepted:** Feedbacks SKILL.md already has ticket-takeaway awareness (outputs to `.feedbacks/{ticket-id}/`), so ticket-takeaway just needed to look for those directories during review.

### Decisions
- Integration is purely at the skill layer (review SKILL.md), not in Python code — keeps CLI and generator feedbacks-agnostic
- `.feedbacks/` is gitignored per-project since sessions contain screenshots and transcripts

## 2026-03-29 — SQLite migration, install script, read-before-write sync

### Summary
- Migrated source of truth from PRODUCT_BACKLOG.md to SQLite (tickets.db)
- Created tickets-cli.py with seed, list, add, update, move, accept, sync subcommands
- Added read-before-write sync: direct markdown edits absorbed into DB before each CLI write
- Created install.py for one-command install/upgrade/register
- Generalized parent-child tickets (any type, not just bugs) with smart labels
- Updated all skill files, README, INSTALL.md for new architecture
- Released as v0.2.0

### Lessons Learned
- **Gotcha:** Two copies of generate.py exist (`~/.claude/ticket-takeaway/` and `~/.claude/dashboard/`) with different DASHBOARD_DIR — install.py patches this automatically now
- **Accepted:** Read-before-write pattern (ingest markdown → apply CLI change → write back) solves the race condition where agents edit markdown directly while CLI also writes
- **Accepted:** Separate tables for acceptance_criteria and depends (not JSON columns) enables clean querying and ordering
- **Gotcha:** Won't Do section name has an apostrophe that breaks SQL string literals — must use `''` escaping
- **Rejected:** Telling agents "never edit PRODUCT_BACKLOG.md" — agents will self-improve and edit files. Better to absorb their edits gracefully.

### Decisions
- SQLite DB is source of truth but markdown edits are absorbed (cooperative, not locked-down)
- PRODUCT_SPECIFICATION.md stays as plain markdown (not in DB) — it's an append-only archive
- install.py always upgrades system files but preserves registry.json and tickets.db
- v0.2.0 is a major release with breaking change to architecture

## 2026-03-28 — Parent-child bug ticket rendering overhaul

### Summary
- Implemented nested bug sub-tickets under parent cards (filtered from standalone columns, rendered inline on expand)
- Added auto-promotion: parents move to For Review when all child bugs are resolved
- Converted bottom sections (Done, Bugs, Icebox, Won't Do) from card grid to compact list rows
- Made linked bug cards individually clickable with their own double-click clipboard prompts

### Lessons Learned
- **Gotcha:** The `/dashboard` skill runs `~/.claude/dashboard/generate.py`, not `~/.claude/ticket-takeaway/generate.py` — changes must be deployed to both locations
- **Accepted:** Filtering parented tickets out of column lists then rendering them only inside parent cards is clean and avoids duplication
- **Accepted:** Adding `class="card"` to list rows lets them inherit existing JS click handlers without duplicating event binding code

### Decisions
- Child tickets with `Parent:` field never appear as standalone cards anywhere — they only render nested under their parent
- Bottom sections use a distinct list-row style to visually separate them from the kanban board cards
- Linked bug cards have red-tinted borders/background to stand out from parent card content
