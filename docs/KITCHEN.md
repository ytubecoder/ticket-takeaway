# Kitchen

*Ticket Takeaway is the takeaway shop — standardised work units served fast.
Kitchen is the back-of-house: an execution layer that turns eligible work units
into agent runs in isolated worktrees, with full audit trails and a live view
of every cook on the line.*

Plan version: **v3** (locked, pending consultant final sign-off)
Branch: `feat/kitchen`

---

## 1. Problem statement

Ticket Takeaway is a good order book — work is captured, specced, tracked. It is
not yet a kitchen. Today every order requires the shop owner to step into the
back, light the burner, watch it cook, plate it. That breaks at scale: the human
becomes the bottleneck on supervision, not on judgment.

OpenAI's Symphony spec describes the right back-of-house pattern — work flows
from the order book into per-order stations, agents cook, humans review what
comes off the pass. We adopt the pattern as a runtime layer on top of the
existing kanban. We do not adopt the implementation, the daemon shape, the
Linear coupling, or the musical vocabulary.

Concurrently, the existing scenarios/journeys system is the seed of a second
paradigm: zero-knowledge use cases that prove (or disprove) that the product can
do what users want. Today these are decorative. Under Kitchen they close the
loop with tickets.

## 2. North star: the closed loop

```
  Journey runs RED  →  files structured gap tickets in Ideas
                    →  human triages, moves to Backlog
                    →  ticket reaches D∧C∧(T ∨ linked-journey ∨ no_test_required) → eligible
                    →  cook (agent) picks it up at an isolated station (worktree)
                    →  PR lands, ticket → For Review → Done
                    →  linked journey re-runs automatically → GREEN
                    →  journey becomes the acceptance proof for the ticket
```

Tickets describe intended changes. Journeys describe desired user outcomes.
Tickets build the product. Journeys continuously prove whether the product can
actually do what users ask. Kitchen is the engine that runs both lanes and
closes the loop between them.

## 3. Principles

1. **Ticket lifecycle is the canonical state.** Sections (Ideas → Backlog → WIP
   → Review → Done) are the orchestration board. No parallel automation state
   machine on tickets.
2. **Automation is intent + facts, not state.** A subject has an *intent*
   (`automation_mode`) and accumulates *facts* (`runs`, `activity_events`).
3. **Symphony's pattern, not its implementation.** Python/SQLite/local-first/
   single-machine. Background threads inside `serve.py`, not a daemon.
4. **Opt-in automation, never opt-out.** Subjects default to `manual`.
5. **One ticket board per project.** Per-project navigation stays
   `Board | Journeys | Settings`. Automation surfaces as badges, filter chips,
   and a cross-project Kitchen view.
6. **Eligibility = DCSTL + deps + intent.**
7. **The cook moves the order.** Agents perform ticket writes (section
   transitions, criteria, comments) inside their session. Kitchen schedules,
   reconciles, observes, audits.
8. **Everything is auditable and rollback-able.** Every agent action is logged
   distinctly from human actions. Worktrees mean rollback = wipe worktree state
   and let the next attempt start from a clean base.

## 4. The three work-item types

| Type | Question | Output | Eligibility gate |
|---|---|---|---|
| **Ticket** | "Build/fix/change X" | Code change + accepted criteria | D ∧ C ∧ deps clear ∧ (T ∨ linked-journey ∨ explicit no_test_required) |
| **Journey** | "User can accomplish Y" | Green run OR structured gap report | Manifest validates, selectors resolvable |
| **Investigation** | "Is/how/why Z" | Pinned writeup + recommendations + optional draft tickets | Clear question, bounded scope |

**Investigation constraint:** scoped, mostly read-only discovery. If it needs
code, it produces tickets. If it needs a prototype, that's a spike ticket.

Tickets and journeys remain separate domain tables. Investigations get their own
table when built (deferred to M1.5; the `subject_type` enum accepts them from
M1).

## 5. Architecture

```
Policy Layer       — repo-owned WORKFLOW.md (replaces some settings rows)
Tracker Layer      — already exists: SQLite + actions.py + markdown sync
Orchestrator       — new: src/kitchen.py
Workspace Manager  — new: src/workspaces.py
Runner Layer       — new: src/runners.py
                       AgentRunner    (extends existing workflow_runs)
                       ScenarioRunner (existing tests/scenario_runner.py)
                       GapAnalyzer    (new: classifies red scenario runs)
Evidence Store     — separate ~/.ticket-takeaway/evidence/, with retention
Audit Layer        — activity_events table; agent events visually distinct
Status Surface     — Kitchen view (new) + per-card badges + live ticket view
```

`serve.py` calls `kitchen.start(get_db, settings)` on startup and
`kitchen.stop()` on shutdown. API handlers proxy to functions in `kitchen.py` /
`actions.py`. No orchestration logic inside `serve.py` itself.

Tracker Adapter is collapsed: we *are* the tracker. Polling is a SQLite read.

## 6. Data model

Three new tables. Migration #6. Nothing changes on existing `tickets` or
`journeys` tables (except one new optional flag on tickets — see §7).

```sql
-- Intent: how the human wants this subject treated.
CREATE TABLE automation_subjects (
  project_id      TEXT NOT NULL,
  subject_type    TEXT NOT NULL,                 -- ticket | journey | investigation
  subject_id      TEXT NOT NULL,
  automation_mode TEXT NOT NULL DEFAULT 'manual', -- manual | auto | held
  hold_reason     TEXT,
  watched_at      TIMESTAMP,
  created_at      TIMESTAMP NOT NULL,
  created_by      TEXT,                          -- 'human' | 'agent:<run_id>' | 'system'
  updated_at      TIMESTAMP NOT NULL,
  updated_by      TEXT,
  PRIMARY KEY (project_id, subject_type, subject_id)
);

-- Facts: every execution attempt.
CREATE TABLE runs (
  id                 INTEGER PRIMARY KEY,
  project_id         TEXT NOT NULL,
  subject_type       TEXT NOT NULL,
  subject_id         TEXT NOT NULL,
  runner_kind        TEXT NOT NULL,              -- agent | scenario | gap_analyzer
  status             TEXT NOT NULL,              -- queued | preparing | running | needs_input | succeeded | failed | stalled
  workspace_path     TEXT,
  thread_id          TEXT,                       -- agent thread, reused across continuation turns

  -- claim / heartbeat
  claimed_at         TIMESTAMP,
  claim_owner        TEXT,                       -- pid/host/instance ID of the kitchen process
  heartbeat_at       TIMESTAMP,

  -- timing
  started_at         TIMESTAMP,
  finished_at        TIMESTAMP,
  duration_ms        INTEGER,

  -- result
  exit_code          INTEGER,
  error_class        TEXT,
  error_message      TEXT,
  summary            TEXT,                       -- short rolling summary
  metadata_json      TEXT NOT NULL DEFAULT '{}', -- extensible: tool call counts, token usage, etc.
  evidence_dir       TEXT,                       -- absolute path under ~/.ticket-takeaway/evidence/{run_id}/
  evidence_status    TEXT NOT NULL DEFAULT 'live',  -- live | summarised | pruned

  -- needs_input pause
  needs_input_prompt TEXT,

  -- relationships
  attempt            INTEGER NOT NULL DEFAULT 1,
  parent_run_id      INTEGER,                    -- on retry, the prior run we're continuing from
  retry_kind         TEXT,                       -- 'resume' (default) | 'fresh' (worktree wiped)
  triggered_by       TEXT                        -- human | journey-cascade | retry | scheduled
);

CREATE INDEX runs_subject_latest ON runs (project_id, subject_type, subject_id, id DESC);
CREATE INDEX runs_active ON runs (status) WHERE status IN ('queued','preparing','running','needs_input');
CREATE INDEX runs_evidence_age ON runs (finished_at, evidence_status);

-- DURABILITY GUARANTEE: only one active run per subject, ever.
-- This is what makes deriving state from runs (instead of caching it) safe.
CREATE UNIQUE INDEX one_active_run_per_subject
  ON runs (project_id, subject_type, subject_id)
  WHERE status IN ('queued','preparing','running','needs_input');

-- Audit: every state-changing event.
CREATE TABLE activity_events (
  id            INTEGER PRIMARY KEY,
  project_id    TEXT NOT NULL,
  subject_type  TEXT NOT NULL,
  subject_id    TEXT NOT NULL,
  actor_type    TEXT NOT NULL,                   -- human | agent | system
  actor_id      TEXT,                            -- run_id when agent, user identifier when human
  event_kind    TEXT NOT NULL,                   -- see vocabulary in §9
  payload_json  TEXT NOT NULL,                   -- before/after values, prompt/response text, etc.
  occurred_at   TIMESTAMP NOT NULL,
  discarded_run_id INTEGER                       -- set when a run rollback retracts this event
);
CREATE INDEX activity_subject ON activity_events (project_id, subject_type, subject_id, occurred_at DESC);
CREATE INDEX activity_run ON activity_events (actor_type, actor_id) WHERE actor_type = 'agent';
```

**Deliberate non-decisions:**
- No `automation_state` enum stored anywhere. Derived from the latest non-terminal `runs` row.
- No `last_run_id` cache. Indexed `MAX(id)` query.
- No new fields on `tickets` (except `no_test_required` boolean — see §7).
- No file-based audit log. `activity_events` is the table.

## 7. Eligibility (computed, never stored)

```python
def eligibility(subject) -> EligibilityResult:
    """Returns (eligible: bool, reasons: list[str]).
    Always returns reasons even when eligible (for UI 'why' tooltips)."""
```

Eligibility for tickets:
- `automation_mode == 'auto'`
- AND `section IN ('backlog', 'wip', 'review')`
- AND description present
- AND criteria present and not all empty
- AND no unresolved blocker tickets
- AND (tests present OR `has_linked_journey` OR `no_test_required` flag set with note)
- AND no active run (the unique index enforces this; pre-flight checks for clarity)

Eligibility for journeys:
- `automation_mode == 'auto'`
- AND manifest validates
- AND no active run

`no_test_required` — new boolean column on tickets, requires non-empty
`no_test_required_note`. UI: a checkbox under the T section "no tests required"
revealing a mandatory text field. Keeps the bypass explicit, not implicit.

## 8. Concurrency

Two caps, configured in `WORKFLOW.md`:

- `max_concurrent_runs: 3` — global cap (system-wide, all projects, all runners)
- `max_concurrent_per_project: 1` — per-project cap

Per-project default of 1 prevents a noisy project from monopolising slots. Both
caps are hot-reloadable on tick.

Orchestrator poll tick:
1. Reconcile: refresh state of all active runs; expire stalled ones (heartbeat).
2. Compute available slots: `max_concurrent_runs - count(active runs)`.
3. Fetch eligible subjects across all projects (sorted by priority, age, identifier).
4. For each candidate: skip if its project already has `max_concurrent_per_project` active.
5. Dispatch until slots are exhausted.

Claim is durable: insertion of the run row at status `queued` with `claim_owner`
set is atomic. The `one_active_run_per_subject` partial unique index makes
double-dispatch impossible.

## 9. Activity event vocabulary (frozen in M1)

This is the wire format. Add new kinds later, but every M1+ event uses one of
these:

| `event_kind` | Emitted when | Required `payload_json` keys |
|---|---|---|
| `mode_changed` | automation_mode flipped | `from`, `to`, `reason?` |
| `hold_set` | mode → held | `reason` |
| `hold_cleared` | mode → manual or auto from held | `prior_reason` |
| `section_change` | ticket section moved | `from`, `to` |
| `status_change` | ticket status badge changed | `from`, `to` |
| `criteria_check` | acceptance criterion ticked/unticked | `criterion_id`, `state` |
| `comment_added` | new comment on subject | `comment_id`, `excerpt` |
| `run_started` | runs row inserted with status `preparing` | `run_id`, `runner_kind`, `triggered_by` |
| `workspace_created` | workspace dir created (or reused) | `path`, `reused` |
| `agent_output` | agent emitted text we want to remember | `run_id`, `summary`, `tokens?` |
| `needs_input` | run paused for user | `run_id`, `prompt` |
| `input_provided` | user responded | `run_id`, `response_excerpt` |
| `run_succeeded` | terminal success | `run_id`, `summary`, `duration_ms` |
| `run_failed` | terminal failure | `run_id`, `error_class`, `error_message` |
| `run_stalled` | killed for inactivity | `run_id`, `last_heartbeat_age_ms` |
| `run_discarded` | rollback button pressed | `run_id`, `reverted_event_count` |

Discarding a run sets `discarded_run_id` on every `activity_events` row from
that run — the history view shows them struck-through with the discard reason.
Section/status changes performed by the discarded run get reverted by the
rollback action (it issues fresh events with `actor_type = 'system'`, doesn't
delete history).

## 10. Workspace policy

- **`git worktree` default.** Clone fallback only when not a git repo.
- **Path:** `~/.ticket-takeaway/workspaces/{project_id}/{subject_type}/{subject_id}/`
- **Persistent while subject is active.** Reused across runs.
- **On retry, resume in the same worktree** — worktree IS the
  collision-prevention; same agent picking up its own incomplete state can't
  conflict with anything else.
- **"Retry fresh"** as a separate, lesser-used action: wipes worktree state,
  re-runs `after_create`. Useful when previous state is corrupt or
  fundamentally wrong.
- **Retention:** archived 14-30 days after subject reaches Done; cleaned
  thereafter. Configurable in WORKFLOW.md.
- **Hooks:** all hooks run with `cwd = workspace_path`. The worktree itself is
  created by the workspace manager BEFORE `after_create` runs.
- **Safety invariants** (lifted from Symphony §9.5):
  - runner cwd MUST equal workspace_path
  - workspace_path MUST live inside workspace_root
  - workspace key sanitized to `[A-Za-z0-9._-]`

## 11. Repo-owned policy file

`WORKFLOW.md` at repo root. YAML front matter (config) + Markdown body (prompt
template).

```yaml
---
automation:
  default_mode: manual
  max_concurrent_runs: 3
  max_concurrent_per_project: 1
agent:
  command: claude -p
  sandbox: workspace-write
  max_turns: 20
workspace:
  retention_days_after_done: 21
evidence:
  live_days: 30
  summarised_days: 60
hooks:
  # All hooks run with cwd=<workspace_path>. The worktree itself is
  # created by the workspace manager BEFORE after_create runs.
  after_create: |
    # Fresh worktree: bootstrap deps. Runs once per workspace lifetime.
    npm install --silent
    cp ~/.ticket-takeaway/secrets/.env.local .env.local 2>/dev/null || true
  before_run: |
    # Runs before every attempt. Idempotent.
    git pull --rebase origin main
---

# (prompt body — passed to the agent as workflow instructions)
You are working on Ticket Takeaway ticket {{subject.id}}: {{subject.title}}
...
```

`.ticket-takeaway/project.toml` for structured policy added later if WORKFLOW.md
grows complex.

## 12. UI surfaces

**Per-project board** (existing kanban, light additions):
- Card badge: idle dot · queued spinner · running spinner · ⚠ needs-input · ✗ failed · 🔒 held
- Click badge → run-history popover (with quick-respond if needs_input)
- Filter chips on header: `All | Needs Me | Running | Ready To Delegate | Held | Failed`
- Auto toggle in detail overlay: Manual / Auto / Held (held requires reason)

**Ticket detail** (existing, with new sections):
- Live run panel (top) when a run is active — current step, last assistant
  message, token counters, runtime, workspace link, "Stop run" / "Discard run"
- Inline `needs_input` panel when applicable
- History tab shows audit events (human and agent visually distinct;
  discarded events struck-through)
- "Run now" / "Retry" / "Retry fresh" buttons

**Kitchen view** (new landing page at `/`):
- **Needs Me** — runs in `needs_input`, eligible-but-failed, ambiguous-criteria handoffs
- **Running** — active worktrees with current step / last heartbeat / runtime / link to live view
- **Ready To Delegate** — eligible subjects with no active run
- **Held** — paused subjects with reasons
- **Projects** — per-project health rows
- Watched-projects toggle in registry: `watched: true/false`

**`needs_input`: three entry points, one event.** The Kitchen queue, the ticket
detail panel, and the card badge popover all submit to the same endpoint and
write one `input_provided` event. User picks whichever surface they're on.

## 13. Evidence storage and rotation

- **Location:** `~/.ticket-takeaway/evidence/{run_id}/`
- **Auto-attached:** every run that produces evidence registers an attachment
  row of `kind = 'run_evidence'` so it shows up in the ticket's existing
  attachments tab.
- **Retention pipeline** (background thread, runs daily):

| Age | `evidence_status` | What's there |
|---|---|---|
| 0–30 days | `live` | Everything raw |
| 30–90 days | `summarised` | Auto-generated `summary.md` (LLM pass over the transcript), screenshots compressed, raw transcript gzipped |
| 90+ days | `pruned` | Only `summary.md` remains; large artifacts deleted |

The `runs` row stays forever — only on-disk artifacts are rotated.

## 14. Audit + live ticket view

**Audit / rollback:**
- Every section change, status change, criteria toggle, comment, etc. is an
  `activity_events` row.
- Agent events have `actor_type = 'agent'` and `actor_id = <run_id>`. Human
  events have `actor_type = 'human'`.
- Ticket detail's history tab shows them inline with distinct visual treatment
  (agent rows have a small bot icon + workspace link; discarded rows struck
  through).
- Undo/redo: Ctrl+Z on a human action undoes it. Agent actions are not undoable
  via Ctrl+Z — they're rolled back via "Discard run" which reverts every event
  from that run as a unit.

**Live ticket view (open a ticket → see what's happening right now):**
- If there's an active run, the ticket detail shows a live panel at the top.
- Server-pushed via existing 2s polling (no websockets needed).
- "Open transcript" gives a live-tailing view of the full agent stream.
- This is the view you'll actually use to "see agents running in the right
  context" — opening any ticket lets you watch its current cook.

## 15. Phasing

**M1 — Schema + intent + audit + badges** *(~1 wk)*
- Migration #6: `automation_subjects`, `runs`, `activity_events` (+ all indexes)
- `subject_type` enum accepts `ticket | journey | investigation`
- `eligibility(subject)` pure function in `actions.py`
- `no_test_required` + `no_test_required_note` columns on tickets
- Auto toggle in ticket detail (Manual / Auto / Held + reason)
- Derived badge on every card
- All event_kinds in §9 are emitted by existing human actions (criteria, moves, status, comments) from M1 onward
- Module skeletons exist: `src/kitchen.py`, `src/workspaces.py`, `src/runners.py` with stubs
- No runner yet, no workspace, no Kitchen view yet

**M2 — Kitchen view + board filter chips + history tab** *(~1 wk)*
- New landing page at `/`, replacing project picker as default
- Filter chips on per-project board
- Activity history tab on ticket detail (only human events visible until M3)
- Watched-projects flag in registry
- Still no execution

**M3 — Workspace manager + runner + live view** *(~2 wks)*
- Workspace manager with worktree default + hooks + safety invariants
- `Runner` ABC + `AgentRunner` + `ScenarioRunner`
- WORKFLOW.md reader, replacing relevant settings rows
- Orchestrator background thread inside `serve.py` (poll → eligible → claim → dispatch → reconcile → heartbeat)
- Live run panel on ticket detail
- `needs_input` inline panel
- "Run now" / "Stop run" / "Discard run" / "Retry" / "Retry fresh" buttons
- Audit log captures agent events
- This is the workspace-isolation milestone.

**M4 — Closed loop + GapAnalyzer** *(~1-2 wks)*
- `GapAnalyzer` runner classifies red scenario runs into typed gaps:
  `missing_selector | missing_screen | missing_feature | ambiguous_goal | external_dependency | test_harness_gap`
- "Accept gap → file ticket" with pre-populated D/C from gap context
- `journey_tickets` linkage shown on both card sides
- Auto re-run linked journey when its dependent tickets reach Done
- Eat our own dog food: ticket-takeaway becomes the first project running under Kitchen.

**M5 — Evidence rotation pipeline** *(~3 days)*
- Daily background job summarises 30+ day runs, prunes 90+ day artifacts
- `evidence_status` field driven by the pipeline
- Summary template + LLM call

**Deferred:** Investigations table + UI. Codex-app-server-style fleet
execution. Multi-agent runner orchestration (extending workflow-bounce).

## 16. What we are explicitly NOT building

- A new ticket board. Existing kanban is canonical.
- A parallel automation state machine on tickets.
- A Linear adapter. We are the tracker.
- A daemon process. Background threads inside `serve.py` are sufficient.
- Auto-run by default. Always opt-in.
- An investigations UI in M1-M4.
- A separate notification system for `needs_input` — it lives on the ticket.
- File-based audit logs — `activity_events` is the table.
- Cached automation state. Derived from runs.

## 17. Name rationale

Ticket Takeaway is a takeaway shop: standardise the work unit so you can serve
fast. **Kitchen** is the back-of-house — where orders get cooked.

- **Tickets** = orders on the rail
- **Worktrees** = stations (each cook gets their own prep area, no collisions)
- **Agents** = cooks
- **Kitchen view** = the pass (where you see what's on, what's coming up, what needs the chef's eye)
- **`needs_input`** = a cook calling the chef over to taste
- **Audit log** = the prep notes taped to the wall

The takeaway shop framing also explains the constraints. A takeaway doesn't do
bespoke; it standardises so it can be fast. We don't add custom workflows per
ticket — we add one orchestrator, one runner interface, one audit log. The win
is throughput on standard orders, not personalisation.

---

## Changelog

- **v1** — initial sketch from Symphony reading.
- **v2** — orthogonal automation state proposal; later rejected in favour of
  one canonical lifecycle + derived facts.
- **v3** — current. Consultant feedback locked in: `automation_mode`
  vocabulary cleaned (`manual | auto | held`); durable claim + heartbeat;
  `queued` status added; modules separated from `serve.py`; WORKFLOW.md hook
  example fixed; explicit `no_test_required` flag; activity event vocabulary
  frozen at M1; per-project concurrency cap added.
