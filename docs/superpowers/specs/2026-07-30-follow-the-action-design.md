# Follow the Action — global activity-follow mode for the kanban

**Date:** 2026-07-30
**Status:** Approved design (brainstorm complete; architecture chosen by user)

## Problem

Agents work across many projects, mutating tickets constantly (moves, creates, status
changes, Kitchen runs). The user keeps a dedicated monitor showing a Ticket Takeaway
board, but the board on screen rarely corresponds to the project where the action is
happening — so the visualisation benefit is lost.

## Goal

A toggleable **Follow mode**: when enabled on any kanban board, the dashboard watches
activity across **all watched projects** and directs attention to each action — scrolling
to and spotlighting the affected card, captioning what happened and who did it, and
**navigating to another project's board** when the action is elsewhere. The board becomes
a passive "mission control" screen.

## Decisions locked during brainstorm

| Question | Decision |
|---|---|
| Which events animate | **Everything** — every `activity_events` row (moves, creates, status, runs, field/criteria edits, hooks, pauses) |
| Queue semantics | **Live + coalesce bursts** — start from "now" on enable; consecutive events on the same ticket collapse into one step; no historical replay |
| Follow-then-return? | No. Mode stays wherever the last action was ("go to the next action") |
| Cross-board mechanism | **Approach A: real navigation + persisted cursor**, masked by a transition overlay. No SPA surgery, no duplicate board renderer |

## Architecture

```
activity_events (single cross-project SQLite table, monotonic id)
        │
        ▼
GET /api/activity/feed?since_id=&limit=     ← NEW global endpoint (serve.py)
        │  enriched: project_name, ticket_title, section, actor_name
        ▼
Follow engine (new build_follow_mode_js() in generate.py, board pages only)
  poll 2s → queue → coalesce → play steps at STEP_MS
        │                         │
        │ same-project step       │ cross-project step
        ▼                         ▼
  spotlight card +          departure overlay → location.href =
  ticker caption            /{pid}/kanban → arrival spotlight (sessionStorage handoff)
```

Card *movement* animation remains the job of the existing 2s HTML-diff poll
(`patchCards()` — `just-moved` / `card-enter` / `card-exit` / `content-flash`).
Follow mode never mutates the board; it only directs attention.

## Server

### `GET /api/activity/feed` (global route, no project prefix)

Query params:
- `since_id` (int, optional) — return events with `id > since_id`, ascending, oldest first.
- `limit` (int, default 100, max 500).

Response:

```json
{
  "latest_id": 12345,
  "events": [
    {
      "id": 12301,
      "project_id": "goodform",
      "project_name": "GoodForm",
      "subject_type": "ticket",
      "subject_id": "B-12",
      "ticket_title": "OAuth flow",
      "section": "WIP",
      "event_kind": "section_change",
      "payload": {"before": "Backlog", "after": "WIP"},
      "actor_type": "agent",
      "actor_name": "bounce-workflow",
      "occurred_at": "2026-07-30T12:34:56+00:00"
    }
  ]
}
```

Rules:
- **No `since_id` → `latest_id` only, empty `events`** (client cursor init; enforces
  live-only semantics).
- Filter `discarded_run_id IS NULL` (never replay reverted history).
- Filter to **watched** projects (registry `watched` flag, kitchen-feed precedent),
  **plus** the `_kitchen` sentinel project (pause/resume lifecycle banners).
- Enrichment: `project_name` from the in-memory registry cache; `ticket_title` +
  current `section` via join to `tickets` (nullable — ticket may be deleted);
  `actor_name` via the existing run→workflow-name resolution used by
  `get_ticket_activity()` (extract/shared helper, do not duplicate).
- Query is `WHERE id > ? ORDER BY id ASC LIMIT ?` — rowid B-tree scan, no new index.

### Placement & parity

- Logic in `actions.get_activity_feed(conn, since_id, limit, project_ids)` — importable,
  TDD-testable, no server required.
- Route handler in `serve.py` global routes (alongside `/api/kitchen/feed`).
- CLI: `tickets-cli.py feed [--since <id>] [--limit <n>]` — thin read-only wrapper
  (feature-parity rule: actions + API + CLI).

## Client — follow engine

New `build_follow_mode_js()` + `build_follow_mode_css()` in `generate.py`, emitted on
board pages only. The mode is meaningless off-board (Kitchen/Workflows pages: out of
scope v1).

### State

| Key | Store | Meaning |
|---|---|---|
| `tt-follow-enabled` | localStorage | mode on/off, global across projects |
| `tt-follow-cursor` | localStorage | id of last **played** event |
| `tt-follow-arrival` | sessionStorage | step to play immediately after cross-project navigation |

The in-memory queue is **never persisted** — it is always rebuildable by fetching
`since_id=cursor`. Navigation therefore loses nothing.

### Loop

1. Poll `/api/activity/feed?since_id=<cursor>` every 2000 ms (house cadence).
   Pause polling while `document.hidden`; on visibility regained, re-init cursor to
   `latest_id` (live semantics — no stale replay).
2. Append new events to the queue; **coalesce** contiguous events sharing
   (project_id, subject_id) into one step. Headline = highest-precedence kind:
   `section_change` > `ticket_created` > `status_change` > run lifecycle
   (`run_started`/`run_succeeded`/`run_failed`/…) > `field_changed`/`criteria_*`/rest.
   Remaining events in the group render as "+N more".
3. Play one step per `STEP_MS` (constant, 1600 ms; code-tweakable, no UI slider v1).
4. **Overflow guard:** if the unplayed backlog exceeds 40 events, jump cursor to
   `latest_id`, drop the queue, ticker shows "skipped N actions".
5. Cursor advances only when a step **finishes playing** (or is skipped by the guard).

### Playing a step

- **Same project:** `scrollIntoView({behavior:'smooth', block:'center'})` on the card,
  apply a spotlight ring class (reuse the `card-moved` glow vocabulary; new
  `follow-spotlight` keyframes in the same style), update the ticker caption. Caption
  colored by the existing `EVENT_KIND_GROUPS` / `EVENT_GROUP_COLORS` taxonomy
  (constants.py:277-331 — mirror into the emitted JS the same way the activity tab does).
- **Card not in DOM** (collapsed bottom section, draft, deleted ticket): caption-only
  step; pulse the section header when one exists. Never auto-expand sections.
- **Non-ticket subjects** (`subject_type` journey/investigation): caption-only step —
  still navigates to the owning project's board first if elsewhere.
- **Different project:** departure overlay — board dims, caption shows
  "→ {project_name} · {actor} {action} {ticket}" (~1000 ms) — then write
  `tt-follow-arrival`, persist cursor, `location.href = /{pid}/kanban`. On board init
  with follow enabled and a fresh arrival payload (< 30 s old): play the arrival step
  (spotlight + caption), then resume the normal loop.
- **`_kitchen` sentinel events** (kitchen_paused/resumed): ticker banner only, never
  navigation.
- The engine plays the step **after** the board's own diff-poll has (or will have)
  applied the DOM change; ordering races with `patchCards()` are acceptable — spotlight
  works on the card wherever it currently is.

### Interaction guard — never yank the board from a human

Playback and navigation **suspend** while any of: ticket overlay open, drag in progress,
text input focused, bounce overlay open. Events keep queueing (coalescing + overflow
guard bound it); playback resumes when the interaction ends.

### Motion

`prefers-reduced-motion: reduce` → no smooth scroll, no keyframes; ticker still updates,
navigation still happens (instant).

## UI

- **Toggle:** a "Follow" chip in the board header next to the existing filter chips.
  Doubles as the state indicator — pulsing dot (reuse `kitchen-pulse`) when live.
  Same markup/CSS style as existing chips; try/catch localStorage like every `tt-*` key.
- **Ticker bar:** slim fixed bar at the bottom of the board, above toasts, visible only
  when the mode is on. Contents: current step caption with event-group color as left
  border; queue depth when > 0 ("· 3 queued"); quiet "following · live" idle state.
  Clicking the caption opens that ticket's overlay via the existing `#ticket/{id}`
  hash router (same-project only; cross-project captions navigate on click).
- **Departure/arrival overlay:** full-board dim + centered caption, reusing the
  `panelSlide` animation vocabulary.

## Error handling

- Feed fetch failure: silent retry next tick; after 5 consecutive failures the ticker
  shows a "feed offline" state. The mode never self-disables.
- Unknown `event_kind` (vocabulary grows): generic caption from the kind string.
- Malformed/stale `tt-follow-arrival`: discard, continue live.
- Navigation target 404 (project removed from registry): clear arrival payload, stay put,
  ticker notes the skip.

## Out of scope (v1, deliberate)

- Speed slider UI (constant in code).
- "Go back to what you were looking at" return mode (user chose go-to-next-action).
- Nav-rail per-project activity badges.
- Per-human actor identity (backend records `actor_id = None` for all humans; captions
  say "human" generically).
- Follow mode on Kitchen/Workflows/Journeys pages.
- SSE/WebSocket push (polling is the house pattern; docs/KITCHEN.md §16 explicitly
  excludes push infra).

## Testing & verification

- **TDD (the verify gate):** `tests/test_tdd_activity_feed.py` covering
  `actions.get_activity_feed`: since_id ordering + limit; `latest_id` init shape;
  discarded-run exclusion; watched-project filtering + `_kitchen` passthrough;
  title/section/actor_name enrichment incl. deleted-ticket nulls. Pure logic, no server.
- **Smoke (deliberate run, real DB):** endpoint returns wrapped shape; cursor init call.
- **E2E/manual (mandatory before "shipped"):** with serve.py running and follow enabled,
  drive a CLI move in another project; verify via Chrome DevTools MCP: departure overlay
  → navigation → arrival spotlight → ticker caption. Screenshot the ticker + spotlight
  states for the user (user is remote; visual sign-off happens from screenshots).
- **JS unwrap rule:** feed consumers use `data.events || data || []` (wrapped-response
  convention).

## Deployment notes

- Edits touch `generate.py` (JS/CSS), `serve.py` (endpoint), `actions.py`, `constants.py`
  (if event-precedence table lands there), `tickets-cli.py`.
- After editing: `cp` runtime copies to `~/.claude/ticket-takeaway/`, **regenerate the
  static dashboard HTML** (`python3 src/generate.py` — generate.py edits are invisible
  without it), restart serve.py. Canonical production writer: llm-node, port 8788.
- Feature branch off main per house workflow; TT is OpenSpec-enrolled — ticket +
  `spec --lane A` at implementation start.
