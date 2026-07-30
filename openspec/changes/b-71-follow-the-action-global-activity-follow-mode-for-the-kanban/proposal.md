## Why

Agents mutate tickets across many projects continuously, but a board on screen shows
exactly one project. The monitor kept on a Ticket Takeaway board therefore rarely
corresponds to where the action is, so the visualisation benefit is lost — the user has
to guess which board to open, and by the time they switch the action has moved on.

`activity_events` already records every mutation across every project in one table with
a monotonic id. Nothing consumed it as a forward feed, and no board surface directed
attention at what it recorded.

## What Changes

- New cross-project forward feed over `activity_events` — `actions.get_activity_feed()`,
  the global `GET /api/activity/feed` route, and a `tickets-cli.py feed` wrapper
  (actions + API + CLI parity).
- New opt-in **Follow mode** on board pages: a "Follow" filter chip, a bottom ticker
  bar, a card spotlight ring, and a departure/arrival overlay that navigates the browser
  to another project's board when the action is there.
- `EVENT_KIND_GROUPS` extended to the full documented event vocabulary, so the generic
  caption fallback is the exception rather than the common case, plus a precedence order
  used to pick the headline event when a burst is coalesced.
- The existing 2s HTML-diff poll suppresses its own `scrollIntoView` while Follow is
  enabled, so exactly one subsystem drives the viewport at a time.

## Capabilities

### New Capabilities

- `activity-feed`: cursor-based forward read over `activity_events` spanning all watched
  projects, enriched with project name, ticket title, current section, and resolved
  actor name.
- `follow-mode`: opt-in board behaviour that plays the feed as attention — spotlighting
  cards, captioning actions in a ticker, and navigating across project boards.

### Modified Capabilities

### Impact

- `src/actions.py`, `src/constants.py`, `src/serve.py`, `src/tickets-cli.py`,
  `src/generate.py` (+458 lines: follow CSS, follow engine JS, chip and ticker markup).
- No schema migration — the feed is a read over the existing `activity_events` table
  using the `id` rowid B-tree; no new index.
- Follow mode never mutates the board, never clears the user's filters or search, and
  never expands collapsed sections; it only directs attention.
- Default off. With the mode off, board behaviour is byte-for-byte unchanged.
