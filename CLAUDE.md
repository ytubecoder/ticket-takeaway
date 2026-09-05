# CLAUDE.md

Guidance for Claude Code working in this repo. Detail lives in `docs/` and `INSTALL.md`; this file is the agent-facing index + the gotchas you'll actually trip on.

## What This Is

Ticket Takeaway is an SQLite-backed project board. Tickets live in SQLite, `PRODUCT_BACKLOG.md` is regenerated from the DB on every write, and an interactive HTML kanban renders the board. All writes go through `actions.py` (invoked via `tickets-cli.py` or `serve.py`).

## CLI Quick Reference

```bash
CLI=~/.claude/ticket-takeaway/tickets-cli.py

python3 $CLI list --project <project>
python3 $CLI add <project> "Title" --section ideas [--tag t1 --priority high]
python3 $CLI move <project> <ID> wip|review|backlog|ideas|bugs|icebox|done|wontdo
python3 $CLI update <project> <ID> --status blocked|rework|...
python3 $CLI update <project> <ID> --add-tag x --remove-tag y --add-criteria "..."
python3 $CLI spec <project> <ID> --lane A|B|C   # declare lane + create OpenSpec change
python3 $CLI verify <project> <ID>              # run WORKFLOW.toml [verify], record real result
python3 $CLI gate <project> <ID>                # read-only: what accept would decide
python3 $CLI accept <project> <ID> [--force "reason"]  # GATED: needs verify + spec; archives change, then → Done
python3 $CLI seed                          # rebuild DB from markdown (recovery)
python3 $CLI sync                          # regenerate markdown from DB

python3 ~/.claude/ticket-takeaway/generate.py            # render dashboard
python3 ~/.claude/ticket-takeaway/serve.py               # interactive server (port 8787)
```

Every CLI write auto-syncs DB → `PRODUCT_BACKLOG.md`. Markdown preamble and any custom `##` sections you add are preserved; only ticket sections are regenerated.

## Architecture

```
UI (browser) → API (serve.py) → actions.py → SQLite → markdown
CLI (tickets-cli.py) → actions.py → SQLite → markdown

src/constants.py        STATUSES, VALID_STATUSES_BY_SECTION, compute_status_on_move()
src/db.py               schema + migrations
src/actions.py          move/accept/add/update + post-change hooks + emit_event()
src/tickets-cli.py      thin CLI wrapper
src/serve.py            HTTP server + background threads (markdown watcher, scheduled events, kitchen)
src/generate.py         dashboard HTML renderer
src/journeys.py         user journey CRUD + compile to scenario manifest
src/seek.py             project file discovery (TODO/FIXME, GH issues, etc.)
src/scenarios.py        scenario manifest discovery + gallery publishing
src/scenario_drafting.py template-based draft generation
src/page_scraper.py     screen discovery for journey path builder
src/kitchen.py          Kitchen orchestrator (poll/claim/dispatch + pause/resume)
src/workspaces.py       per-subject git worktree manager (Kitchen)
src/runners.py          Runner ABC + AgentRunner + ScenarioRunner + NoopRunner
src/workflow_config.py  WORKFLOW.toml + PROMPT.md reader (Kitchen policy; also [verify])
src/evidence.py         Kitchen evidence rotation
src/trigger_describe.py renders trigger_json / on_success_json to English
src/openspec_adapter.py SOLE place that shells out to `openspec` (pin + telemetry-off + JSON shapes)
```

Source files in `src/` are canonical; runtime copies live in `~/.claude/ticket-takeaway/`. **The deployed copies are what `serve.py` and the CLI actually execute** — see "Deployment gotcha" below.

## Source of Truth

- **DB:** `~/.claude/ticket-takeaway/tickets.db` (SQLite). All writes go through `actions.py`.
- **Markdown:** DB → `PRODUCT_BACKLOG.md` is one-directional on every write. A hash-based watcher thread (5s poll) detects external markdown edits and diff-imports them back. Draft tickets (`draft=1`) are excluded from markdown.
- **Recovery:** if `tickets.db` is lost, `tickets-cli.py seed` reconstructs it.

## Ticket Model

- **Section** (where the work is) → kanban column. **Status** (badge) → lifecycle state within a column. **Readiness flags D C L** (+ `spec` / `verified`, written by the CLI) → checkpoints. All orthogonal — see `docs/LIFECYCLE.md` §3b/§4b for the full model. (Migration 15 collapsed the old T/S flags into acceptance criteria; any "D C S T L" reference elsewhere is stale.)
- **ID prefixes:** `B-` backlog, `R-` released, `I-` idea, `W-` won't do, `Z-` icebox, `BUG-` bug.
- **Ticket format in PRODUCT_BACKLOG.md** and full field definitions: `docs/LIFECYCLE.md` §2.
- **Parent/child:** bug sub-tickets with `Parent: {ID}` are never standalone — rendered nested. If all child bugs are `for-review`/`bug-fixed`/`done`, parent auto-promotes to For Review (now a system workflow, see Workflow Bounce).

## Spec Lifecycle (OpenSpec) — the accept gate

Work is described through OpenSpec change proposals and closed through one uniform gate. **Full model: `docs/LIFECYCLE.md` §4c.** What a cold-start agent must not get wrong:

- **The gate lives in `actions.accept_ticket()`, not in a skill or the UI.** Both surfaces (CLI + `serve.py`) call it, so headless and dashboard accepts are bound identically. Never restate gate rules in `SKILL.md` prose or dashboard JS — that is how the two paths silently diverge.
- **Automation entry is gated too:** `_ticket_eligibility` + the seeded `Backlog → WIP` trigger require `spec_linked` — the Kitchen never dispatches an implementing agent on free text alone. Fixtures that intend "eligible" must declare a lane (`_declare_lane` helpers in the kitchen tests); `test_tdd_engine_parity.py` enforces the two paths agree.
- **Close = verify (real command, real exit code, recorded) → obligations → `validate --strict` → `archive` → write spec.** Identical in all three lanes (A spec'd / B interviewed / C direct); the lane only changes where obligations are read from. `--force "reason"` is the sole bypass and records the reason everywhere.
- **All `openspec` shell-outs go through `src/openspec_adapter.py`.** Pinned to `@fission-ai/openspec@1.6.0` — **not** the bare `openspec` npm name (dead 2019 squat, no `bin`) — with `OPENSPEC_TELEMETRY=0`. Captured fixtures in `tests/fixtures/openspec/` guard the JSON parsing; regenerate them (see LIFECYCLE.md §4c) after any deliberate version bump.
- **Adding a readiness flag is a 3-place change, not one.** The DB accepts any name, so the real gates are: `constants.READINESS_FLAG_LABELS` (both surfaces validate against it), and it supplies the `PRODUCT_BACKLOG.md` line prefix for both the writer and the parser. A flag missing from that registry is written to the DB and then silently dropped on the next regeneration.
- **Per-project verify command:** `WORKFLOW.toml` `[verify] command`. Fallbacks: `tests/run-tests.sh` → `package.json` test → `pytest` → ask once and write it in. This repo's own is pinned to the TDD suite — never widen it (see Testing).
- **TT itself is enrolled** (`openspec/` root present, since 2026-07-29). Enroll other projects via `workflows/enroll-project-openspec.txt` — 9 steps with footprint verification against a baseline snapshot.
- **Spec surfacing (shipped 2026-08-02):** ticket pages have a Spec tab (`?tab=spec`) — status strip, inline doc editing, unrecorded-change backfill (reuses the readiness endpoint, no new mutation route); triggers filter via `spec_status_in`. Derived `SPEC_STATUSES` (constants.py) are **filesystem-only — never subprocess**; validation stays with `spec_validates`. Full spec: `docs/superpowers/specs/openspec-surfacing.md`. Known gap: `spec_doc_edited` events render a generic Activity label until mapped in the label/icon registries.

## Workflow Bounce (I-19) — quick map

Multi-agent prompt routing. Tables: `workflow_agents`, `workflows`, `workflow_runs` (migration 4). Per-project enable state in `workflow_projects` (migration 16).

- **System rows (`system=1`) are read-only.** Their definition lives in `workflows_seed.py`. Server returns `403 system_workflow` / `403 system_agent` on edit/delete. Customize by duplicating (workflows only) or editing seed + restarting serve.py.
- **Source-of-truth invariant:** every system agent + workflow that ships must round-trip through `workflows_seed.py`. Run `src/compare_seed_to_db.py` before declaring a ship version.
- **Master automation switch:** every default mutating workflow includes `automation_mode='auto'` in its trigger. Per-ticket `automation_subjects.automation_mode` is the master kill switch.
- **`on_success` effects:** `move_section`/`move_to`, `set_status`, `set_priority`, `set_automation_mode` (+ optional `pause_reason`), `set_is_container`, `add_tags`/`remove_tags`, `accept_ticket: true`, `set_readiness_content {flag, from: "stdout"|"<literal>"}`, `clear_readiness_flag`, `set_summary_oneliner: true`, `apply_to: "self"|"parent"` (default self).
- **Zero-step workflows** route through `runners.NoopRunner` — no workspace, just applies `on_success` under `ActorContext.system()`.
- **Dispatcher SQL must JOIN `workflow_projects`** and filter `wp.enabled=1 AND wp.project_id=?`. The legacy `workflows.enabled` / `workflows.project_id` columns are stale for system rows.
- **`POST /api/workflow/lint`** returns `{status: ok|warn|manual|empty, shared}` — surface in editors when effects don't mutate any filter attribute (rule won't self-terminate).

## Kitchen

Agentic execution layer on top of the kanban — tickets (build) and journeys (prove) flow through one isolated-worktree runner with audit trails. **Full spec: `docs/KITCHEN.md`.** Critical operational notes are in the gotchas below.

## Testing

```bash
python3 -m pytest tests/test_tdd_*.py -v     # pure logic, no server
python3 -m pytest tests/test_smoke_*.py -v   # API + UI (needs serve.py)
python3 -m pytest tests/test_e2e_*.py -v     # full workflows (needs serve.py + browser)
```

Business logic in `actions.py`, constants in `constants.py`, DB in `db.py` — all importable without side effects. `conftest.py` provides `dashboard_server`, `browser`/`page` (mocked gate-check), `live_page` (no mocks).

**Suite safety:** the smoke/e2e suites' `dashboard_server` fixture writes to the **real** `tickets.db`, and the streaming smokes can spawn real billed `claude -p` sessions. Run them deliberately, never in a loop or as a gate — the verify gate (`WORKFLOW.toml [verify]`) is pinned to the TDD suite for exactly this reason.

**Lint bar:** `ruff.toml` pins the ruleset (ruff 0.16+ ships a ~415-rule default set — the bar comes from this file, not ruff's drifting defaults; `uvx ruff check` is clean as of 2026-07-29). Every ignored rule carries its WHY in a comment — read it before "fixing" an ignored category. SIM118 in particular stays ignored: membership tests here are on `sqlite3.Row`, where `.keys()` is correct and `in row` iterates *values* — auto-applying ruff's fix broke 14 sites.

**Scenario runner** (manifest-driven UI scenarios with screenshot gallery publishing): `python3 -m pytest tests/test_scenarios.py -v [--scenario-id ID] [--publish] [--backend=playwright|cdp]`. Manifests in `tests/scenarios/*.json`. Backend toggle in journey detail view.

## API Response Format

**All workflow/journey list APIs return wrapped objects:** `{"agents": [...]}`, `{"workflows": [...]}`, `{"runs": [...]}`. JS must always unwrap with `data.agents || data || []` — iterating the response directly has caused bugs 5+ times.

## Deployment

`src/` is canonical; `~/.claude/ticket-takeaway/` is the runtime copy `serve.py` and the CLI actually load. After editing any source file, **redeploy and restart**:

```bash
cp src/{generate,tickets-cli,serve,constants,db,actions,journeys}.py ~/.claude/ticket-takeaway/
python3 src/generate.py                                # ONLY if you edited generate.py — see below
pkill -f "ticket-takeaway/serve.py"; sleep 1
python3 ~/.claude/ticket-takeaway/serve.py &
```

**`generate.py` edits also need an HTML regenerate.** The `/kanban` route serves the pre-generated `docs/sdlc-dashboard.html` (not a live re-render). After any edit to `generate.py` — rail JS/CSS, card markup, overlay HTML, etc. — you must also run `python3 src/generate.py` to rewrite the static file. Restarting serve.py alone leaves the kanban serving stale UI even though the API/full-page routes pick up the new code. Caught after editing `build_nav_rail_js` and seeing the kanban keep firing the old handler.

Full deployment map: `INSTALL.md`.

## Critical gotchas

- **Kitchen orchestrator is paused by default on every fresh server boot.** Eligible tickets will NOT auto-dispatch until the user clicks Resume in the Kitchen view (or POSTs `/api/kitchen/resume`). Persistence: `settings.kitchen.paused`. Manual "Run now" buttons bypass the pause. If you're debugging "why didn't my eligible ticket run", check `kitchen.is_paused()` first.

- **Next migration must be #24.** Live sequence: `1–5` (core), `6` (`ticket_branches`), `7` (`ticket_tags`), `8` (Kitchen — automation_subjects, runs, activity_events), `9–17` (factory-talk primitives + interactive runs), `18` (`tickets.summary_oneliner` + `tickets.summary_hash`), `19` (backfill `ticket_created` activity events for tickets that predate provenance tracking; recovers seek origin from description's `Source:` prefix), `20` (`endpoints` — agent endpoint table + workflow_agents.endpoint_id backfill), `21` (`ticket_bookmarks` + `ticket_recents`), `22` (skipped — never used), `23` (`pane_links` — tmux pane→ticket binding, capture tail, attention state). The `_migrations` table is a set of versions, not a strict sequence — gaps are harmless — but never reuse a number. Always check `grep "_migrations.*VALUES" src/db.py | tail` before adding one; also `git fetch origin main` first, since in-flight branches can race for the same number.

- **macOS `socket.getfqdn(127.0.0.1)` hangs `serve.py` startup for 30s+.** Symptom: `Serving N project(s)` prints then nothing — `ThreadingHTTPServer.__init__` never returns. Fix: monkey-patch `HTTPServer.server_bind` to skip the reverse-DNS lookup before importing `serve.py` (reference: `/tmp/start-kitchen-demo.py`). Doesn't affect WSL/Linux.

- **One canonical DB writer at a time across machines.** Running `serve.py` on two machines against unison-synced copies of `tickets.db` diverges them silently — `sqlite3.backup()` snapshots aren't merge-aware. As of 2026-05-10 the canonical writer is **llm-node** (`tt.rhino-balance.ts.net` via Tailscale Serve, port 8788); WSL `serve.py` is shut down. To migrate: (1) `sqlite3.backup()` source DB to /tmp, (2) `scp` to destination's runtime path, (3) restart destination's `serve.py` to release file handle, (4) stop source's `serve.py`. PRODUCT_BACKLOG.md is the git-merge-friendly fallback — `tickets-cli.py seed` reconstructs the DB from it.

- **Page renderers must use origin-relative API base URLs.** Every `_render_*_page()` in `serve.py` emits client-side JS. If it bakes `http://localhost:{port}/{pid}/api`, the JS breaks under Tailscale Serve / port forwarding / any reverse proxy because the browser resolves `localhost:{port}` on the *client's* laptop. Always use `f"/{pid}/api"`.

- **Same-transaction event emission is the audit invariant.** Every mutation in `actions.py` (and routed-through helpers in `serve.py`) MUST call `emit_event()` inside the same `conn` transaction as the SQL write. `db_session()` in `runners.py` enforces this for runner code; ad-hoc helpers must do it explicitly. Splitting them lets rollback leave the audit log disagreeing with state.

- **Ticket creation always emits `ticket_created` with an `origin` payload.** `actions.add_ticket` centrally fires the event (default origin `human`/`agent` from `ActorContext.actor_type`). Callers wanting richer payloads — `seek` (origin=`seek` + source info), `seed_project` (`seed`), `_ingest_markdown_changes` (`markdown_edit`), journey-gap path (`journey_gap` + run/journey ids) — pass `emit_created_event=False` and emit their own. New origins also need render branches in JS `_eventSummary` and Python `_ticket_created_summary` or the Activity tab shows generic "origin: <name>". Never emit twice for the same ticket.

- **Inline `onclick="..."` inside JS string literals must use HTML entities for nested quotes.** Pattern that broke twice in `serve.py`'s activity-tab JS: a JS source line like `runLink = ' <a onclick="setItem(\'foo\',\'bar\')">...</a>';` renders to `... onclick="setItem('foo','bar')" ...` because Python's triple-quoted f-string converts `\'` to `'`. JS then sees the inner `'foo'` as the closer and fails with `Unexpected identifier`, killing the *entire* `<script>` block. Use `&apos;` for inner single quotes and `&quot;` for inner double quotes — the browser HTML-decodes them inside the attribute at parse time. Don't rely on backslash escapes inside Python triple-quoted f-strings that emit JS string literals.

- **`run_verify` must merge stderr into stdout, not capture them separately.** The `verified` flag records the tail of a verify run to show *how it ended*. Capturing stdout and stderr into two buffers and concatenating puts all of stderr last, which routinely buries the real `passed: N, failed: 0` summary under trailing `ResourceWarning` noise. Use `stderr=subprocess.STDOUT`.

- **Port 8787 is not always free.** On llm-node it's owned by `growth-console` (maguyva-marketing), not TT. When starting `serve.py` for a one-off (e.g. verifying a gate through the HTTP path), pass `--port 8790` and check `lsof -nP -iTCP:<port> -sTCP:LISTEN` first. The canonical production writer is still port 8788 (see the DB-writer gotcha).

## Further reading

- `docs/LIFECYCLE.md` — ticket format, field definitions, sections, statuses, readiness flags; **§4c = the OpenSpec spec lifecycle + accept gate**
- `docs/KITCHEN.md` — Kitchen orchestrator spec (architecture, eligibility, runners, closed loop)
- `docs/ARCHITECTURE.md` — high-level system overview (note: predates SQLite migration; markdown-as-DB framing is stale)
- `docs/REVIEW_PROCESS.md` — `/review` skill workflow
- `INSTALL.md` — deployment map + per-project setup
- `docs/superpowers/specs/` — design docs for major features (gate-check, feedbacks, workflow bounce, etc.)

## 🗄️ Archived warmstart (2026-09-06)

`HANDOFF.md` was moved to `docs/_archive/warmstarts/`. It was a session handoff —
ephemeral by design — and most of it was already stale (it opens with a directory
rename that has long since happened). **Nothing was deleted and nothing was
extracted.**

🚨 It may still hold a durable fact or two that belongs in this file or the specs.
Nobody has checked. If you next touch an area it covered, open it, lift what is
still true into the right permanent doc, and note that here.

A doc in this repo is either **static** — maintained, no live system state in it —
or **ephemeral**, in which case it belongs in a scratchpad and gets deleted after
use. The tell for the second kind: a "current status" or "next steps" section.
