# Kitchen

*Ticket Takeaway is the takeaway shop — standardised work units served fast.
Kitchen is the back-of-house: an execution layer that turns eligible work units
into agent runs in isolated worktrees, with full audit trails and a live view
of every cook on the line.*

Plan version: **v4** (Codex sign-off conditional on M1a patches; this version
incorporates all of them).
Branch: `feat/kitchen`
Python: requires 3.11+ (for `tomllib` stdlib).

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
paradigm: zero-knowledge use cases that prove (or disprove) that the product
can do what users want. Today these are decorative. Under Kitchen they close
the loop with tickets.

## 2. North star: the closed loop

```
  Journey runs RED  →  files structured gap tickets in Ideas
                    →  human triages, moves to Backlog
                    →  ticket reaches D∧C∧deps∧(T ∨ linked-journey ∨ no_test_required) → eligible
                    →  cook (agent) picks it up at an isolated station (worktree)
                    →  PR opened by agent, human merges
                    →  Kitchen detects merged PR → ticket auto → Done
                    →  linked journey re-runs automatically → GREEN
                    →  journey becomes the acceptance proof for the ticket
```

Tickets describe intended changes. Journeys describe desired user outcomes.
Tickets build the product. Journeys continuously prove whether the product can
actually do what users ask. Kitchen is the engine that runs both lanes and
closes the loop between them.

## 3. Principles

1. **Ticket lifecycle is the canonical state.** Sections (Ideas → Backlog → WIP
   → For Review → Done) are the orchestration board. No parallel automation
   state machine on tickets.
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
9. **Merge stays human.** Kitchen never runs `gh pr merge`. It detects
   human-initiated merges and reconciles state.

## 4. The three work-item types

| Type | Question | Output | Eligibility gate |
|---|---|---|---|
| **Ticket** | "Build/fix/change X" | Code change + accepted criteria | D ∧ C ∧ deps clear ∧ (T ∨ linked-journey ∨ explicit `no_test_required`) |
| **Journey** | "User can accomplish Y" | Green run OR structured gap report | Manifest compiles AND validates |
| **Investigation** | "Is/how/why Z" | Pinned writeup + recommendations + optional draft tickets | Clear question, bounded scope |

**Investigation constraint:** scoped, mostly read-only discovery. If it needs
code, it produces tickets. If it needs a prototype, that's a spike ticket.

Tickets and journeys remain separate domain tables. Investigations get their
own table when built (deferred to M1.5; the `subject_type` enum accepts them
from M1a).

## 5. Architecture

```
Policy Layer       — repo-owned WORKFLOW.toml + PROMPT.md
Tracker Layer      — already exists: SQLite + actions.py + markdown sync
Orchestrator       — new: src/kitchen.py
Workspace Manager  — new: src/workspaces.py
Runner Layer       — new: src/runners.py
                       AgentRunner    (extends existing workflow_runs)
                       ScenarioRunner (existing tests/scenario_runner.py)
                       GapAnalyzer    (new: classifies red scenario runs)
Evidence Store     — ~/.claude/ticket-takeaway/evidence/, with retention
Audit Layer        — activity_events table; agent events visually distinct
Status Surface     — Kitchen view (new) + per-card badges + live ticket view
```

`serve.py` calls `kitchen.start(get_db, settings)` on startup and
`kitchen.stop()` on shutdown. API handlers proxy to functions in `kitchen.py` /
`actions.py`. No orchestration logic inside `serve.py` itself.

Tracker Adapter is collapsed: we *are* the tracker. Polling is a SQLite read.

**SQLite threading policy:** Kitchen background threads share the existing
`_db_lock` for writes. Reads use short transactions. All connections set
`PRAGMA busy_timeout=5000`. The dispatch claim transaction uses
`BEGIN IMMEDIATE`. Single-instance server is the assumption; multi-instance
would require additional coordination (out of scope).

## 6. Data model

Three new tables + two columns on `tickets`. Migration #6. Existing `tickets`
and `journeys` tables otherwise unchanged.

```sql
-- New tickets columns (M1a)
ALTER TABLE tickets ADD COLUMN no_test_required INTEGER NOT NULL DEFAULT 0
  CHECK (no_test_required IN (0, 1));
ALTER TABLE tickets ADD COLUMN no_test_required_note TEXT NOT NULL DEFAULT '';
-- (App-level helper enforces: no_test_required=1 requires non-empty note.
--  Pure SQL CHECK across both columns is awkward in SQLite ALTER; enforced in actions.py.)

-- Intent: how the human (or agent) wants this subject treated.
CREATE TABLE automation_subjects (
  project_id      TEXT NOT NULL,
  subject_type    TEXT NOT NULL CHECK (subject_type IN ('ticket','journey','investigation')),
  subject_id      TEXT NOT NULL,
  automation_mode TEXT NOT NULL DEFAULT 'manual'
                  CHECK (automation_mode IN ('manual','auto','held')),
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
  subject_type       TEXT NOT NULL CHECK (subject_type IN ('ticket','journey','investigation')),
  subject_id         TEXT NOT NULL,
  runner_kind        TEXT NOT NULL CHECK (runner_kind IN ('agent','scenario','gap_analyzer')),
  status             TEXT NOT NULL CHECK (status IN
                       ('queued','preparing','running','needs_input',
                        'succeeded','failed','stalled','cancelled')),
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
  summary            TEXT,
  metadata_json      TEXT NOT NULL DEFAULT '{}',
  evidence_dir       TEXT,
  evidence_status    TEXT NOT NULL DEFAULT 'live'
                     CHECK (evidence_status IN ('live','summarised','pruned')),

  -- needs_input pause
  needs_input_prompt TEXT,

  -- relationships
  attempt            INTEGER NOT NULL DEFAULT 1,
  parent_run_id      INTEGER,
  retry_kind         TEXT CHECK (retry_kind IS NULL OR retry_kind IN ('resume','fresh')),
  triggered_by       TEXT NOT NULL CHECK (triggered_by IN
                       ('human','run-now','journey-cascade','retry','scheduled','pr-merge'))
);

CREATE INDEX runs_subject_latest ON runs (project_id, subject_type, subject_id, id DESC);
CREATE INDEX runs_active ON runs (status) WHERE status IN ('queued','preparing','running','needs_input');
CREATE INDEX runs_evidence_age ON runs (finished_at, evidence_status);

-- DURABILITY GUARANTEE: only one active run per subject, ever.
CREATE UNIQUE INDEX one_active_run_per_subject
  ON runs (project_id, subject_type, subject_id)
  WHERE status IN ('queued','preparing','running','needs_input');

-- Audit: every state-changing event.
CREATE TABLE activity_events (
  id               INTEGER PRIMARY KEY,
  project_id       TEXT NOT NULL,
  subject_type     TEXT NOT NULL CHECK (subject_type IN ('ticket','journey','investigation')),
  subject_id       TEXT NOT NULL,
  actor_type       TEXT NOT NULL CHECK (actor_type IN ('human','agent','system')),
  actor_id         TEXT,                            -- run_id (string) when agent; user identifier when human; NULL for system
  event_kind       TEXT NOT NULL,                   -- see vocabulary in §9
  payload_json     TEXT NOT NULL,                   -- {before, after, ...} per §9
  occurred_at      TIMESTAMP NOT NULL,
  discarded_run_id INTEGER                          -- set when a run rollback retracts this event
);
CREATE INDEX activity_subject ON activity_events (project_id, subject_type, subject_id, occurred_at DESC);
CREATE INDEX activity_run ON activity_events (actor_type, actor_id) WHERE actor_type = 'agent';
```

**Deliberate non-decisions:**
- No `automation_state` enum stored anywhere. Derived from the latest non-terminal `runs` row.
- No `last_run_id` cache. Indexed `MAX(id)` query.
- No file-based audit log. `activity_events` is the table.

**Missing-row rule:** absence of an `automation_subjects` row is treated as
`automation_mode='manual'`. Rows are created lazily on first toggle from `set_mode`.

**Timestamp policy:** all writes use `utcnow_iso()` helper —
`datetime.now(timezone.utc).isoformat()`. Reads parse stored ISO strings.

## 7. Eligibility (computed, never stored)

```python
def eligibility(subject, conn) -> EligibilityResult:
    """Returns (eligible: bool, reasons: list[str]).
    Always returns reasons even when eligible (for UI 'why' tooltips)."""
```

**Eligibility for tickets:**
- `automation_mode == 'auto'` (missing row → `manual` → not eligible)
- AND `section IN ('Backlog', 'WIP', 'For Review')` (canonical names)
- AND `draft = 0 AND archived = 0`
- AND description present (non-empty text)
- AND criteria present (≥1 criterion row)
- AND **deps clear** (see below)
- AND **tests covered** (see below)
- AND no active run (the unique index enforces; pre-flight checked too)

**Deps clear means:**
- Every dependency ID resolves to a real ticket (case-insensitive lookup,
  canonicalised on read). Missing/unknown ID → blocks.
- Dep ticket is "cleared" iff `section='Done' OR status IN ('done','released')`.
- `wontdo` does NOT clear — must be explicitly removed if no longer needed.
- Archived dep treated as missing → blocks. (Forces explicit cleanup.)

**Tests covered means** (any one):
- A `readiness_flags` row exists with `flag='tests'` AND non-empty `content`, OR
- The ticket has at least one `journey_tickets` link and that linked journey
  compiles (`compile_to_manifest()` returns) AND validates
  (`validate_manifest()` passes), OR
- `tickets.no_test_required = 1 AND no_test_required_note != ''`.

Empty readiness rows do NOT satisfy. The `no_test_required` bypass requires a
non-empty rationale note (enforced in actions.py helper, surfaced in UI as
mandatory text field when checkbox is set).

**Eligibility for journeys:**
- `automation_mode == 'auto'`
- AND `compile_to_manifest()` succeeds AND `validate_manifest()` passes
- AND no active run

Selector resolution is a runtime concern, not eligibility — surfaces as a
`failed` run with `error_class='selector_not_found'`.

## 8. Concurrency

Two caps, configured in `WORKFLOW.toml`:

- `max_concurrent_runs = 3` — global cap (system-wide, all projects, all runners)
- `max_concurrent_per_project = 1` — per-project cap

Per-project default of 1 prevents a noisy project from monopolising slots. Both
caps are hot-reloadable on tick.

**Capacity counts only `('preparing', 'running')`.** `queued` and
`needs_input` do NOT consume slots. Rationale: `needs_input` runs are paused
waiting for human input; the worker process can release the slot. M3 will
implement: when a run transitions to `needs_input`, the worker exits and the
slot is freed; when the user responds, the run goes back to `queued` to be
re-picked-up by the dispatcher.

Orchestrator poll tick (in `kitchen.py`):
1. Reconcile: refresh state of all active runs; expire stalled ones (heartbeat).
2. Compute available slots: `max_concurrent_runs - count(runs WHERE status IN ('preparing','running'))`.
3. Fetch eligible subjects across all projects (sorted by priority, age, identifier).
4. For each candidate: skip if its project already has `max_concurrent_per_project` active.
5. Dispatch (`BEGIN IMMEDIATE` → INSERT into `runs` at `status='queued'` with
   `claim_owner` set → `COMMIT`) until slots are exhausted. The
   `one_active_run_per_subject` partial unique index makes double-dispatch
   impossible.

## 9. Activity event vocabulary

Two tiers: **M1a spine** (frozen at M1a) and **M1b expansion** (added when
serve.py direct writes are routed through actions.py).

**M1a spine (frozen):**

| `event_kind` | Emitted when | Required `payload_json` keys |
|---|---|---|
| `mode_changed` | automation_mode flipped | `before`, `after`, `reason?` |
| `hold_set` | mode → held | `before` (prior mode), `after` ('held'), `reason` |
| `hold_cleared` | mode → manual or auto from held | `before` ('held'), `after`, `prior_reason` |
| `section_change` | ticket section moved | `before`, `after` |
| `status_change` | ticket status badge changed | `before`, `after` |
| `criteria_check` | acceptance criterion ticked/unticked | `criterion_id`, `before`, `after` |
| `run_started` | runs row inserted at status `queued` (claim taken) | `run_id`, `runner_kind`, `triggered_by` |
| `workspace_created` | workspace dir created (or reused) | `path`, `reused` |
| `agent_output` | agent emitted text we want to remember | `run_id`, `summary`, `tokens?` |
| `needs_input` | run paused for user | `run_id`, `prompt` |
| `input_provided` | user responded | `run_id`, `response_excerpt` |
| `run_succeeded` | terminal success | `run_id`, `summary`, `duration_ms` |
| `run_failed` | terminal failure | `run_id`, `error_class`, `error_message` |
| `run_stalled` | killed for inactivity | `run_id`, `last_heartbeat_age_ms` |
| `run_cancelled` | "Stop run" pressed | `run_id`, `reason?` |
| `run_discarded` | "Discard run" pressed (rollback) | `run_id`, `reason`, `reverted_event_count` |
| `hook_started` | a workspace hook started | `hook`, `run_id?` |
| `hook_succeeded` | hook completed 0-exit | `hook`, `duration_ms`, `run_id?` |
| `hook_failed` | hook exited non-zero or timed out | `hook`, `error_class`, `error_message`, `run_id?` |

**M1b expansion** (when serve.py direct writes are routed through actions.py):

| `event_kind` | Required `payload_json` keys |
|---|---|
| `ticket_created` | `id`, `title`, `section` |
| `ticket_deleted` | `id`, snapshot fields for restore |
| `field_changed` | `field`, `before`, `after` (one event per field) |
| `dependency_changed` | `before` (list), `after` (list) |
| `readiness_changed` | `kind`, `before`, `after` |
| `attachment_added` | `attachment_id`, `kind`, `label` |
| `attachment_removed` | `attachment_id`, `kind`, `label` |
| `journey_linked` | `journey_id`, `step_id?` |
| `journey_unlinked` | `journey_id`, `step_id?` |
| `criteria_added` | `criterion_id`, `text` |
| `criteria_removed` | `criterion_id`, `text` |
| `criteria_changed` | `criterion_id`, `before`, `after` (text edit, distinct from `criteria_check` toggle) |

**Wire format invariant:** every mutable event includes `before` and `after`
keys (or per-event inverse payload sufficient to reconstruct the prior state).
Events written under M1a remain valid under M1b.

**Internal side-effect rule:** events are emitted from inside `actions.py`
mutation functions, INCLUDING side effects that run inside other action
functions. Examples: `auto_promote_parents()` emits `section_change` for the
promoted parent; the scheduled-events poller emits its own events when it
auto-accepts. The "spine" must include side effects from within actions, not
just top-level calls.

## 9b. Actor attribution

Every audit event needs reliable `actor_type` and `actor_id`. M1a introduces an
`ActorContext` passed explicitly through every action.py mutation:

```python
@dataclass(frozen=True)
class ActorContext:
    actor_type: str  # 'human' | 'agent' | 'system'
    actor_id: str | None = None  # run_id when agent; user identifier when human; None for system

    @classmethod
    def human(cls, user_id: str | None = None) -> "ActorContext": ...
    @classmethod
    def agent(cls, run_id: int) -> "ActorContext": ...
    @classmethod
    def system(cls) -> "ActorContext": ...
```

Every mutation in actions.py accepts `actor: ActorContext = ActorContext.human()`
as its last param. The mutation and its `activity_events` row are written in
the **same DB transaction** — never in two separate transactions. Helper
`emit_event(conn, project_id, subject_type, subject_id, event_kind, payload, actor)`
inserts the row inside the caller's open transaction.

API entrypoints in `serve.py` construct the `ActorContext` from the request
(default: `human()`) and pass it through. Background threads (scheduler,
auto-promote) use `system()`. Agent-driven mutations (when M3 lands) use
`agent(run_id)`.

## 10. Workspace policy

- **`git worktree` default.** Clone fallback only when the project isn't a git repo.
- **Path:** `~/.claude/ticket-takeaway/workspaces/{project_id}/{subject_type}/{subject_id}/`
- **Persistent while subject is active.** Reused across runs.
- **On retry, resume in the same worktree** — worktree IS the
  collision-prevention; same agent picking up its own incomplete state can't
  conflict with anything else.
- **"Retry fresh"** as a separate, lesser-used action: wipes worktree state,
  re-runs `after_create`. Useful when previous state is corrupt.
- **Retention:** archived after the merged-PR detection ages past
  `workspace.retention_days_after_done` (default 21); cleaned thereafter.
- **Hooks:** all hooks run with `cwd = workspace_path`. The worktree itself is
  created by the workspace manager BEFORE `after_create` runs.
- **Safety invariants** (lifted from Symphony §9.5):
  - runner cwd MUST equal workspace_path
  - workspace_path MUST live inside workspace_root
  - workspace key sanitized to `[A-Za-z0-9._-]`

### Hook execution contract

| Hook | Cwd | Shell | Timeout | Failure semantics |
|---|---|---|---|---|
| `after_create` | workspace | `bash -lc` | `hooks.timeout_ms` (default 60000) | Fatal → run fails with `error_class='hook_after_create'`. One-time per workspace; guarded by `.kitchen-bootstrap-complete` marker file. |
| `before_run` | workspace | `bash -lc` | same | Fatal → run fails with `error_class='hook_before_run'`. |
| `after_run` | workspace | `bash -lc` | same | Logged, ignored. |
| `before_remove` | workspace | `bash -lc` | same | Logged, ignored. |

Every hook execution emits `hook_started` followed by `hook_succeeded` or
`hook_failed`. Hook env inherits the kitchen process env (no special scrubbing
in M3; tighten later if needed). Stdout/stderr are captured to the run's
evidence dir as `hooks/{hook_name}.{started_at}.log`.

## 10b. Branch and PR contract

For each ticket-runner attempt:

- **Branch name:** `kitchen/{subject_type}/{subject_id}` (e.g. `kitchen/ticket/B-42`).
  Subject type included to prevent collisions between tickets and journeys
  sharing numeric IDs.
- **Persistent across resume retries.** "Retry fresh" force-recreates from base.
- **Base ref:** `agent.base_ref` in WORKFLOW.toml (default `origin/main`). If the
  configured value already starts with `origin/`, used as-is; otherwise prefixed.
- **Worktree creation:**
  ```
  git fetch origin
  git worktree add -b kitchen/ticket/<id> <path> <fully-qualified base ref>
  ```
  Never touches local `main`, so dirty main is irrelevant.
- **Push:** agent runs `git push -u origin <branch>` and `gh pr create` (already
  a project convention).
- **Merge: human-initiated only.** Kitchen NEVER runs `gh pr merge`. PR merging
  is policy work outside the scope of this plan.
- **Merge detection:** reconciliation tick runs `gh pr view --json mergedAt` for
  each open PR linked to an active subject. When a merge is detected:
  - ticket auto-moves to `Done` (emit `section_change` with `actor_type='system'`)
  - any active run is marked `succeeded` if not already
  - linked journeys queue a re-run with `triggered_by='journey-cascade'`
- **Worktree removal:** after `workspace.retention_days_after_done` elapses
  post-merge.

## 11. Repo-owned policy files

Two files at repo root, both version-controlled:

`WORKFLOW.toml` (parsed via Python 3.11 `tomllib` stdlib, no dependency):

```toml
[automation]
default_mode = "manual"
max_concurrent_runs = 3
max_concurrent_per_project = 1

[agent]
command = "claude -p"
sandbox = "workspace-write"
max_turns = 20
base_ref = "origin/main"

[workspace]
retention_days_after_done = 21

[evidence]
live_days = 30
summarised_days = 60

[hooks]
timeout_ms = 60000
after_create = """
# Fresh worktree: bootstrap deps. Runs once per workspace lifetime.
npm install --silent
cp ~/.claude/ticket-takeaway/secrets/.env.local .env.local 2>/dev/null || true
"""
before_run = """
# Runs before every attempt. Idempotent.
git pull --rebase origin main
"""
```

`PROMPT.md`:

```markdown
You are working on Ticket Takeaway ticket {{subject.id}}: {{subject.title}}
...
```

**Discovery:** repo root only. No `.ticket-takeaway/` directory in M3-M5.

## 12. UI surfaces

**Per-project board** (existing kanban, light additions):
- Card badge: idle dot · queued spinner · running spinner · ⚠ needs-input · ✗ failed · 🔒 held · ⏹ cancelled
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

**Kitchen view (new landing page at `/`):**
- **Needs Me** — runs in `needs_input`, eligible-but-failed; cancelled runs
  are NOT here (they show in board filter "Cancelled" and offer Retry)
- **Running** — active worktrees (status in `preparing|running`)
- **Ready To Delegate** — eligible subjects with no active run
- **Held** — paused subjects with reasons
- **Projects** — per-project health rows
- Watched-projects toggle in registry: `watched: true/false`

**Project picker moves to `/projects`** (was at `/`). The new `/` is Kitchen.
No 301 redirect — old root now intentionally lands on Kitchen for the same
audience that previously saw the picker.

**`needs_input`: three entry points, one event.** The Kitchen queue, the ticket
detail panel, and the card badge popover all submit to the same endpoint and
write one `input_provided` event. User picks whichever surface they're on.

## 13. Evidence storage and rotation

- **Location:** `~/.claude/ticket-takeaway/evidence/{run_id}/`
- **Auto-attached:** every run that produces evidence registers an attachment
  row of `kind = 'run_evidence'` so it shows up in the ticket's existing
  attachments tab.
- **Retention pipeline** (background thread, runs daily):

| Age | `evidence_status` | What's there |
|---|---|---|
| 0–`live_days` | `live` | Everything raw |
| live → live+`summarised_days` | `summarised` | Auto-generated `summary.md`, screenshots compressed, raw transcript gzipped |
| past summarised | `pruned` | Only `summary.md` remains |

The `runs` row stays forever — only on-disk artifacts are rotated.

## 14. Audit + live ticket view

**Audit / rollback:**
- Every spine event (M1a) and expanded event (M1b) is an `activity_events` row.
- Agent events: `actor_type='agent'`, `actor_id=<run_id>`. Human:
  `actor_type='human'`. System (auto-promote, scheduler, PR-merge detection):
  `actor_type='system'`.
- Ticket detail's history tab shows them inline with distinct visual treatment
  (agent rows have a small bot icon + workspace link; discarded rows
  struck-through).
- Undo/redo: Ctrl+Z on a human action undoes it (uses `before` value). Agent
  actions are not undoable via Ctrl+Z — they're rolled back via "Discard run"
  which reverts every event from that run as a unit (issuing fresh
  `actor_type='system'` events for each reversal).

**Live ticket view (open a ticket → see what's happening right now):**
- If there's an active run, the ticket detail shows a live panel at the top.
- Server-pushed via existing 2s polling (no websockets needed).
- "Open transcript" gives a live-tailing view of the full agent stream.

## 15. Phasing

**M1a — Spine** *(~1 wk)*
- Migration #6: `automation_subjects`, `runs` (with `cancelled` + indexes +
  partial unique), `activity_events`. New columns on `tickets`:
  `no_test_required`, `no_test_required_note`.
- `subject_type` enum accepts `ticket | journey | investigation` from day one.
- `eligibility(subject, conn)` pure function in `actions.py` per §7.
- `ActorContext` introduced; every mutation in actions.py takes
  `actor: ActorContext = ActorContext.human()`; events written in same tx.
- Auto toggle in ticket detail (Manual / Auto / Held + reason).
- Derived badge on every card.
- "No tests required" checkbox + mandatory note field.
- Spine event vocabulary (§9 M1a list) emitted at actions.py call sites,
  including from internal side-effect functions (`auto_promote_parents`,
  scheduled-event poller).
- `utcnow_iso()` helper. `BEGIN IMMEDIATE` for dispatch claim.
  `PRAGMA busy_timeout=5000`.
- Module skeletons: `src/kitchen.py`, `src/workspaces.py`, `src/runners.py`
  with stubs.
- **No runner, no workspace, no Kitchen view yet. Audit UI is partial — only
  spine events appear in history. Documented as such in the UI itself.**

**M1b — Audit completion** *(~1 wk)*
- Route serve.py direct writes through new `actions.py` helpers.
- Expanded event vocabulary (§9 M1b list).
- Full history tab on ticket detail; discard/undo paths cover all event kinds.
- Wire format invariant maintained — events written under M1a remain valid.

**M2 — Kitchen view + board filter chips** *(~1 wk)*
- New landing page at `/`, replacing project picker as default.
- Project picker relocated to `/projects`.
- Filter chips on per-project board.
- Watched-projects flag in registry.
- Still no execution.

**M3 — Workspace manager + runner + live view** *(~2 wks)*
- Workspace manager with worktree default + hooks + safety invariants.
- `Runner` ABC + `AgentRunner` + `ScenarioRunner`.
- WORKFLOW.toml + PROMPT.md reader; replaces relevant settings rows.
- Orchestrator background thread inside `serve.py` (poll → eligible → claim →
  dispatch → reconcile → heartbeat → PR merge detection).
- Branch + PR contract per §10b.
- Live run panel on ticket detail.
- `needs_input` inline panel; worker process exits on pause and re-spawns on resume.
- "Run now" / "Stop run" / "Discard run" / "Retry" / "Retry fresh" buttons.
- Audit log captures agent events.
- Hook execution per §10 contract.

**M4 — Closed loop + GapAnalyzer** *(~1-2 wks)*
- `GapAnalyzer` runner classifies red scenario runs into typed gaps:
  `missing_selector | missing_screen | missing_feature | ambiguous_goal |
  external_dependency | test_harness_gap`.
- "Accept gap → file ticket" with pre-populated D/C from gap context.
- `journey_tickets` linkage shown on both card sides.
- Auto re-run linked journey when its dependent tickets reach Done
  (`triggered_by='journey-cascade'`).
- Eat our own dog food: ticket-takeaway becomes the first project under Kitchen.

**M5 — Evidence rotation pipeline** *(~3 days)*
- Daily background job summarises 30+ day runs, prunes 90+ day artifacts.
- `evidence_status` field driven by the pipeline.
- Summary template + LLM call.

**Deferred:** Investigations table + UI. Codex-app-server-style fleet
execution. Multi-agent runner orchestration. PR auto-merge policy.

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
- PR auto-merge by Kitchen. Human stays the merge gate.
- Multi-instance server coordination. Single instance assumed.

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
- **v3** — consultant round 1 feedback: `automation_mode` vocabulary cleaned
  (`manual | auto | held`); durable claim + heartbeat; `queued` status added;
  modules separated from `serve.py`; activity event vocabulary frozen at M1;
  per-project concurrency cap added.
- **v4** — consultant round 2 feedback: section names corrected to canonical
  (`Backlog | WIP | For Review`); `draft`/`archived` excluded; deps-clear and
  tests-covered semantics defined precisely; `no_test_required`
  + note columns; `cancelled` status added; `comment_added` removed;
  `{before, after}` payload standard; rollback semantics specified; M1 split
  into M1a (spine) + M1b (audit completion); `ActorContext` for actor
  attribution with same-tx event writes; branch + PR contract specified
  (`kitchen/{subject_type}/{subject_id}` naming, base ref handling, merge
  human-only with detection); WORKFLOW.toml + PROMPT.md (replaces YAML front
  matter, requires Python 3.11+); paths consolidated under
  `~/.claude/ticket-takeaway/`; project picker moved to `/projects`; CHECK
  constraints on enum columns; `triggered_by` enumerated; `needs_input`
  releases worker slot at M3; hook execution contract codified; spine events
  fire from internal side effects too; `run_cancelled`, `hook_started`,
  `hook_succeeded`, `hook_failed` added to spine; `criteria_added`,
  `criteria_removed`, `criteria_changed` added to M1b expansion.
