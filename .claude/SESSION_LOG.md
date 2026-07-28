# Session Log

## 2026-07-25 — Entry gate: spec_linked now binding at Kitchen dispatch

### Summary
- Per Tom's direction, the spec lane became the *entry* gate into automation, not just advisory: `_ticket_eligibility` and the seeded `Backlog → WIP` trigger both require `spec_linked`, so the Kitchen never dispatches an implementing agent on free text alone. A justified lane-C declaration satisfies it (entry asks "is intent declared", not "is there a delta"). Live DB reseeded; verified against a real Backlog ticket — eligibility now lists the missing lane as a blocker.
- Gate banners updated (Backlog/WIP/For Review) — the full-page ticket view now describes the real gates.
- Saved `workflows/enroll-project-openspec.txt` — the 9-step per-project enrollment recipe with footprint verification, distilled from the loops + stuntsclone2 pilots.

### Lessons Learned
- **Accepted:** Flipping advisory → binding was safe *because* eligibility already requires `automation_mode='auto'` (per-ticket opt-in, default manual). The original advisory caution protected pre-existing tickets; the auto-mode requirement was already doing that job.
- **Gotcha:** `seed_default_workflows` only refreshes system-workflow bodies at serve.py startup — after editing `workflows_seed.py`, the live DB still had the old trigger until explicitly reseeded. `compare_seed_to_db.py` reported OK for the row anyway (it compares the src seed to itself for body state) — verify the actual `trigger_json` in the DB, not the comparator's OK.
- **Gotcha:** 13 kitchen-test fixtures built "eligible" tickets without a lane. Fixed with `_declare_lane` helpers per file rather than baking the flag into `_add_ticket`, so tests that intend *ineligible* stay honest. New parity cases: undeclared lane blocks both paths; justified lane-C passes both.

### Decisions
- The automation pipeline is now gated at three points: Backlog→WIP = `spec_linked`; WIP→Review = commit + `verify_passed`; Review→Done = verify at HEAD + validate + archive. Recorded in LIFECYCLE.md §4c.
- The engine master switches (`kitchen.use_db_workflows`, `automation_mode`) remain off — unchanged from the adoption plan.

## 2026-07-23 — OpenSpec adopted as the spec lifecycle; gates enforced in the core

### Summary
- Built the spec lifecycle from the plan in `docs/` (feat/openspec-lifecycle-gates, pushed, PR not yet opened). New `src/openspec_adapter.py` is the sole `openspec` shell-out (pins `@fission-ai/openspec@1.6.0`, `OPENSPEC_TELEMETRY=0`, fixtures in `tests/fixtures/openspec/`). `actions.accept_ticket()` is now a gate: verify → obligations → `validate --strict` → `archive` → write spec, identical in all three lanes, with a `--force` escape that records the reason. New CLI subcommands `spec` / `verify` / `gate`; new `conditions.py` predicates `spec_linked` / `spec_validates` / `verify_passed` delegate to the same actions.py functions the gate uses. Skills `/spec` and `/accept` became thin callers. `docs/LIFECYCLE.md` §4b/§4c rewritten.
- Repaired `tests_covered`: it sat in six projects' `Backlog → WIP` triggers while `journey_tickets` is empty and nothing sets `no_test_required`, so those tickets were unsatisfiable. It now accepts verify evidence.
- Piloted end-to-end in `loops` (enrolled, both lanes closed through one gate; its own SESSION_LOG has the detail) and in `stuntsclone2` (OpenSpec `--tools none`, one backfill change staged-not-archived; that repo has a live parallel session, so footprint was kept to `openspec/` only).
- Filed the registry-duplicate bug (`reworking-order` + `reworkingorder` → same path) as `ticket-takeaway/BUG-02`, not fixed (choosing the canonical id needs provenance knowledge).

### Lessons Learned
- **Accepted:** Put the rule in `actions.accept_ticket()`, not in the two callers. Verified the gate can't be bypassed by calling `accept` directly (no skill, no GUI) *and* through the `serve.py` HTTP endpoint — both refuse identically, neither half-closes the ticket. That equivalence is the whole argument for enforcing in the core.
- **Accepted:** Capture real OpenSpec JSON as committed fixtures and unit-test the adapter's parsers against them. The package ships ~2 releases/month and self-reports inconsistent key casing; the fixtures make a shape change fail loudly instead of a gate silently passing everything.
- **Rejected:** The plan's "extend the `{"reviewed"}` allowlist — one line at `tickets-cli.py:1204`" was wrong. It was a **4-place** change: `serve.py` had its own duplicate allowlist, and the markdown writer's label map + the parser are two more gates. A flag missing from the label map is written to the DB and then silently dropped on the next regeneration. Collapsed all of it onto one registry in `constants.py`.
- **Rejected:** The plan's "append `Spec: openspec/changes/<name>` to the description" would have collided with the new `Spec:` readiness-flag label — the parser would eat it out of the description. Dropped; the flag content already carries the change name.
- **Gotcha:** `run_verify` first captured stdout/stderr separately and concatenated them, which put all of stderr last and buried the real `passed: N` summary under `ResourceWarning` noise. Fixed with `stderr=subprocess.STDOUT`. [Promoted to CLAUDE.md]
- **Gotcha:** The reversibility test (`rm -rf openspec …`) is destructive to *untracked* files — it deleted the archived change and canonical spec, which aren't tracked until committed. Do it on a copy or commit first.
- **Gotcha:** Editing a ticket's criteria/description via a stale reference re-imported from markdown and briefly clobbered `BUG-01`; restored from the pre-session DB backup. Snapshot `tickets.db` before bulk ticket surgery.

### Decisions
- **Engine stays off.** `_ticket_eligibility` surfaces `spec_linked` as advisory only, so shipping this does not make every pre-existing ticket ineligible. The binding gate is at accept, not at dispatch.
- **Lane is chosen by intent, not size** (A spec'd / B interviewed / C direct), and it changes only how work is *described*. The close is identical in every lane — lane C reads obligations from acceptance criteria instead of a spec delta, and must *justify* a no-delta claim rather than assert one.
- **Archive before the accept commit** (step 5, not after) so the spec-merge diff joins the accept rather than stranding a second PR.
- Deferred: dashboard artifact strip, launch/resume buttons, `kitchen.use_db_workflows`, any `automation_mode` change, and a TT MCP server — all inherit the gates for free once the core enforces them. Per-change token cost was **not** measured (both pilots ran inside this build session); still owed a dedicated per-lane session.

## 2026-05-19 — Pane Link v1 shipped: tmux pane ↔ ticket binding with live tail + attention events (PR #13)

### Summary
- Shipped a new feature that binds a tmux pane to a TT ticket. CLI: `tt link / current / unlink / panes`. API: full CRUD on `pane_links` + `send-keys` with 4 KB cap, 10/s rate limit, null-byte reject, and local-host guard. A 2-second-tick background capture worker runs `tmux capture-pane`, strips ANSI, and feeds a heuristic attention classifier (question / exception / idle / none). GUI: live tail panel + send-keys form in the ticket detail overlay, plus pulsing card indicator on the kanban for question/exception states.
- Brainstormed → spec'd → 14-task TDD plan → executed by a single background `executor` agent grounded only on the plan → 10-issue code review (2 critical + 8 important) → all fixes pushed → merged `origin/main` into the branch to resolve conflicts → renumbered to migration 23 after a second collision check → admin-merged PR [#13](https://github.com/ytubecoder/ticket-takeaway/pull/13) (`85eb63f`) → deployed to `~/.claude/ticket-takeaway/` and restarted serve.py on 8788. Production verified live: API returns wrapped `{"pane_links": []}`, table created, `tt panes` working.
- Plan doc updated with a `## 15. Shipped (as built)` section recording the delta vs plan and the deferred items.

### Lessons Learned
- **Accepted:** One big `executor` subagent + the plan file as its only grounding beats 14 separate per-task dispatches when the plan is detailed and tasks are mechanical. Same isolation, way less orchestration overhead. The two-stage spec+quality review per task is still right for ambiguous architectural tasks, but for "follow this TDD recipe" work it's bureaucracy.
- **Accepted:** Dedicated worktree (`git worktree add ../ticket-takeaway-pane-link feat/pane-link`) was load-bearing — a parallel session was actively committing on `cleanup/dead-code` and kept pulling the working-tree branch out from under us. Without the worktree, the first executor agent saw the wrong branch and aborted. Pattern: when there's known parallel activity, branch isolation needs FS isolation too, not just branch switching.
- **Rejected:** Conflict-resolving agent's first instinct was to keep the in-flight branch's migration number (20) and renumber main's already-merged migrations to 21/22. That would have silently skipped the pane_links migration on any DB at main-state — `version=20` is already recorded as "endpoints" → outer `if not conn.execute(...).fetchone()` is False → CREATE TABLE block never runs → feature broken on first production boot. **Always renumber the in-flight branch, never main's already-merged history.** Caught only by suspicion; the agent's own "this is fine" reasoning was wrong.
- **Gotcha:** Visual-companion server (`superpowers:brainstorming`) has a 30-minute idle timeout. If the user opens the URL late, the server has already shut down. Restart with the same flags works fine; saved files persist across sessions in `.superpowers/brainstorm/`.
- **Gotcha:** macOS Tailscale MagicDNS resolution for the local node (`http://llm:PORT`) only works if MagicDNS is enabled on the client too. For visual-companion and similar local previews, default to `--url-host <tailscale-ip>` instead of `<hostname>` — works from any tailnet node regardless of MagicDNS state.
- **Gotcha:** Spec wrote migration 19, plan bumped to 20 (19 was taken). Five days later at merge time, main had ALSO taken 20 (endpoints) and 21 (bookmarks). Final correct number: 23. Migration-collision check must happen TWICE: at spec time, and again at merge time. Already in CLAUDE.md gotchas; this incident reinforces it.

### Decisions
- Feature scope narrowed from a sprawling "fork-with-context primitive + protocol + reabsorption + Surface A" brainstorm down to **just** "tmux pane ↔ ticket binding with live tail + attention events". The rest is explicitly deferred and listed in the plan's *Shipped (as built)* section. This was the right reframe — the original protocol was over-architected for what was actually needed.
- Implementation lives in a new `src/pane_links.py` module (not `actions.py`) — matches the `journeys.py` / `scenarios.py` pattern and keeps the already-bloated `actions.py` from growing.
- Heuristic attention classifier (regex over the tail) in v1; model-emitted markers like `::tt-needs-input::` reserved for a v2. False positives are acceptable because alerts are non-blocking pulses, never modals.
- Local-host only in v1. `host` column is in the schema and the capture worker filters on `socket.gethostname()`; cross-machine requires a daemon-per-host design and is deferred.
- Send-keys gets an `activity_events` row (audit invariant), but the row's payload logs `text_bytes` and `press_enter` only — never the raw text, which may contain secrets.

## 2026-05-17 — Bookmarks + Recents in nav rail (I-43, B-69) + serve --bind flag

### Summary
- Shipped Bookmarks/Recents as two collapsible sections in the shared left rail. Star toggle on every kanban card, the overlay header, and the full-page ticket header. Per-project, DB-backed (migration 21 — `ticket_bookmarks` + `ticket_recents`). Recents capped at 20 per project; bookmarks survive Done/Wontdo. Settings drops to the rail bottom.
- Followed up with B-69 (UX fixes for 7 user-reported issues): mutually-exclusive Bookmarks/Recents lists (LEFT JOIN filter), unbookmark touches recents in same transaction, capture-phase star click handler (the card's bubble-phase handler was calling `stopPropagation` and eating the toggle), inline row-star buttons on every section entry, both sections default-expanded, tp-star/detail-star bumped to 16px @ 0.85 opacity so they read as primary affordances.
- Added `--bind HOST` to serve.py so port 8799 can stay reachable on the LAN after killing the deleted `/tmp/tt_tailnet_preview.py` (it had bound to `*:8799`; my replacement defaulted to 127.0.0.1, breaking `http://llm:8799/`).
- Three PRs merged to main: #14 (feature), #15 (UX fixes), #16 (bind flag). I-43 + B-69 accepted to Done.

### Lessons Learned
- **Accepted:** Treat the left nav rail as a single shared component — `build_nav_rail_css/html/js` in generate.py is the SoT and every page renderer pulls those three functions. No per-page variant. Spelled out in a beefed-up docstring above `build_nav_rail_css` and in auto-memory (`feedback_nav_rail_singleton.md`).
- **Accepted:** Document-level click handlers for in-card buttons MUST register in capture phase (`addEventListener(..., true)`) — the card's own bubble-phase `stopPropagation` will otherwise eat the event. Also use `stopImmediatePropagation` to belt-and-brace against other capture handlers re-firing the card-expand.
- **Gotcha:** `/kanban` serves `docs/sdlc-dashboard.html`, a pre-generated static file. After ANY `generate.py` edit you must run `python3 src/generate.py` in addition to copying source and restarting serve.py — restart alone keeps the kanban on stale JS. Promoted to CLAUDE.md Deployment section. Memory updated (`feedback_keep_dev_server_current.md`).
- **Gotcha:** Star button width matters for centroid-based playwright clicks. Sizing my new `.star-toggle` at 24×24 (vs other card buttons at ~18×14) shifted the card layout enough that `card-click-expands` smoke test now clicked through to `card-open-btn` and navigated away. Tightened the card variant to match the existing button padding.
- **Gotcha:** serve.py default bind is `127.0.0.1`. Long-lived preview scripts (`/tmp/tt_tailnet_preview.py`, since deleted) bound to `*:PORT`. Restarting serve.py on the same port silently breaks any non-loopback URL until you pass `--bind 0.0.0.0`. The default unchanged for the canonical writer; Tailscale Serve still works.

### Decisions
- Bookmark/Recent storage in SQLite (not localStorage) — follows the user across machines via the canonical writer at port 8788. Personal UI state, not ticket state, so no `emit_event()` and no audit trail.
- Bookmarks and Recents are mutually exclusive lists. A ticket is in exactly one at any time. Unbookmarking puts it back in Recents (touches the row in the same transaction) rather than vanishing.
- Both sections default to expanded so the new affordance is discoverable on first paint. User can collapse and choice is per-section in localStorage.
- Inline row-star renders outline in Recents (click → bookmark) and filled in Bookmarks (click → unbookmark). Same toggle handler; CSS controls visibility (hover-only in Recents, always-on in Bookmarks).
- Star handler delegated at document in capture phase rather than bound per-button — survives any future card re-renders without rebinding.

## 2026-05-12 — Model endpoint abstraction (PR #11)

### Summary
- Shipped the agent/endpoint split: `workflow_agents` now carries a persona + `endpoint_id`; new `endpoints` table (migration #20) owns runtime config (command + args + capabilities + session-resume template). Many agents can share one endpoint. Phase 1 executes `cli` type only (covers claude/codex/hermes); schema reserves slots for `anthropic_api` / `openai_api` / `gemini_api` / `ssh_cli` which raise `UnsupportedEndpointType` at dispatch.
- Built via subagent-driven-development: 26 tasks across 10 phases, each with a fresh implementer + spec-compliance reviewer + code-quality reviewer. Spec went through 2 rounds of independent Codex review (20 findings folded in) before any code was written. Final cumulative review: APPROVED for merge.
- Hit a migration-number collision at merge time. The plan claimed #19; `feat/pwa-mobile` had already merged its own #19 (ticket_created backfill). Renamed mine to #20 across db.py, two test files, the spec, the plan, and CLAUDE.md. Memory saved: always `git fetch origin main` and check live `_migrations` numbers before assigning one.
- Post-implementation UX gap caught during pre-merge dev-server smoke: the agent UI still showed the legacy Command + Args input rows alongside the new Endpoint dropdown, which read as "both are source of truth." Dropped the legacy inputs from the agent UI rendering (kept the DB columns for compat fallback). Fix: `06134b8`.
- Second UX gap: the endpoint dropdown was `disabled` on system agent rows, blocking the most common runtime customisation. Unlocked the endpoint dropdown specifically (persona stays locked) + taught the seed's `ON CONFLICT DO UPDATE` clause to omit `endpoint_id` (preserves user choice across re-seeds) + extended the PUT route to accept `endpoint_id` changes on system rows with 403 on other fields. Fix: `8394a18`.
- I-42 follow-up filed: "Rethink system-row lock: lock on workflow usage, not seed provenance." User pointed out the current "system=1 means uneditable" model conflates provenance with usage; a cleaner model would lock only when a row is referenced by a live workflow. Spec-level rethink owed for a future PR.
- Stale `server_runs_on_wsl.md` memory caught and removed (the server runs on this Mac via Tailscale Serve, not WSL — old memory was wrong by ~25 days). Two new memories saved: `feedback_migration_number_collision.md` and `feedback_keep_dev_server_current.md` (latter went through 3 iterations on user feedback: scope expanded from "after merge" → "after push" → "after any change to dev code, no judgement call").

### Lessons Learned
- **Accepted (architecture):** Agent = persona, Endpoint = runtime is the right split. The Codex review surfaced "OpenRouter is one endpoint per model, not one endpoint with a model selector" — adopted that and it falls out naturally from the dataclass shape. Endpoint binding is configuration; persona is identity; they should not be conflated on the same row.
- **Accepted:** Template-with-`{prompt}`-placeholder for CLI args was strictly better than the alternative `prompt_mode` enum (positional/flag/stdin). One field handles all three day-1 CLIs (claude with flags-after-prompt, codex positional, hermes subcommand+flag) and the codex-resume case where the resume_args replace the entire arg array.
- **Accepted (process):** Subagent-driven-development with the two-stage review (spec compliance then code quality) caught real issues at every layer. Code reviewer caught a HIGH-severity NULL-command crash in T11's compat path that the implementer missed. Spec reviewer caught several minor issues across multiple tasks. Worth the per-task overhead.
- **Accepted (process):** Codex's 2-round spec review surfaced 20 findings, including 4 critical (migration grouping key, FK pragma prereq, transactionality, canonical-id duplication). Folding them in before writing any code prevented all 4 from landing as bugs.
- **Rejected:** Plan's "WSL deploy" step (T26). The stale `server_runs_on_wsl.md` memory drove this; reality is the server runs on this Mac. Collapsed T26 to a post-merge restart note.
- **Gotcha (caught pre-merge):** Migration numbers aren't reserved by spec — they're claimed by whichever PR merges first. Always fetch origin/main and check the live `_migrations` table before writing migration code. Memory saved.
- **Gotcha (recovered):** PR merge blocked by branch protection (requires review on this repo); used `gh pr merge --admin --merge --delete-branch` per the established `feedback_pr_merge_defaults.md` pattern. The gh-local-checkout step failed harmlessly (main was checked out elsewhere); GitHub-side merge succeeded.
- **Gotcha (recovered):** serve.py hardcodes `127.0.0.1` binding at `:11846`. To run a dev server reachable over the LAN/tailnet I sed-patched to `0.0.0.0`, ran, then reverted (worktree only). Worth lifting into a `--bind` flag in a follow-up rather than continuing to sed-patch.
- **Gotcha (process):** "Major code change" was the wrong trigger for the restart-dev-server rule. User corrected three times: any code change, no judgement call. The "is it major" framing invites convincing oneself it's minor and skipping; just always restart. Memory now reflects this.
- **Gotcha (caught by user, not me):** I conflated "deploy" with "manual file-copy step" in the plan and PR description ("deploy to WSL"). Reality: merging to main IS the deploy; the local serve.py picks up changes after a restart. Stale WSL memory drove the wrong framing.

### Decisions
- **Migration #20 is the source of truth for the new schema.** Plan/spec docs were retro-updated; future references should cite #20, not the originally-planned #19.
- **Endpoint binding is editable on system agents.** Persona (name + system_prompt) stays locked because the seed re-upserts it; the endpoint dropdown is unlocked because the seed no longer touches `endpoint_id` after first creation. PUT `/api/workflow/agents/{id}` accepts `endpoint_id` changes on system rows but returns 403 for any other field change.
- **Compat fallback semantics are narrow.** `workflow_agents.command` and `.args` are read by the runner ONLY when `endpoint_id IS NULL`. A non-NULL `endpoint_id` pointing at a missing endpoint, or any non-cli endpoint, is a hard error — no silent fallback.
- **Hermes ships as a seeded endpoint, not a default agent.** Users opt in by editing an existing agent or creating a new one.
- **Dev-server-current rule is unconditional.** After any code change, restart serve.py if the change touches anything in the running module's import graph. Don't judge "is this major" — just always do it.

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

