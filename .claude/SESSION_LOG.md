# Session Log

## 2026-05-12 — PWA shell + /kitchen attention-feed redesign

### Summary
- Shipped PWA installability for the dashboard: `manifest.webmanifest`, `sw.js`, and brand-color icons (180/192/512 PNG + SVG) served at site root. SW is network-first for navigations, never touches `/api/*`. New shared `PWA_HEAD_TAGS` constant in `serve.py` is injected into every top-level page renderer (kanban, projects picker, kitchen, workflows, journeys, ticket detail). A `@media (max-width: 760px)` block in the kanban CSS stacks columns vertically with 36-44px tap targets and a `100dvh` fullscreen detail overlay with safe-area insets.
- Auto-collapse the nav rail on phone widths after the user taps any rail item — was leaving the expanded rail overlaid on top of the destination page. JS sets `localStorage['tt-rail-expanded']='0'` plus removes the body class before the anchor navigates, so the next page paints with the rail collapsed.
- Redesigned `/kitchen` as a single-column "attention feed" (replacing the bucket-stacked orchestrator view). New `src/kitchen_feed.py` (data layer) returns a frozen payload — paused flag, totals, projects, flat items list — with time buckets keyed on `tickets.updated_at` and an unread heuristic (24h + actionable status). New `src/kitchen_view.py` (renderer) builds the HTML. `_render_kitchen_view()` in `serve.py` shrinks to a thin wrapper. New `GET /api/kitchen/feed` for 5s client polling. `/kitchen/demo` renders the same view against a baked-in stub state for visual review; cards there are click-inert with an explanatory banner since the IDs don't exist in the live DB.
- Iterated the card design five passes: original colorful tile + glyph → ticket IDs + section badges + per-bucket glyphs → user feedback "too colorful, too many pictorials" → simplified to thin left-border for bucket + ticket ID + neutral section badge + plain "Running" / "Needs me" / etc. text inline; chip color tinting reverted; Live/Paused indicator surfaced as a header pill (always visible, was previously buried under the "⋯" menu).
- Project filter relocated from a chip row above the feed into a checkbox list under the "⋯" overflow menu (multi-select, all-checked default, "All / None" toggle). Overflow icon gets a small accent dot when any project is unchecked so the filter state is visible without opening the menu.
- Memories updated: `feedback_branch_per_feature.md` extended with the merge-defaults pairing + the 2026-05-11 pile-on incident; new `feedback_check_tip_before_admin_merge.md` codifies fetch-and-verify before any admin-merge.

### Lessons Learned
- **Accepted (architecture):** Splitting the new Kitchen view into `kitchen_feed.py` (pure data) + `kitchen_view.py` (pure renderer) let two parallel agents work without file conflicts. The frozen payload schema between them — `paused`, `totals`, `projects[]`, `items[]` with `bucket`, `time_bucket`, `is_unread`, `section` — became the integration contract. `_render_kitchen_view` in `serve.py` collapsed to 20 lines (was ~575). Same shape should work for any other multi-piece UI: define the payload first, write data + view in parallel, integrate.
- **Accepted (UX):** Bucket-tinted thin left border is the right amount of color cue. The user explicitly rejected the bigger tile + glyph pictorials; left-border keeps the run-state legible without dominating the card.
- **Accepted (UX):** Surfacing system Live/Paused in the header as a clickable pill — rather than hiding it under the `⋯` overflow — was an explicit user ask and clearly correct. State that drives behavior should never need a click to reveal.
- **Accepted (process):** Running serve.py from this Mac with `HTTPServer.__init__` monkey-patched to bind `0.0.0.0` instead of `127.0.0.1` lets the user hit `http://llm.rhino-balance.ts.net:8799/kitchen/demo` from any tailnet-connected device for live preview. Faster than the pull-on-WSL-then-restart loop. Pairs with the existing macOS `getfqdn` workaround.
- **Rejected (UX):** Per-state chip color tinting + per-section pill color tinting + bucket glyphs all at once. The user called it out as too colorful; reverting to one color cue per concept (left border for bucket, neutral text for bucket label, neutral pill for section) felt much cleaner.
- **Rejected (UX):** Card href using `?ticket={id}` — the kanban only listens for `#ticket/{id}` hashes (`generate.py::_parseTicketHash`), so the query form just opened the kanban without the detail overlay. Always use the hash form when deep-linking to a ticket from outside the kanban.
- **Gotcha:** A new page renderer that omits `gen.build_nav_rail_css()` produces a left rail whose menu items render as unstyled inline anchors. The rail JS injects the items into `#navRail`, but without the CSS they look broken. Caller must pass rail_css + rail_html + rail_js into any composing function. Codified in CLAUDE.md.
- **Gotcha:** Polling on `/kitchen/demo` replaces the demo stub items with the empty live feed after the first 5s tick. Fix: JS checks `location.pathname === '/kitchen/demo'` and skips polling. The demo route is intentionally not backed by a corresponding stub-aware `/api/kitchen/feed` because that would complicate the contract.
- **Gotcha (recovered):** PR #10 was admin-merged at commit `20f91ef` while I was still pushing `19a35da` to `feat/pwa-mobile`. GitHub auto-deleted the source branch on merge, orphaning my last commit. Recovered via `git cherry-pick 19a35da` onto main. Lesson is the new check-tip memory.

### Decisions
- **`/kitchen` is the new home of cross-project triage.** The orchestrator controls (pause/resume) move to a header pill + overflow panel inside the new view. The bucket-stacked legacy view is gone. The same URL, replaced content.
- **One responsive layout** for /kitchen, not a mobile fork. `@media (max-width: 760px)` only widens cards and centers the column on desktop; mobile-first design works at every width.
- **Demo cards are inert.** The `/kitchen/demo` route renders the full populated state but clicks preventDefault + show a banner explaining the cards are mockup. Avoids ghost-ticket detail overlays when the demo IDs don't exist in the live DB.
- **PWA start_url stays `/` (redirects to /projects)** for now — not auto-routing to /kitchen on PWA-standalone launch. Deferred until the user lives with the feed long enough to decide.

## 2026-05-11 — Activity tab redesign + ticket origin provenance + Add-Project modal

### Summary
- Redesigned the Activity tab from a card list to a 5-column grid (time | badge | actor | summary | chevron). Added a top filter bar with multi-select event-type chips (Created/Moved/Status/Criteria/Run/Hook/Workspace/Field/Input/Pause) and single-select date-range presets (1h/24h/7d/All). Each row shows both absolute (`HH:MM:SS` for today, `Mon DD HH:MM` for older) and relative (`Nm ago`) timestamps. Click a row to expand inline detail showing event_kind, actor + actor_id, full ISO timestamp, optional run-link, and pretty-printed payload JSON. New `EVENT_KIND_GROUPS` / `EVENT_GROUP_ORDER` / `EVENT_GROUP_COLORS` maps in `constants.py` consumed by the chip filter and badge styling.
- Added centralised `ticket_created` event emission in `actions.add_ticket` with an `origin` payload field. Origins: `human` (UI/CLI), `agent` (when `actor.actor_type == 'agent'`), `seek` (with source_type/file/line metadata via `seek.py`), `seed` (via `tickets-cli.seed_project` for tickets imported from PRODUCT_BACKLOG.md), `markdown_edit` (via `tickets-cli._ingest_markdown_changes` when an external editor adds a ticket directly to the markdown), `journey_gap` (via the file-gap-ticket path in `serve.py`), and `backfill` (via migration 19 for tickets that predate provenance tracking). Callers that want a richer payload pass `emit_created_event=False` to `add_ticket` and emit their own. Migration 19 backfills synthetic events for older tickets, recovering seek-origin from the description's `Source:` prefix.
- Added agent display name resolution in `actions.get_ticket_activity`: for events with `actor_type='agent'`, batch-joins `runs.metadata_json.workflow_name` (kitchen runs, integer ids) or `workflows.name` (workflow-bounce runs, UUID ids). The Activity tab renders agent rows as `→ <name>` in accent color instead of a generic "agent" label, with a hover tooltip showing the run id.
- Converted the Add-Project flow at `/projects?new=1` from an inline form below the project grid to a centred modal overlay (close × button, click-outside-to-close, Escape, autofocus on Name input). Same modal opens from both the header `+ New` button and the rail switcher's "Add new project" link.
- Two pre-existing JS parse errors fixed in the activity-tab `<script>` block, both around inline `onclick` attributes inside JS string literals. The Python triple-quoted f-string was converting `\'` to literal `'`, which then closed the JS string early and produced "Unexpected identifier" errors that killed the entire script — meaning the Activity tab was broken from the day it shipped. Switched the inner quotes to `&apos;` HTML entities so the browser decodes them inside the attribute at parse time.
- Fixed Seek button getting stuck in `.active` state after click — the generic `.filter-btn` click handler in `generate.py` was toggling `.active` on it. Added a guard that skips buttons without a `data-filter` attribute. Improved Seek toast messages to distinguish "N draft(s) created" vs "All N items already tracked — nothing new" vs "No new ticket-like items found" vs "Seek failed: <error>".
- Promoted two architectural rules to CLAUDE.md gotchas: ticket creation must always emit `ticket_created` with origin metadata; inline `onclick` inside JS string literals must use HTML entities for nested quotes (no backslash escapes in triple-quoted Python f-strings emitting JS).

### Lessons Learned
- **Accepted (architecture):** Centralising `ticket_created` emission in `actions.add_ticket` with a `suppress` flag is the right shape — beats sprinkling emit_event calls at every creation site, and the suppress flag lets callers (seek, seed, journey-gap) emit a richer payload without producing duplicate events. The single-place emit also means future creation paths can't forget the event.
- **Accepted:** Activity-tab grid uses CSS `display: contents` on the row wrapper so each row's child cells participate in the parent grid's column layout while still being a single click target. Lets us bind the click handler at the wrapper level + still get column alignment across rows. Empty-state cell uses `grid-column: time / -1` to span all five columns.
- **Accepted:** Date-range filter is single-select chip group (1h/24h/7d/All) rather than a date picker. Covers >95% of "what changed recently" lookups; users who need exact bounds can still expand individual rows for ISO timestamps. Custom range deferred until anyone asks.
- **Accepted (user-confirmed pattern):** Agent display names rendered in accent color with a `→` prefix, vs uppercase muted `HUMAN`/`SYSTEM` labels. Visually disambiguates agent-attributed mutations from human edits at a glance.
- **Rejected:** Adding an Actor filter chip row alongside Type and Date. User explicitly skipped it when offered — type + date covers the common case; filtering by actor is rare enough that the per-row text is sufficient.
- **Rejected:** Free-text search inside the activity feed. Same reason — payload search is power-user; type + date filtering already collapses the feed enough.
- **Gotcha:** `serve.py` reads `docs/sdlc-dashboard.html` as a static file for the kanban route; UI changes in `generate.py` don't take effect until you regenerate the dashboard for *each affected project* (not just running `python3 generate.py` from the TT repo, which only regenerates TT itself). Use `cd ~/projects/<other-project> && python3 ~/.claude/ticket-takeaway/generate.py --no-open` per project, or trigger any API write that calls `regenerate_dashboard()`.
- **Gotcha:** `~/.claude/dashboard/generate.py` (legacy fallback path) doesn't have `constants.py` co-located, so `python3 ~/.claude/dashboard/generate.py` fails with `ModuleNotFoundError: No module named 'constants'`. The correct path is `~/.claude/ticket-takeaway/generate.py`. The fallback exists for very old installs; modern deploys should always use the ticket-takeaway path.
- **Gotcha (recovered):** Inline `onclick` attribute inside a JS string literal that uses `\'` for the inner quotes is broken — Python's triple-quoted f-string emits a real single-quote that closes the outer JS string. Promoted to CLAUDE.md.
- **Gotcha (process):** I committed unrelated work (activity provenance) onto an in-flight feature branch (`feat/pwa-mobile`) rather than cutting a fresh branch off main; the existing `branch_per_feature` memory exists specifically to prevent this. Then I admin-merged PR #10 without `git fetch origin <branch>` first, which orphaned a concurrent push from another agent (`19a35da`); recovered via `git cherry-pick` to main. Two memories now exist: `feedback_branch_per_feature.md` (extended with the pile-on incident) and `feedback_check_tip_before_admin_merge.md` (new). Future flow: branch-off-main BEFORE the first commit, fetch-and-compare-tip BEFORE admin-merge.

### Decisions
- **Activity timeline UX is now the canonical pattern for chatty event feeds** in TT (run history, journey runs, etc. should follow the same grid + chip-filter shape if/when those grow beyond 10 rows). The grid is denser, scannable, and click-to-expand keeps the default view tight without losing access to detail.
- **`origin` payload field on `ticket_created` events is the source of truth for ticket provenance.** Description-based heuristics (e.g. parsing `Source:` prefix) are only used by migration 19 backfill; new tickets must carry origin in the structured payload. New creation paths must add a render branch in both the JS `_eventSummary` and the Python `_ticket_created_summary` helper.
- **Add-Project modal pattern (`.add-modal-backdrop` overlay + close mechanisms) is reusable** for any other "add new resource" flow on the projects picker page. Z-index 900 for the backdrop, 1100 for nested folder-picker.
- **Cherry-pick from reflog is the recovery for orphaned commits** when a source branch is auto-deleted by GitHub on PR merge. The lesson is to prevent it (fetch + verify tip), not to make the flow tolerate concurrent pushes.

## 2026-05-10 — Migrate canonical writer to llm-node + origin-relative API base fix

### Summary
- Migrated the canonical TT writer from WSL to the macOS llm-node. Atomic `sqlite3.backup()` of WSL's live DB → `scp` to llm's runtime path → `pkill` + relaunch llm's serve.py. Verified all 277 tickets, all 17 ticket-takeaway journeys, migrations 1–18, the 6 system agents and 10 system workflows on llm. Stopped WSL serve.py; llm is now the only writer.
- Fixed a bug that surfaced as soon as we accessed llm via the public Tailscale URL (`https://tt.rhino-balance.ts.net/ticket-takeaway/journeys`): five `_render_*_page()` functions plus the Kitchen run-detail panel hardcoded the client-side API base as `http://localhost:{port}/{pid}/api`. JS in the user's remote browser fetched against *its own* localhost, not the serve host. Switched all six sites to origin-relative `/{pid}/api` so the browser resolves against whatever hostname loaded the page. Promoted the rule to CLAUDE.md as a load-bearing convention for future page renderers.
- Built `src/diff_dbs.py` — read-only row-by-row comparison of two `tickets.db` files (tickets, criteria, tags, branches, agents, workflows, automation_subjects, settings; counts-only for runs/activity_events). Used as a diagnosis tool when claude-sync is paused; not a merge tool.
- Pre-migration audit: confirmed via `compare_seed_to_db.py` that `agent_planner` + `agent_consultant` were `system=0` in DB despite being seeded — fixed the seed (`system: 1` for both). After serve restart, audit shows zero drift.
- Hot-fix workflow validated: scp source straight to llm runtime + restart bypasses the git round-trip, useful for live diagnosis. Followed up with commit + push + pull + redeploy on llm so the four locations (WSL source, WSL runtime, llm source, llm runtime, origin/main) all converge on the same checksum.

### Lessons Learned
- **Accepted (architectural rule):** Page renderers always use origin-relative API base URLs, never `http://localhost:{port}/...`. The convenience default breaks under any proxy/tunnel/port-forward in ways that are invisible until someone hits the page from a non-localhost browser. The cost of always being origin-relative is zero. Promoted to CLAUDE.md.
- **Accepted:** PRODUCT_BACKLOG.md is the git-mergeable text form of the tickets table — DB-to-text round-trips on every CLI write. For cross-machine ticket merge there's no need to commit the binary `tickets.db`; the markdown already covers the rows that matter (tickets, criteria, tags, branches, descriptions). Settings/automation_subjects are the only DB-only gaps and they're tiny.
- **Rejected:** Checking `tickets.db` into git. Binary, doesn't merge (git treats it as a blob; `git merge` becomes "pick a side"), bloats the pack files because every CLI write rewrites the file, and it's multi-project (one DB serves several registry entries) so it would conflate ticket-takeaway's repo with other projects' ticket data. Path is also outside the repo by design.
- **Rejected:** Using SSH to "merge" two diverged tickets.db files. Unison-style sqlite3.backup snapshots don't preserve a merge basis; there's no useful three-way merge for the binary. The sane move is "pick the canonical writer, copy that DB everywhere, replay any unique work from the other side via the existing CLI."
- **Gotcha:** Replacing `tickets.db` on disk under a running serve.py does NOT take effect — SQLite holds the file open and pages are cached in process memory. The replacement landed at filesystem level but the open file descriptor and page cache held the pre-replacement state. Restart the process after any DB swap, even if the file is byte-identical to what you intended.
- **Gotcha:** macOS `python3` is system 3.9, which has no `tomllib` (stdlib only since 3.11). serve.py imports it transitively via `workflow_config`, so the Homebrew Python 3.14 path is required: `/opt/homebrew/Cellar/python@3.14/3.14.4/bin/python3.14`. `python3 serve.py` silently fails with `ModuleNotFoundError: No module named 'tomllib'`; nothing in the WSL setup hits this because system python there is recent enough.
- **Gotcha:** `growth-console` from another project on this WSL grabs port 8787 if started after our serve.py exits. The 404 shape (`{"detail":"Not Found"}` vs our `{"error":"Not found"}`) is the cleanest tell that you're hitting the wrong server.

### Decisions
- **llm-node is the canonical TT writer** as of 2026-05-10. WSL serve.py is shut down; ticket-takeaway is reachable from anywhere via the Tailscale Serve URL `https://tt.rhino-balance.ts.net/ticket-takeaway/...`. Future canonical-writer migrations follow the snapshot+scp+restart sequence captured in CLAUDE.md.
- **Hot-fix via scp + ssh restart is allowed for live diagnosis**, but every hot-fix requires an immediate git round-trip (commit + push + pull + redeploy on llm) so the four file locations don't drift. The runtime copy on llm survives a normal `cp src/* ~/.claude/ticket-takeaway/` only if source matches it — without a commit, the next deploy script silently undoes the fix.
- **Don't commit tickets.db.** PRODUCT_BACKLOG.md handles the merge-relevant rows. If settings/automation_subjects ever need to round-trip across machines, the answer is a small `db_state.json` text dump committed to the repo, not the binary blob.

## 2026-05-09→10 — Workflow rules editor + closed-loop linter + system-rows-readonly invariant

### Summary
- Built an Apple-mail-style rules editor on `/workflows` Edit panel (attribute → operation → value rows, single all/any toggle, +Add affordances). Replaces the dead "Open in project to edit steps →" deep-link. Walks `conditions.ui_catalog()` (also exposed at `GET /api/workflow/catalog`) which maps each predicate kind / on_success effect to a unified attribute domain so the filter side and the action side share the same vocabulary.
- Added four new on_success effects (`set_priority`, `set_automation_mode`, `set_is_container`, `clear_readiness_flag`) so every filterable attribute also has a matching action — the closed-loop principle. Added `lint_closed_loop()` + `POST /api/workflow/lint` returning `ok | warn | manual | empty`; surfaced as inline advisory that re-runs on every edit.
- Extended the system-row-readonly invariant from workflows to agents: editor disabled + banner pointing at `workflows_seed.py`; server-side `PUT`/`DELETE` on system agents return `403 system_agent`. Fixed long-standing footgun where Name/Description on system workflows accepted typing but silently dropped the change at save (added `readonly` on inputs).
- Fixed system-flag drift on Planner + Consultant agents (seed had `system: 0`, now `system: 1`; seed sync flips DB on next restart). Built `src/compare_seed_to_db.py` that reports drift between `workflows_seed.py` and live DB — exits non-zero if any drift, used as ship-readiness check.
- Made the Auto-accept reviewed tickets workflow's action visible: it had `accept_ticket: true` which the catalog mapped to `section` but Section's `action_ops` didn't include accept, so the editor silently dropped it. Added "accept (move to Done + write spec)" as a Section action_op.
- Pre-existing `conftest.api_post` bug: error response branch called `e.read()` twice, eating the body before parsing. Fixed.

### Lessons Learned
- **Accepted (user-confirmed pattern):** Filter attributes ⊆ action attributes. The closed-loop principle isn't a soft linter rule — it's the structural reason the attribute domain is unified. When the filter and action share the same attribute, mutating the attribute naturally invalidates the trigger and the rule self-terminates. User explicitly redirected from a separate "actions list" toward this design after I proposed the easier shape.
- **Accepted:** Apple-mail rule UI for v1 — flat attribute/operation/value rows, single `all_of/any_of` toggle for the whole rule, no nested groups. Covers ~95% of real automations and matches user mental models from Mail / Smart Folders / Shortcuts.
- **Rejected:** `apply_to: children` and `apply_to: siblings` for v1. The canonical "parent moves when all children done" workflow doesn't need them — it filters on `children_all_status_in` (read-only) and acts on `self.section` (mutate). User redirected me from over-engineering broadcast targets.
- **Rejected:** Adding `move_section: Done` to Auto-accept reviewed alongside `accept_ticket: true`. The runner moves first; once `section != "For Review"`, the accept precondition fails and the spec-append silently skips. The fix was to surface `accept_ticket` in the editor catalog, not to double up the effect.
- **Gotcha:** The runner's `_apply_on_success` calls `conn.close()` in `finally`. Tests that pass a shared in-memory connection break after the first call. Pattern: use a tempfile-backed DB and pass a connection-factory function (returns a fresh connection each call) instead of a single connection.
- **Gotcha:** Seeding overwrites the system flag if `DEFAULT_AGENTS` entries don't carry the right value — silent drift between source and DB. Fix: set the flag explicitly on every entry, run the audit tool to confirm.
- **Gotcha:** Another project's server (`growth-console` from `~/projects/maguyva-marketing/`) grabbed port 8787 after our serve.py died. Don't trust "I just restarted" without checking `ss -ltnp` and `curl --include` to see what's actually serving the port — FastAPI 404 (`{"detail":"Not Found"}`) vs our 404 (`{"error":"Not found"}`) is a useful tell.

### Decisions
- **System rows must round-trip through source.** The live DB is a materialised copy of `workflows_seed.py`; any drift is a bug. Editor gates ensure source-as-canonical, audit tool catches drift, ship-readiness check uses it.
- **For asymmetric attributes (Description, Criteria count, Dependencies, Tests, Run state):** show them in the editor's attribute dropdown with the action side disabled and a `hint` field explaining why no action exists. Keeps discoverability without silently hiding capability gaps.
- **Pre-ship dev cruft is OK.** User will prune user workflows + dev settings (e.g. `test.key`) on the macOS llm-node before declaring "to ship." The audit tool flags them; no automatic deletion.

## 2026-05-08→09 — Workflows fixed end-to-end: dispatcher regression, /workflows UX, system/user symmetry

### Summary
- Found that migration 16 (canonical workflows + `workflow_projects` join table) had silently broken the kitchen dispatcher: its SQL still filtered on `workflows.project_id = ?`, but migration 16 nulled that column for system rows. Result: zero system workflows had been firing for ~3 weeks (Parent auto-promote, Spec → Backlog, Backlog → WIP, WIP → Review, Bug triage all dormant). Fixed dispatcher to JOIN `workflow_projects`. Fixed `_create_workflow` to seed the join row for new user workflows. Fixed global PUT to mirror `enabled` into all linked projects. Logged as B-61.
- Reworked `/workflows` page from dense card grid → line items: each row shows name + plain-English trigger sentence ("When the ticket is in Backlog, it has a description, ...") + effect sentence ("Then move ticket to WIP, set status to in-progress.") + meta strip + match badge ("0 match" / "5 matches" / "manual"). Scope is a header-based grouping now, not a filter. Added `src/trigger_describe.py` translator with `describe_trigger()`, `describe_on_success()`, `predicate_rows()`, `effect_rows()`. New `GET /api/workflow/workflows/{id}/preview` returns count + samples for the live badge.
- Edit panel now surfaces structured Trigger and Effects sections so the rule logic is visible inline — fixes the "looks empty" problem for zero-step rules like Auto-accept where Steps is intentionally blank.
- Made every default system workflow's trigger include `automation_mode='auto'` (Parent auto-promote, WIP → Review, Auto-accept, Done → Learnings, Sprint tag rotation) so the per-ticket toggle is the master switch uniformly. Removed orphan "Review → Done" workflow that wasn't in the seeder.
- Removed dispatcher pool-filter asymmetry: previously system workflows iterated all tickets while user workflows were hard-clipped to `automation_mode='auto'` tickets via a separate pool gate. Now both iterate the same pool; trigger predicates are the sole authority. System rows still get evaluation precedence on identical-trigger overlaps via `ORDER BY system DESC, id ASC` (deterministic tie-breaker, not a privilege).
- Added global `POST /api/workflow/workflows/{id}/duplicate` + UI Duplicate button on system rows (project-scoped duplicate already existed). Replaced misleading "Edit steps in advanced editor →" link on system rows with explanatory note + Duplicate; user rows keep the kanban deep-link renamed "Open in project to edit steps →".
- Bug fixes along the way: `[hidden]` attribute was overridden by `display: flex` on `.wf-edit-panel` so every row shipped with its edit panel open; URL-encoded colons (`%3A`) failed the workflow_id regex so the Duplicate button was silently broken in the browser.

### Lessons Learned
- **Gotcha:** Post-migration-16 the dispatcher must read from `workflow_projects.enabled` joined to `wp.project_id`, NOT from `workflows.enabled` / `workflows.project_id`. The latter columns are stale for system rows where `project_id IS NULL` after migration 16's collapse. [Promoted to CLAUDE.md]
- **Accepted:** Per-ticket automation toggle as the universal master switch. Every default system workflow that mutates a ticket includes the `automation_mode='auto'` predicate. Users keep one mental model (the play/pause icon on the ticket card) instead of needing to know which rules respect it.
- **Accepted:** System and user workflows must be functionally symmetric in the dispatcher. The previous asymmetric pool filter ("user workflows only fire on auto-mode tickets, system workflows can fire on anything") meant the platform could automate in ways its users couldn't — the wrong incentive structure. Privilege now lives only in (a) read-only body for system rows and (b) deterministic precedence on identical-trigger overlaps.
- **Accepted:** Trigger sentences as the primary readable artifact on /workflows. Users couldn't tell what each rule did from a JSON predicate tree behind an Edit click; rendering it inline as prose ("When ticket is in Backlog AND has a description AND ...") cut the cognitive cost dramatically.
- **Accepted:** Pattern A (per-ticket trigger evaluation) is the universal automation pattern. The dispatcher already iterates every ticket as its own subject; cross-relationship predicates (`parent_done`, `children_*`) read related state but the mutation target is always the subject. Users can express "fire on each child whose parent is Done" with `section_equals + status_equals + parent_done` predicates, no apply_to:children needed.
- **Gotcha:** `[hidden]` attribute is overridden by explicit `display: flex` (specificity > UA stylesheet's `display: none`). Always pair display rules on toggle-able panels with `[class][hidden] { display: none }`.
- **Gotcha:** `encodeURIComponent` encodes `:` to `%3A`. Path regexes with `[a-z0-9_:.-]` won't match. Either decode the path at request entry (chosen approach — `path = unquote(urlparse(self.path).path)` in every `do_*`) or widen the char class to allow `%`. Same trap will hit any other path-segment ID with non-alnum chars.

### Decisions
- Logged the dispatcher regression as B-61 (review section, infra+ux tags). Capacity for "what was broken silently for 3 weeks" was zero because there's no automated alerting on dispatcher activity — opportunity for a Kitchen heartbeat metric in the future, but not pulled into this session's scope.
- Sprint tag rotation kept as a disabled-by-default example template, not removed. It demonstrates the tag-mutation `on_success` shape and is useful as a duplicate target. Auto-accept and Done → Learnings stay disabled-by-default — both can do destructive things autonomously, opt-in only.
- Global "Enabled" toggle on /workflows treats `enabled` as a bulk operation (writes to `workflows.enabled` AND every `workflow_projects.enabled` row for that workflow). For per-project on/off the kanban bounce panel remains the granular surface; the global page is "is this rule live anywhere?".
- Did NOT remove the `ORDER BY system DESC` tie-breaker. Defensible default — system rows are platform baselines and most user workflows should augment, not duplicate-override. Documented so users know to disable the system row per-project if they want a duplicate to take precedence.

---

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

