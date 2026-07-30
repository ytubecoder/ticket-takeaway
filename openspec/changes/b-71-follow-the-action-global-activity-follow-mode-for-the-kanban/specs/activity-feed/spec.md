## ADDED Requirements

### Requirement: Cursor-based forward feed over cross-project activity

`actions.get_activity_feed(conn, since_id, limit, projects)` SHALL return activity
events with `id > since_id` in ascending id order, capped by `limit`. The function SHALL
be importable and testable without a running server. `GET /api/activity/feed` SHALL
expose it as a global route (no project prefix) reading the `since_id` and `limit` query
parameters, and `tickets-cli.py feed` SHALL expose it as a read-only CLI wrapper.

#### Scenario: Events are returned oldest-first after the cursor

- **GIVEN** activity events exist with ids spanning a cursor value
- **WHEN** the feed is read with that cursor as `since_id`
- **THEN** only events with a strictly greater id are returned, ascending by id

#### Scenario: Limit is bounded

- **WHEN** the feed is read with a limit above the maximum or below one
- **THEN** the limit is clamped into the supported range rather than rejected

### Requirement: Omitting the cursor yields a cursor-init response

A read with no `since_id` SHALL return the current `latest_id` and an empty `events`
list. This enforces live-only semantics: a client initialising its cursor can never
receive historical events to replay.

#### Scenario: Cursor initialisation returns no events

- **WHEN** the feed is read without `since_id`
- **THEN** `latest_id` is the current maximum event id and `events` is empty

#### Scenario: Empty event table

- **GIVEN** no activity events exist
- **WHEN** the feed is read without `since_id`
- **THEN** `latest_id` is 0 and `events` is empty

### Requirement: latest_id is the global unfiltered maximum

`latest_id` SHALL be `MAX(id)` over the whole `activity_events` table, ignoring the
watched-project and discarded-run filters. Because those filters only remove rows, a
cursor initialised at the global maximum is always a correct "now" — a filtered maximum
could sit behind a suppressed row and cause that row to replay later.

#### Scenario: latest_id ignores filtering

- **GIVEN** the newest event belongs to an unwatched project or a discarded run
- **WHEN** the feed is read
- **THEN** `latest_id` still reflects that newest event's id
- **AND** the event itself is absent from `events`

### Requirement: Feed excludes unwatched projects and reverted history

The feed SHALL return events only from projects whose registry entry is watched, plus
the `_kitchen` sentinel project, which always passes so pause and resume lifecycle
banners are never suppressed. Events belonging to a discarded run
(`discarded_run_id IS NOT NULL`) SHALL be excluded so reverted history is never replayed.

#### Scenario: Unwatched project events are filtered out

- **GIVEN** a project marked unwatched in the registry
- **WHEN** the feed is read
- **THEN** no events from that project appear

#### Scenario: Kitchen sentinel always passes

- **GIVEN** an event on the `_kitchen` sentinel project
- **WHEN** the feed is read
- **THEN** that event appears with display name "Kitchen"

#### Scenario: Discarded run events are excluded

- **GIVEN** an event whose `discarded_run_id` is set
- **WHEN** the feed is read
- **THEN** that event does not appear

### Requirement: Feed events are enriched for display

Each returned event SHALL carry `project_name` resolved from the caller-supplied project
registry dicts, `ticket_title` and current `section` joined from `tickets`, and
`actor_name` resolved through the existing run-to-workflow-name resolution. Ticket
enrichment fields SHALL be null rather than raising when the referenced ticket no longer
exists.

#### Scenario: Ticket subject is enriched

- **GIVEN** an event whose subject is an existing ticket
- **WHEN** the feed is read
- **THEN** the event carries that ticket's title and current section

#### Scenario: Deleted ticket does not break the feed

- **GIVEN** an event whose subject ticket has since been deleted
- **WHEN** the feed is read
- **THEN** the event is returned with null title and section rather than an error
