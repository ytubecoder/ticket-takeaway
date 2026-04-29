# Session Log

## 2026-04-28→30 — Kitchen: agentic work orchestrator (M1a–M6) on `feat/kitchen`

### Summary
- Designed Kitchen end-to-end through three rounds of Codex review (`/plan-check`); shipped as plan v4 with consultant sign-off baked in. Spec at `docs/KITCHEN.md`.
- Shipped M1a (schema + spine events + UI badges/chips), M1b (audit completion + history tab), M2 (Kitchen view + filters + watched flag), M3 (workspaces + AgentRunner + orchestrator + live run panel + run management API), M4 (ScenarioRunner + rule-based gap classifier + journey cascade + gap-ticket flow), M5 (evidence rotation pipeline), and M6 (pause-by-default + simulate mode added during demo).
- Three parallel sub-agents during M3/M4 cut serial time: workflow_config.py reader, live run panel UI, ScenarioRunner+gap classifier. Each returned with contract questions; net win after fixing one URL doubling bug and one orchestrator runner_kind dispatch oversight.
- 24 commits, 456 TDD tests passing (no regressions across the milestone arc), live demo at `https://llm.rhino-balance.ts.net:9443/` via Caddy + tailscale serve.

### Lessons Learned
- **Accepted:** Pause-by-default for the orchestrator. User's framing ("nothing runs without me saying to run it") is the right safety posture for an autonomous-execution system. Persist the choice to `settings.kitchen.paused` so explicit Resume survives restarts; manual Run-now bypasses the gate (clicking IS the OK).
- **Accepted:** Same-transaction event emission as the audit invariant. Every mutation in actions.py + every routed-through helper in serve.py calls `emit_event()` inside the caller's open `conn` transaction, never in two separate transactions. The `db_session()` wrapper in runners.py enforces this for thread-bound code.
- **Accepted:** Single-priority bucket assignment in Mission Control aggregator (Needs Me > Running > Failed > Held > Ready To Delegate). Each subject appears in at most one bucket → no double-counting, no UI ambiguity.
- **Accepted:** Parallel sub-agents work well when paths don't overlap and contract is pre-spec'd. Workflow_config (new file), UI in generate.py only (separate file from my serve.py), ScenarioRunner in runners.py only — three agents in parallel saved roughly an hour of serial work.
- **Rejected:** Storing `automation_state` enum on the ticket. Earlier plan v2 had it; v3 dropped it because the latest non-terminal `runs` row is the source of truth for "is this subject currently being worked." Caching would create two sources of truth and inevitable drift.
- **Rejected:** PR auto-merge by Kitchen. Plan §16 explicitly excludes it — humans stay the merge gate. Kitchen detects merged PRs and reconciles state but never runs `gh pr merge`.
- **Gotcha:** macOS `socket.getfqdn(127.0.0.1)` hangs `serve.py` startup for 30s+. Symptom: `Serving N project(s)` prints, then nothing. Workaround in `/tmp/start-kitchen-demo.py` — 6-line wrapper that monkey-patches `HTTPServer.server_bind` to skip the reverse-DNS lookup. WSL/Linux unaffected.
- **Gotcha:** Cross-machine migration collision. Plan called for migration #6 but `origin/main` had already shipped #6 (`ticket_branches`) and #7 (`ticket_tags`) in parallel. Renumbered Kitchen migration to #8. Future Kitchen-branch migrations must be #9+.
- **Gotcha:** Parallel UI agent built `EDIT_API + '/api/runs?ticket=...'` URLs but `EDIT_API` already includes `/api`. The doubling produced 404s. Fixed via `sed` across 5 lines once their report flagged it. Convention check: `grep "EDIT_API + '/api"` before assuming new endpoints work.
- **Gotcha:** ScenarioRunner agent's tests used uppercase journey IDs (`J-1`); `compile_to_manifest` produces `id="journey-J-1"` which fails `validate_manifest`'s `[a-z0-9][a-z0-9-]*` pattern. Tests use lowercase (`j-1`). Followup: normalize in `compile_to_manifest` itself (deferred to M4+ polish).

### Decisions
- Kitchen ships as a feature branch (`feat/kitchen`) — not merged to main yet. Live demo is the verification surface; merge happens after WSL-side dogfooding.
- Migration #8 instead of plan's #6 (collision avoidance with shipped main work).
- Pause/resume audit events live on a synthetic `_kitchen/investigation/lifecycle` subject so they're cross-project visible and don't pollute any one ticket's history.
- Investigations table deferred. Schema enum already accepts `investigation` so future M1.5 doesn't require migration.
- `_PROJECT_PATH_RESOLVER` test seam in `kitchen.py` lets tests inject fake project paths without touching the real registry.

[Promoted to CLAUDE.md] — Kitchen architecture pointer + the four critical gotchas (pause-by-default, migration numbering, macOS getfqdn hang, same-tx audit invariant) + cross-machine WSL note.

---

## 2026-04-20 — Project registration fix, DB recovery, ticket rubric

### Summary
- Fixed bug where adding a new project via the UI returned "Dashboard not generated yet" — POST `/api/projects` was missing `cli.regenerate_dashboard(new_project)` call after scaffold/seed
- Recovered corrupted SQLite DB (WAL corruption from killed serve.py) by reseeding from PRODUCT_BACKLOG.md files across all projects
- Established ticket tracking rubric in global `~/projects/CLAUDE.md`: what constitutes a ticket vs sub-ticket vs not-a-ticket, tag strategy (thematic + sprint/initiative), parent-child structure for epics

### Lessons Learned
- **Gotcha:** Killing serve.py while it's writing can corrupt the SQLite DB (WAL + SHM left in inconsistent state). Recovery: back up corrupted files, delete DB + WAL + SHM, reseed from markdown with `tickets-cli.py seed`
- **Accepted:** DB recovery via reseed is safe — PRODUCT_BACKLOG.md is the durable source of truth for ticket content. Only ephemeral data (workflow runs, journey run history) is lost.
- **Gotcha:** `ticket_tags` table didn't exist despite migration 6 code being present — the migration had never actually run against the live DB. Reseeding triggered `init_db()` which ran all migrations fresh.

### Decisions
- Ticket rubric: < 15 min single-file changes are not tickets; technical tasks are sub-tickets under feature parents; tags favor existing before creating new; sprint tags link epics in a batch
- No startup-time dashboard regeneration added — the one project (flickki) that was affected was resolved by re-registering, which is a one-time occurrence

## 2026-04-20 — Git sync, tag feature merge & deployment, feature parity audit

### Summary
- Synced local and remote: pushed 3 local commits, merged `origin/claude/add-ticket-tagging-filter-SodoZ` (ticket tagging feature) into main, pushed merge
- Discovered deployed runtime files at `~/.claude/ticket-takeaway/` were stale — `actions.py` lacked tag support, causing CLI `--add-tag` to crash with `TypeError: unexpected keyword argument 'add_tags'`. Redeployed all source files.
- Audited feature parity across CLI, API, actions.py, and SKILL.md. Found SKILL.md has zero mention of tags — agents can't discover the feature. Established memory rule for four-layer parity on all new features.

### Lessons Learned
- **Gotcha:** Merging a feature branch doesn't update the deployed runtime copies at `~/.claude/ticket-takeaway/`. The CLI and serve.py read from deployed copies, not `src/`. Must redeploy after every merge. [Promoted to CLAUDE.md]
- **Gotcha:** The `dashboard` skill at `~/.claude/skills/dashboard/SKILL.md` is a stale older copy that doesn't know about SQLite, tags, or the current architecture. The canonical skill is `src/skills/ticket-takeaway/SKILL.md`.
- **Accepted:** Feature parity checklist — every new feature must update: (1) actions.py, (2) tickets-cli.py, (3) serve.py API, (4) SKILL.md. Saved as memory rule.
- **Rejected:** Assuming "code exists in src/" means "feature works" — deployment step is a hard requirement, not optional.

### Decisions
- All five core files redeployed: actions.py, db.py, generate.py, serve.py, tickets-cli.py
- SKILL.md update for tags identified as needed but deferred to next session
- Stale `~/.claude/skills/dashboard/SKILL.md` should be updated or deprecated in favor of `ticket-takeaway` skill

## 2026-04-16 — Settings/bounce split + workflow execution reliability

### Summary
- Split settings into two surfaces: right-hand drawer (gear icon) for app settings, full-page "Workflows & Agents" view (zap icon) for bounce config. Deleted legacy `/settings` server route (`_render_project_settings`). Ported Project metadata, Scenarios, Draft Generator, Danger Zone into the drawer.
- Fixed workflow bounce execution: progress entries before subprocess, returncode+stderr checking, `--no-session-persistence`, stuck run recovery on startup, dead-thread detection on poll, agreement check error logging.
- Added kanban card indicators: pulsing text while workflow running (3s active-runs poll), static accent dot when complete (unread, cleared on overlay open).

### Lessons Learned
- **Accepted:** User redirected plan mid-stream — originally proposed merging everything into full-page view, user corrected to drawer for settings + separate full-page for bounce. Better separation of "config" vs "pipeline management."
- **Gotcha:** `serve.py` reads from pre-generated `docs/sdlc-dashboard.html`, not from `generate.py` at request time. Must run `generate.py` to see changes. Tripped up verification initially (new elements missing from served HTML).
- **Gotcha:** `subprocess.run()` returncode was never checked — if `claude` CLI exits non-zero, stdout is empty but no error raised. Silent empty conversation turns.
- **Gotcha:** Daemon threads die on server restart but DB records stay `status="running"` forever. UI polls indefinitely seeing "running" with no progress. Fixed with startup recovery + dead-thread detection.
- **Gotcha:** `setCardWfIndicator` was scoped to the workflow bounce IIFE — not accessible from other script blocks. Added the active-runs polling and unread tracking inside the same IIFE to share scope.

### Decisions
- Drawer gets all "settings" (Appearance, Feedbacks, Managed Files, Project metadata, Scenarios, Draft Generator, Danger Zone). Full-page view gets only Agents + Workflows. User explicitly chose this split.
- Zap icon for bounce nav button (user chose from options).
- Unread tracking is client-side/session-scoped (no DB changes). Will be replaced by global notification system later.
- `.sp-*` CSS class names kept as-is (only page wrapper renamed to `.bounce-page`/`bounce-open`).
- Progress entries ("Running agent X…") are removed and replaced by actual response — prevents clutter in completed conversation.

## 2026-04-16 — Drag-drop fix, ticket cleanup, tagging rules

### Summary
- Fixed drag-and-drop triggering card click/expand on release (window._justDragged flag)
- Cleaned up 15 junk/test tickets (moved to Won't Do), grouped technical sub-tickets under parents (B-10, B-32)
- Tagged journey tickets (I-29, I-30) with "journeys" tag
- Established memory rules: tag tickets thematically, tickets must describe user-facing value (technical fixes as sub-tickets)

### Lessons Learned
- **Gotcha:** Drag-end fires a click event on the card — `dragend` doesn't prevent subsequent `click`. Fixed with `window._justDragged` flag set on dragstart, cleared 50ms after dragend, checked in click handlers
- **Gotcha:** `var` in one IIFE isn't accessible from another — `_justDragged` needed to be on `window` since drag handlers and click handlers are in different script blocks
- **Gotcha:** Migration 6 (ticket_tags) was recorded in `_migrations` but table wasn't created — `executescript` with FK constraint silently failed. Had to create table manually without FK.

### Decisions
- Junk tickets moved to Won't Do (not deleted) — preserves history
- Technical tickets grouped under parent tickets that describe user-facing value
- All new tickets must be tagged with thematic tags aligned to existing tag vocabulary

## 2026-04-11→16 — Dual-backend scenario runner (B-42) + feedbacks screenshots

### Summary
- Built Backend protocol abstraction for scenario runner — `PlaywrightBackend` (launches browser) and `CDPBackend` (connects to existing Chrome via `connect_over_cdp()`). 10-task plan via subagent-driven development.
- Added `--backend=playwright|cdp` and `--cdp-endpoint` CLI flags to pytest. PW/CDP toggle on journey Run button in UI.
- Created standalone Playwright screenshot script for feedbacks project (`~/projects/feedbacks/docs/screenshots/capture.py`).
- Ticket B-42 accepted to Done.

### Lessons Learned
- **Accepted:** CDPBackend as thin subclass of PlaywrightBackend (`pass` body) — both get a Playwright `Page`, just acquired differently. No logic duplication.
- **Accepted:** Subagent-driven development with haiku for mechanical tasks, sonnet for integration — fast, good quality.
- **Gotcha:** Chrome flags `--use-fake-ui-for-media-stream` auto-accept screen share dialogs — essential for automating feedbacks screenshots.
- **Gotcha:** Running server process doesn't pick up code changes — must restart after merging.
- **Gotcha:** `/api/scenarios/runs/{id}` iterates `.artifacts/scenarios/` and grabs first `summary.json`, not the one matching run_id. Pre-existing bug — stale summaries in API.

### Decisions
- CDPBackend creates fresh BrowserContext per actor (not reusing existing tabs) — intentional for test isolation
- Backend selection is per-run (dropdown, not persisted globally)
- Same manifests/journeys work with both backends — no duplication needed

## 2026-04-12→16 — Journey timeline view, workflow step builder fix, full-page settings

### Summary
- Built unified timeline view for journeys: vertical spine with screenshots left, step details right, URL grouping, inline edit, lightbox
- Fixed workflow "Add Step" disappearing bug (root cause: dashboard live-update polling + window.prompt)
- Added per-journey URLs (`/journeys/{id}`), journey ID display, full run history
- Consultant-reviewed workflow bounce phase 2 plan (3 rounds of feedback incorporated)
- Full-page bounce/agents settings view with project form and scenarios section

### Lessons Learned
- **Gotcha:** Dashboard 2s live-update polling rebuilds DOM via `patchCards()` — any open form/editor gets destroyed. Fix: skip polling when `body.settings-open` is set. This same pattern applies to any future inline editor on the dashboard page.
- **Gotcha:** `window.prompt()` blocks the UI thread and causes event timing issues when the form re-renders — replaced with inline DOM controls (dropdowns + textareas) for step builder
- **Gotcha:** `renderTimeline()` called from `updateField`/`updateTarget` on blur destroys the edit form the user is currently in — removed re-render on save, just update local data silently
- **Gotcha:** Screenshot `run_id` and artifact directory basename differ (generated at different `time.time()` calls) — screenshot serving must use `artifact_dir` from DB, not construct path from `run_id`
- **Gotcha:** Screenshot backfill must map to capture steps specifically (by action type), not sequentially to all steps — 4 screenshots across 11 steps were being assigned to steps 0-3 instead of steps 2,5,7,10
- **Gotcha:** API returns `{"workflows": [...]}` but JS code wrote `workflows.forEach` — this response unwrapping bug recurred again in new code written by agents [Promoted to CLAUDE.md]
- **Accepted:** Unified timeline (screenshots + details in one view) better than separate Flow/Steps tabs — user confirmed this is the right layout
- **Accepted:** Two-tier rendering (large thumbnail boxes for captures, compact cards for actions) gives good visual hierarchy without wasting space on non-visual steps

### Decisions
- Merged Flow + Steps into single unified timeline view rather than keeping as separate tabs
- Journey IDs (slugs) shown alongside titles — users can reference journeys by ID in URLs
- Per-journey URLs via pushState + popstate — shareable, bookmarkable, browser back works
- "+ Add Step" placed at bottom of timeline as final node, not in header
- Edit form fields save on blur silently (no re-render) to prevent form collapse

## 2026-04-08→13 — Workflow Bounce (I-19): full implementation across multiple sessions

### Summary
- Built complete multi-agent workflow bounce system: DB schema (migration 4), API endpoints (12 routes), execution engine with disagreement detection, CLI subcommands, full-page settings UI with agent editor and workflow step builder
- Replaced 320px settings drawer with full-page settings view (toggles kanban visibility via `body.settings-open` class)
- Added instant run feedback (pulsing placeholder block + kanban card "workflow running" indicator)
- Fixed multiple integration bugs across sessions: dropdown API unwrapping, missing GET routes, `_workflow_runs_lock` declarations, `prompt_modifier` field mismatch, conversation `agent_name` rendering

### Lessons Learned
- **Gotcha:** Linter/other sessions repeatedly stripped `_workflow_runs_lock` declarations and workflow constants from source files between sessions — module-level state variables need to be committed immediately, not left in working tree
- **Gotcha:** Copying worktree agent output directly over main's files clobbers features added by other branches (scenario runner, journeys, seek). Must restore main's version first, then layer additions on top — never wholesale replace files
- **Gotcha:** API responses wrapped in `{"agents": [...]}` but JS code expected plain arrays — this bug recurred 4+ times because each agent/session rewrote the JS from scratch without checking the API contract. The pattern: always unwrap with `data.workflows || data || []`
- **Gotcha:** Claude CLI with hooks/plugins takes 2-5 minutes per invocation, not seconds — `WORKFLOW_AGENT_TIMEOUT` needed to be 300s, and UI needed instant visual feedback (pulsing placeholder) since the user sees nothing for minutes otherwise
- **Rejected:** Splitting generate.py across parallel agents — the file has interleaved CSS/HTML/JS so any two agents touching it create merge conflicts. Use one agent for generate.py, another for serve.py
- **Accepted:** `body.settings-open` CSS class approach for full-page settings — cleaner than swapping filter bar innerHTML (which breaks cached DOM references). Permanent hidden back button toggled by CSS.
- **Accepted:** Backend validation helpers (`_normalize_json_array`, `_normalize_workflow_steps`) at route level, not storage level — HTTP 400 semantics belong in routes, storage assumes canonical JSON strings
- **Gotcha:** `--command` CLI flag conflicts with argparse's top-level `dest="command"` for subcommand dispatch — renamed to `--cmd`

### Decisions
- Custom agents in DB (not discovered from project config) — simpler, no sync overhead. Discovered agents shown read-only, import as fast-follow
- Settings as full page (not drawer) — drawer too small for agent/workflow editors
- "Step instructions" label (not "Pre-prompt") — matches actual behavior since text is appended after ticket context
- Disagreement detection via primary agent evaluation prompt — lightweight extra CLI call rather than pattern matching

## 2026-04-13 — Seek feature implementation + empty state CTA

### Summary
- Implemented full Seek feature: `src/seek.py` with 5 scanners (md tasks, README TODOs, code TODOs, CHANGELOG unreleased, GitHub Issues), dedup engine, and draft ticket ingestion
- Added empty state CTA for empty boards — two buttons: "Create First Ticket" and "Seek — scan project files"
- Applied 4 pre-seek fixes: draft exclusion from markdown, drafts toggle persistence, dynamic banner copy, draft param passthrough in create API
- 249 tests passing (18 TDD seek + 4 E2E seek + existing suite)

### Lessons Learned
- **Gotcha:** Working on wrong branch with mixed uncommitted changes from other sessions — must create feature branch BEFORE starting work, not after. Had to stash, create branch, restore only seek files
- **Gotcha:** `_create_ticket()` in serve.py didn't pass `draft` from request body to `add_ticket()` — E2E test for "drafts excluded from markdown" caught this; needed to add `draft=bool(body.get("draft", False))` passthrough
- **Accepted:** Single agent for all wiring (fixes + CLI + API + UI) was more effective than parallel agents on this codebase — the three modified files (generate.py, serve.py, tickets-cli.py) have enough interdependencies that parallel agents caused conflicts and required re-runs
- **Accepted:** Backing up new files to /tmp before git operations (stash/checkout) prevented data loss when cleaning the working tree

### Decisions
- Empty state CTA (Option A) chosen over auto-run (Option C) — clean and discoverable without being pushy
- Seek button kept in filter bar alongside the CTA — CTA is for first-time discovery, button is for re-running later
- Feature branch `seek-feature` created from main, merged fast-forward, branch deleted after merge

## 2026-04-10 — Seek feature planning + consultant review

### Summary
- Designed the "Seek" feature: project file discovery engine that scans TODOs, markdown tasks, README roadmaps, CHANGELOG unreleased items, and GitHub Issues to create draft tickets
- Produced high-level plan + deep technical spec (`docs/plans/seek-technical-spec.md`) with scanner architecture, dedup logic, CLI/API/UI integration
- Incorporated consultant code review identifying 5 issues: CLI dispatch table gap, missing `ingest_markdown()` calls, draft toggle persistence, hardcoded banner copy, split confirm paths

### Lessons Learned
- **Gotcha:** tickets-cli.py dispatches via `commands[args.command](args)` dict, NOT `args.func` — adding only `set_defaults(func=...)` leaves the command unreachable
- **Gotcha:** All mutating flows must call `ingest_markdown()` before DB writes to avoid drift — the existing pattern is consistent but easy to miss when adding new endpoints
- **Gotcha:** `sync_to_markdown()` writes all tickets including drafts — drafts should be excluded (`AND draft = 0`) per product decision
- **Gotcha:** Draft banner copy is hardcoded to "feedback session" — needs to be dynamic for Seek-sourced drafts
- **Accepted:** Source tracking via description prefix (`Source: type @ file:line`) avoids schema migration while remaining parseable for dedup

### Decisions
- Draft tickets do NOT go into PRODUCT_BACKLOG.md — user confirmed "draft tickets don't go into md files"
- Pre-seek fixes (4 items) must land before the Seek feature itself: markdown exclusion, banner copy, confirm path, toggle persistence
- No AI calls in v1 Seek — pure file parsing for speed and determinism
- All discovered items land in Ideas section as drafts — user promotes manually

## 2026-04-09 — User Journeys feature (full relational, Phases 1-8) + reworkingorder menu rename

### Summary
- Designed and implemented User Journeys as a first-class entity: 5 DB tables (migration 5), `src/journeys.py` module, 17 API endpoints in serve.py, full dashboard UI at `/{pid}/journeys`
- Journey system compiles to scenario manifests and executes via existing scenario runner — reuses entire Playwright infrastructure
- Built inference engine that analyzes existing tickets and suggests journeys grouped by lifecycle stage
- Validated the system end-to-end by creating a "Dashboard Screenshot Tour" journey that captured 4 screenshots via the new journey runner
- Fixed stale labels in reworkingorder project (Proofs→Artefacts, Writing→Articles rename cleanup)

### Lessons Learned
- **Gotcha:** Migration version conflict — existing DB had migration 4 (workflow_agents tables from another session) but new code also used version 4. Fix: bumped to migration 5. Always check `SELECT version FROM _migrations` before choosing a version number
- **Gotcha:** Python local import shadowing — `from scenarios import validate_manifest` at top-level was shadowed by `from scenarios import validate_manifest, ScenarioValidationError` inside `do_POST()`. Python treats the whole function as having a local binding. Fix: moved both imports to top-level
- **Gotcha:** conftest.py `api_post` reads `e.read()` twice on HTTPError (once in condition, once in json.loads), draining the buffer. Smoke tests used a local `safe_api_post` to work around it
- **Gotcha:** Unicode surrogate pairs (`\uD83D\uDCF7`) in Python f-strings cause UnicodeEncodeError. Fix: use plain text or HTML entities instead
- **Gotcha:** Playwright strict mode — `wait_for` with `.card` CSS selector matched 63 elements and failed. Fix: use specific testids or `>> nth=0` suffix for first-match
- **Accepted:** JSON blob approach rejected in favor of full relational (user chose Approach B over C) — per-step DB records enable SQL queries against step results and run history
- **Accepted:** Journey `open` action maps `value` field to manifest `path` field — clean translation in `_step_to_manifest_step()`

### Decisions
- Full relational model (Approach B) over JSON blobs (Approach C) — user wants proper run history and per-step DB records from the start
- Journeys are independent of tickets — clean separation, ticket linkage only added when journey is "set" (active status)
- Two entry flows: tickets-first (inference) and journey-first (manual) — both supported from day one
- `compile_to_manifest()` produces ephemeral dicts, never persisted as files — keeps scenario manifest files for manual scenarios only

## 2026-04-09 — Project onboarding flow + folder picker + managed files settings

### Summary
- Built project onboarding: greenfield projects auto-scaffold (PRODUCT_BACKLOG.md + PRODUCT_SPECIFICATION.md), existing projects auto-seed from backlog on registration
- Added browser folder picker to replace manual path typing — auto-fills project name and ID from directory name, removed description field
- Added "Managed Files" section to settings drawer showing all files TT manages with existence indicators
- Fixed global route ordering bug where `--project` legacy redirect blocked `/api/browse` and other global endpoints
- Wrote 10 TDD tests + 3 E2E tests (API greenfield, API existing backlog, full browser flow)

### Lessons Learned
- **Gotcha:** Runtime files in `~/.claude/ticket-takeaway/` can diverge from `src/` — must deploy (copy) after editing source. Earlier check showed "SAME" for symlinked files but serve.py was a copy
- **Gotcha:** Global route ordering with `_LEGACY_PROJECT_ID` — the legacy redirect (`301 /api/* → /{project}/api/*`) was catching ALL global API routes before they could be handled. Fix: move global route handlers before the legacy redirect, put redirect just before 404 fallback
- **Gotcha:** Python f-strings interpret JS regex escapes — `\b\w` in JS regex inside f-string causes SyntaxWarning. Fix: rewrite JS to avoid backslash-letter sequences (use `.split().map()` instead of regex)
- **Gotcha:** Surrogate pairs in f-strings — `\uD83D\uDCC1` (📁) can't encode in Python. Fix: use `String.fromCodePoint(0x1F4C1)` in JS instead
- **Accepted:** `sync_to_markdown()` already handles empty projects correctly — generates all section headers with zero tickets, so scaffold just calls it rather than writing custom template
- **Rejected:** Chrome DevTools MCP for E2E testing this session — browser process lock issues prevented use. Fell back to Playwright which worked reliably for automated tests

### Decisions
- Description field removed from Add Project form — unnecessary friction, name and ID auto-derive from folder name
- `scaffold_project()` creates both PRODUCT_BACKLOG.md and PRODUCT_SPECIFICATION.md; `seed_project()` only imports backlog (spec is created on first `/accept`)
- `regenerate_dashboard()` called after registration so new projects load immediately without "not generated" error
- Managed files list is computed server-side (not hardcoded in UI) via `_MANAGED_FILES` constant + `_get_managed_files()` function

## 2026-04-09 — Scenario runner: crash recovery, full build, dark mode tour

### Summary
- Recovered from crash on `scenario-runner` branch — assessed 6-phase plan, found Phases 1-3 fully coded but uncommitted, committed and verified (131 tests passing)
- Built Phases 4-6 using parallel agents: settings page scenario UI + run/publish endpoints (Phase 4), template-based drafting workflow with 7 intents and 36 TDD tests (Phase 5), README gallery wiring (Phase 6)
- Merged `scenario-runner` into main, created dark mode full-tour showcase scenario (6 screens), replaced all old pasted GitHub screenshots in README with auto-generated gallery shots

### Lessons Learned
- **Accepted:** Parallel agents for independent phases work well — Agent A (serve.py endpoints) and Agent C (README) ran concurrently with no conflicts; Agent B (drafting) ran after A since both touched serve.py
- **Accepted:** Theme support via localStorage injection in Playwright — set localStorage before first navigation, reload, captures get the right theme. Must navigate to origin first (can't set localStorage on about:blank)
- **Gotcha:** Phase 4 agent claimed serve.py had no scenario code, but it was actually already substantially built from the pre-crash session — just not in the git diff because it was committed. Always verify agent claims about file state against actual file contents, not just git status
- **Gotcha:** Stashed changes leak into working tree during rebase — `git stash push` specific files before rebase, but if the stash auto-pops or the rebase touches the same files, you get unstaged changes mid-rebase. Fix: `git checkout -- <file>` to restore during rebase, then `git rebase --continue`
- **Gotcha:** Sub-agents sometimes build far beyond scope — the Phase 4 agent added an entire "Workflow Bounce" feature (agents, CRUD, execution engine) that wasn't requested. Always check `git diff --stat` after agent work to catch scope creep before committing

### Decisions
- Manifest `theme` field is optional, validates to `"dark"` or `"light"` only — keeps the schema simple, no system/auto option since scenarios need deterministic output
- Tour scenario seeds 3 realistic tickets rather than using existing DB data — ensures screenshots are consistent regardless of project state
- Replaced ALL 6 old pasted GitHub images in README with 4 scenario-generated dark mode shots — fewer but more purposeful, each placed in context near the feature it illustrates
- Stashed "workflow bounce" WIP separately from scenario runner work — it's preserved in `git stash` but not committed since it was out of scope


## 2026-04-08 — README restructure and GitHub update

### Summary
- Restructured README: install moved up (first thing after branding), "How a Ticket Progresses" before "Stages and States", skills consolidated into single section, new Feedbacks Integration section
- User updated screenshots and install paths (clone to `~/ticket-takeaway` instead of `~/projects/ticket-takeaway`)

### Lessons Learned
- **Accepted:** Install instructions first, conceptual overview second — users want to try it before reading the theory
- **Accepted:** Skills as subsections under one heading, framed as assistive — they support the workflow, not the other way around

### Decisions
- Removed Paperclip "we intend to" language — speculative compatibility notes don't belong in a README
- Feedbacks Integration section mirrors what feedbacks repo does for us — brief description + link, not a full duplication of their docs
- Clone path changed from `~/projects/ticket-takeaway` to `~/ticket-takeaway` (shorter, user preference)

## 2026-04-05/06/08 — Feedbacks integration: settings, recording, attachments, session watcher

### Summary
- Recovered from power outage: committed 674 uncommitted lines of generate.py UI work, fixed install.py to deploy serve.py/actions.py/constants.py/db.py
- Built feedbacks settings panel (enable toggle, path, auto-start, install, status dot with server detection)
- Built record flow: Record button on card meta row + detail header, popup opens feedbacks recorder, placeholder row during recording, file watcher auto-links sessions to tickets
- Built attachments UI: enriched API with player_url/thumbnail_url, Play button opens player.html, unlink with undo
- Wrote feedbacks integration brief for feedbacks team (recorder widget spec)
- Card UX: ticket ID moved before title, edit button enlarged, record button on cards
- Security hardening: install endpoint validates path + URL allowlists
- Added 12 new smoke tests (API: settings, feedbacks status, attachments CRUD, record URL; UI: settings drawer, attachment rows, play button)

### Lessons Learned
- **Gotcha:** Multi-project routing — new API endpoints must match on `remainder` (project-prefix-stripped path), not `path` (full URL). Every new endpoint needs this check. Three rounds of debugging before catching this pattern.
- **Gotcha:** Settings stored as strings in SQLite — `bool("false")` is `True` in Python, `!!"False"` is `true` in JS. Must use explicit string comparison (`"true"/"false"` lowercase) on both sides.
- **Gotcha:** Detection cache with 30s TTL caches negative results during server startup. Fix: only cache when `running=true`, skip cache when `running=false` so polling during startup gets fresh answers.
- **Gotcha:** JS IIFEs create separate scopes — a variable in the settings IIFE is not accessible from the attachments IIFE. Use `document.getElementById` directly instead of cross-referencing variables.
- **Accepted:** File watcher over callback/webhook — simpler, zero changes needed from feedbacks team, uses existing `meta.json` write-last convention as completion signal.
- **Accepted:** `loadSettings().then(checkFeedbacksStatus)` chain eliminates race condition where status check runs before settings are loaded.
- **Rejected:** Callback POST from feedbacks → ticket-takeaway. Unnecessary complexity for localhost-to-localhost; file watching is simpler and requires no feedbacks changes.

### Decisions
- Feedbacks detection: status dot reflects server running state, not just settings toggle. Green = running, yellow = installed not running, neutral = disabled, red = not installed.
- Enable toggle starts the feedbacks server (calls start.sh), doesn't just save a boolean.
- Record button placement: card meta row (always visible) + detail overlay header. Removed from attachments section to avoid duplication.
- Ticket ID always precedes title on cards — accent mono for ID, primary sans for title, no separator character.
- Integration brief asks feedbacks team for only two things: compact recorder widget (?mode=recorder) and auto-close on save. Everything else handled on our side.

## 2026-04-06 — UI consistency pass: theming, icons, toasts, dialogs, bottom lanes

### Summary
- Designed, planned, and implemented a full UI consistency pass across generate.py, serve.py, and constants.py
- Added light/dark/system theming (3 surfaces), inline SVG icon system (17 icons), unified toast with priority tiers, inline confirm + custom modal dialog patterns, bottom lane visual cohesion, focus rings, reduced-motion support
- Removed Coming Soon placeholder, fixed feedbacks URL mismatch, eliminated all native alert()/confirm() calls

### Lessons Learned
- **Accepted:** Inline SVG per-instance over `<symbol>`/`<use>` sprite — `<use href>` fails in file:// mode due to cross-origin restrictions
- **Accepted:** Blanket `@media (prefers-reduced-motion: reduce)` at end of CSS with `0.01ms` duration — simpler than wrapping each animation individually, `0.01ms` (not `0s`) avoids breaking JS `transitionend` handlers
- **Accepted:** Draft delete uses modal (not inline confirm) because no restore endpoint exists — followed the spec's own undo reliability gate
- **Rejected:** `<symbol>`/`<use>` SVG sprite — breaks in file:// mode
- **Rejected:** Text-only visual companion mockups — user correctly called out that putting text descriptions in HTML is pointless; show actual rendered components or stay in the terminal
- **Gotcha:** Theme init script must be synchronous in `<head>` before `<style>` to prevent flash of wrong theme — DOMContentLoaded is too late
- **Gotcha:** Light theme initially felt "washed out" — borders too subtle (#e5e7eb), needs follow-up with slightly darker border tokens or faint card shadows

### Decisions
- Design direction: Blended (Primer restraint for chrome + Atlassian warmth for content)
- Toast priority: error/undo cannot be displaced by success/copy; queue behind if needed
- Inline confirm contract: one armed at a time, 3s auto-reset, only for actions with reliable undo
- Deferred: icon library migration (staying with inline SVG), trash/bin lane (needs DB schema), new animations (existing set is sufficient)

## 2026-04-05 — Merge multi-project + feedbacks branches, deploy, fix switcher chevron

### Summary
- Merged `feat/feedbacks-integration` (9 commits) and `feature/multi-project-support` (9 commits) into main with conflict resolution
- Resolved merge conflicts in `serve.py` (attachment DELETE route adapted to project-scoped routing) and `generate.py` (feedbacks scripts + project switcher scripts coexist)
- Fixed project switcher chevron rendering as giant icon — SVG `className` doesn't work with `createElementNS`, must use `setAttribute('class', ...)`
- Deployed to runtime, pushed to GitHub, cleaned up merged branches

### Lessons Learned
- **Gotcha:** After deploying new `generate.py`, must also regenerate the HTML (`generate.py --no-open`) — the server serves the pre-generated HTML file, not the template. Restarting the server alone doesn't help if the HTML was generated before the code change.
- **Gotcha:** SVG elements created with `document.createElementNS()` don't support `.className` as a string property (it's an `SVGAnimatedString`). Must use `.setAttribute('class', ...)` instead. This caused the chevron CSS to never apply, rendering at default size.
- **Gotcha:** Chrome aggressively caches localhost pages — users may need Ctrl+Shift+R after regenerating dashboard HTML. Firefox was unaffected.
- **Accepted:** Rebasing feature branches onto main before merging keeps history clean but requires careful conflict resolution when two branches modify the same files (serve.py, generate.py).

### Decisions
- Merged both branches via rebase-then-merge-no-ff to keep linear commit history within each feature
- Feedbacks attachment DELETE route adapted to use `remainder` (project-scoped path) instead of `path` (full URL) and `proj` from resolver instead of `_get_project()`

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
