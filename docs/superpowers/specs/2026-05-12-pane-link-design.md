# tmux pane ↔ ticket link (v1)

**Date:** 2026-05-12
**Branch:** `feat/pane-link`
**Tags:** `cli-integration`, `kitchen-adjacent`

## Problem

The operator vibe-codes in a CLI/TUI (their warm, cached context) while TT runs the broader operations layer (multi-project, semi-automated). Today the two surfaces are disconnected: TT can't see what's happening in the user's coding session, and the agent in that session doesn't natively know which ticket it's working on. The user has to either context-switch back to the terminal to act, or pass ticket data around manually.

The smallest thing that closes the gap: a named link between a tmux pane and a ticket, with TT able to read the pane's tail and surface "needs attention" events.

## Scope

**In:**

- Bind a tmux pane to a ticket via a CLI command, on the same machine as `serve.py`.
- Capture the pane's tail (~200 lines) every 2 seconds.
- Surface the tail in the ticket detail view in the dashboard.
- Detect "question" and "exception" states heuristically; pulse the ticket card when detected.
- Send keys back to the bound pane from the GUI (read + write).
- Expose `tt current` so the agent in the pane can read its bound ticket structurally.

**Out (deferred, named explicitly):**

- Cross-machine capture (pane lives on a different tailnet host than `serve.py`).
- Non-tmux terminals.
- Model-emitted attention markers (`::tt-needs-input::`) — fast follow once heuristic is exercised.
- Reabsorption / parent feature summary updates.
- Surface A clickable feature characteristics in the side pane.
- MCP server exposing TT operations.
- Reactive workflow rules driven by pane content.
- xterm.js full ANSI rendering — v1 strips escapes.

## Architecture

```
[ tmux pane (local) ]
       │  capture-pane every 2s (worker)
       │  send-keys (POST endpoint)
       ▼
[ pane_links table (sqlite) ]
       │
       ├──→ actions.py (link/unlink, lookup, attention state)
       ├──→ serve.py API + capture worker + send-keys endpoint
       ├──→ tickets-cli.py (tt link / current / unlink / panes)
       └──→ generate.py + ticket detail JS (live tail panel, send box, card pulse)
```

One new table, one new background thread, additions to existing layers. No new top-level modules.

## Data model

**Migration 19** — `pane_links` table:

| Column                  | Type    | Notes                                                                  |
|-------------------------|---------|------------------------------------------------------------------------|
| `id`                    | INTEGER | PK                                                                     |
| `ticket_id`             | TEXT    | NOT NULL, e.g. `B-12`                                                  |
| `project_id`            | TEXT    | NOT NULL                                                               |
| `pane_address`          | TEXT    | NOT NULL, UNIQUE — tmux pane id from `$TMUX_PANE` (e.g. `%23`)        |
| `host`                  | TEXT    | NOT NULL — local hostname; v1 only honours rows where host = local     |
| `pane_descriptor`       | TEXT    | session:window.pane (human readable, secondary descriptor)             |
| `created_at`            | INTEGER | unix timestamp                                                         |
| `last_captured_at`      | INTEGER | NULL until first successful capture                                    |
| `status`                | TEXT    | `active` / `stale`                                                     |
| `attention_state`       | TEXT    | `none` / `question` / `exception` / `idle`                             |
| `attention_detected_at` | INTEGER | NULL when state = `none`                                               |
| `tail_text`             | TEXT    | latest capture, bounded to ~200 lines / 8KB                            |

`UNIQUE(pane_address)` — one ticket per pane. Re-linking a pane to a new ticket replaces (with an `activity_events` row noting the previous binding).

## CLI

In `tickets-cli.py` (and deployed copy in `~/.claude/ticket-takeaway/`):

- `tt link <ticket-id>` — uses `$TMUX_PANE` and local hostname; inserts or replaces.
- `tt current` — reads `$TMUX_PANE`, prints bound ticket as markdown (title, section, status, parent, description, criteria). Exit 1 if no link.
- `tt unlink` — removes the row for `$TMUX_PANE`.
- `tt panes` — list all current links (debug / inspection).

All commands fail helpfully if `$TMUX_PANE` is unset (user not inside tmux).

## App layer (`actions.py`)

- `link_pane(ticket_id, project_id, pane_address, host, pane_descriptor)` — emits `pane_linked` event in same transaction.
- `unlink_pane(pane_address)` — emits `pane_unlinked` event.
- `get_ticket_for_pane(pane_address)` — read.
- `list_pane_links_for_ticket(ticket_id)` — read with `tail_text` and `attention_state`.
- `update_pane_capture(pane_address, tail_text, attention_state)` — worker writes.

Audit invariant: every mutation emits an `activity_events` row in the same DB transaction (existing M1a/M1b rule).

## API (`serve.py`)

All under existing per-project routing:

- `POST   /{pid}/api/tickets/{tid}/pane-links` — create/replace. Body: `{pane_address, host, pane_descriptor}`.
- `DELETE /{pid}/api/pane-links/{addr}` — unlink.
- `GET    /{pid}/api/tickets/{tid}/pane-links` — list, includes `tail_text` + `attention_state`.
- `POST   /{pid}/api/pane-links/{addr}/send-keys` — body: `{text, press_enter: bool}`. Runs `tmux send-keys -t <addr> "<text>" [Enter]`. Local-only.

Send-keys is rate-limited (10/s) and validated (no NULL bytes, length cap 4KB).

## Capture worker

New background thread in `serve.py`, alongside markdown watcher / scheduler / kitchen:

- Tick every 2s.
- For each active link with `host == local_hostname`: run `tmux capture-pane -p -S -200 -t <pane_address>`.
- Strip ANSI escape sequences (regex-based; well-known safe set).
- Run **attention classifier** on the captured text (see below).
- `update_pane_capture(...)`.
- On non-zero exit / "pane not found" / tmux error: set status to `stale`, leave `tail_text` as last successful capture.
- Worker is cancellable on shutdown via the existing thread-lifecycle pattern.

## Attention classifier

Pure function `classify_attention(tail_text: str, previous_state: str) -> str`:

- **question:** last non-empty line ends with `?`, `(y/n)`, `(Y/n)`, `> `, or known prompt patterns (`Please specify`, `Which option`), AND the tail hasn't grown since the previous capture.
- **exception:** recent lines (last 30) contain `Traceback (most recent call`, `Error:`, `Exception:`, `panic:`, or `failed with status`.
- **idle:** tail unchanged for ≥30s AND last line looks like a shell prompt (`$ `, `% `, `> ` at end).
- **none:** otherwise.

Question and exception are alerts (UI flags them). Idle is informational.

False positives are inevitable; alerts are non-blocking (visual pulse only).

## GUI

In `generate.py` ticket detail overlay, new panel under existing sections:

- Heading: "Linked panes" (hidden when no links).
- Per pane:
  - Header row: `pane_descriptor`, status dot (active/stale), attention badge (?/!), unlink link.
  - Monospace tail panel, ~12 lines visible, auto-scrolls to bottom on update.
  - Send-keys input + Send button. Enter sends with newline (default); Shift+Enter sends without.
- Empty state: tip — `Run \`tt link <id>\` in a tmux pane on the server host`.

Existing dashboard 2s poll covers the live tail.

**Kanban card pulse:** cards with any link in `attention_state in (question, exception)` get a pulsing icon (?/!) in the card meta row. Reuses existing `data-attention` attribute pattern.

**Browser title:** when the focused ticket's pane has `attention_state != none`, prepend `● needs input — ` to the title.

## Testing

**TDD (no server):**

- `classify_attention` over a corpus of fixture tails (questions, tracebacks, idle, mixed).
- ANSI strip helper.
- Pane address normalization.
- `link_pane` lifecycle (link, re-link, unlink, lookup).

**Smoke (API + worker):**

- `POST /pane-links` round-trip.
- `GET /pane-links` returns expected payload (with mocked capture).
- `POST /send-keys` rate limit, validation rejects.
- Capture worker stub: monkeypatch `tmux capture-pane` subprocess, verify state transitions.

**E2E (real tmux):**

- Start a real tmux session, `tt link`, send fake output via `tmux send-keys`, verify GUI panel updates within 3s, verify `tt current` outputs ticket data, `tt unlink` clears panel.
- Question detection: send a `?`-ending line, verify card pulse appears within 3s.
- Exception detection: send a fake traceback, verify card pulse appears.

## Project mechanics

- **Branch:** `feat/pane-link`, off `main`.
- **Migration:** `19` (per critical-gotchas in CLAUDE.md).
- **Tags on tickets:** `cli-integration`, `kitchen-adjacent`.
- **Feature parity invariant:** `actions.py` + CLI + API + skill docs all updated.
- **Deployment after merge:** copy modified files to `~/.claude/ticket-takeaway/`, restart `serve.py`.
- **Granularity:** parent ticket "Pane link v1", with sub-tickets per layer (data model, CLI, API + worker, GUI + alerting, attention classifier, tests).

## Risks and mitigations

| Risk                                                                                 | Mitigation                                                                                                            |
|--------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| tmux pane ids (`%23`) reused after tmux server respawn                              | Store `pane_descriptor` (session:window.pane) as secondary; surface in GUI for human disambiguation. Mark stale on capture failure. |
| ANSI escape leakage corrupts stored `tail_text`                                     | Strip via well-tested regex. Fallback: render in `<pre>` with no special handling; broken display, never broken data. |
| `tmux capture-pane` subprocess overhead at 2s ticks scales with link count          | Bounded — TT users have a handful of active links at most. If it becomes an issue, batch via `tmux list-panes` first. |
| Send-keys could be abused if TT is exposed beyond local tailnet                     | v1 doesn't add auth, but inherits TT's existing exposure model (local + tailnet). Cross-machine send = deferred.     |
| Heuristic classifier wrong (false positive question/exception)                      | Alerts are non-blocking pulses, never modals. False positives are cheap. v2 layer adds explicit model markers.       |
| User runs `serve.py` on a different host than the pane                              | Capture worker filters on `host == local_hostname`. Other-host rows persist but are not captured (visible "remote pane" in GUI). |

## Conventions to honour (TT-specific)

- **Multi-project routing:** new endpoints match on the remainder after `/{pid}/`, not on the full path (existing gotcha).
- **API response wrapping:** list endpoints return `{"pane_links": [...]}`; JS unwraps as `data.pane_links || data || []`.
- **Origin-relative API base in page renderers:** any JS embedded in `_render_*_page()` must build API URLs as `f"/{pid}/api"` so Tailscale Serve / reverse proxies work.
- **Same-transaction event emission:** every mutation in `actions.py` emits an `activity_events` row inside the same DB transaction as the SQL write (M1a invariant).
- **Deployment:** `src/` files are canonical; deploy to `~/.claude/ticket-takeaway/` after merge and restart `serve.py`.
- **Audit drift:** run `compare_seed_to_db.py` before shipping (existing pre-ship check) — should be neutral here since no system workflows added.

## Out of scope (recorded for future)

- **Cross-machine capture** — needs a tiny daemon on each tailnet host that reports back to a central TT. Or SSH-based capture from the central host. Either way, adds auth + network considerations.
- **Surface A** (clickable feature-summary characteristics) — handled separately, partly covered by "create child ticket" flows that already exist.
- **Reabsorption** — explicitly deferred to the harness, not TT.
- **MCP server** — separate brainstorm.
- **Reactive workflows** triggered by pane content — explicitly out (user did not want this layer).
- **Pane multi-link** in one ticket — supported by table schema (no constraint on ticket_id), allowed to surface for free; no special UI consideration in v1.
