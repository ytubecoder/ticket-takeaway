## ADDED Requirements

### Requirement: Follow mode is opt-in and starts from now

Board pages SHALL render a "Follow" chip in the sticky filter bar, defaulting to off and
persisting its state in `localStorage` under `tt-follow-enabled`. Enabling the mode SHALL
always re-initialise the cursor to the feed's `latest_id`, so re-enabling after any off
period never replays history. With the mode off, the board SHALL behave exactly as it
does without the feature.

#### Scenario: Default state is off

- **WHEN** a board page loads with no stored follow state
- **THEN** the Follow chip is inactive, the ticker is hidden, and no feed polling occurs

#### Scenario: Enabling starts from the present

- **GIVEN** activity occurred while the mode was off
- **WHEN** the user enables Follow
- **THEN** the cursor is set to the current `latest_id`
- **AND** none of that earlier activity is played

#### Scenario: State persists across boards

- **GIVEN** Follow is enabled on one project's board
- **WHEN** the user opens another project's board
- **THEN** Follow is still enabled

### Requirement: The cursor advances only after a step finishes playing

The stored cursor SHALL advance to the maximum event id of a coalesced group only once
that group's playback completes, or when the overflow guard skips it. Before a
cross-project navigation the cursor SHALL be persisted at its pre-step value, with the
pending step handed off in `sessionStorage`. A lost or stale handoff SHALL therefore
cause at most one duplicate replay, never a lost event.

#### Scenario: Cursor holds until playback completes

- **WHEN** a step begins playing
- **THEN** the stored cursor still reflects the previous step until playback finishes

#### Scenario: Lost handoff replays rather than loses

- **GIVEN** a cross-project navigation whose arrival payload is missing or older than
  the staleness window
- **WHEN** the destination board initialises
- **THEN** the engine refetches from the stored cursor and replays that one step

### Requirement: Bursts coalesce into a single captioned step

Contiguous events sharing project, subject type, and subject id SHALL collapse into one
step. The headline SHALL be the highest-precedence kind, ordered `section_change`,
`ticket_created`, `status_change`, run lifecycle, then field and criteria changes. The
remaining events in the group SHALL be summarised as "+N more". Steps SHALL play at a
fixed interval.

#### Scenario: Same-ticket burst plays once

- **GIVEN** several consecutive events on one ticket
- **WHEN** the queue is played
- **THEN** one step plays, captioned with the highest-precedence kind and "+N more"

#### Scenario: Precedence picks the headline

- **GIVEN** a coalesced group containing both a status change and a section change
- **WHEN** the step plays
- **THEN** the section change is the headline

### Requirement: Overflow is skipped rather than queued

When the unplayed backlog exceeds the overflow limit, the engine SHALL drop the queue,
jump the cursor to `latest_id`, and report the number of skipped actions in the ticker,
rather than playing a long stale backlog.

#### Scenario: Large backlog is skipped

- **GIVEN** an unplayed backlog beyond the overflow limit
- **WHEN** the engine next plays
- **THEN** the queue is dropped, the cursor jumps to `latest_id`, and the ticker reports
  the skipped count

### Requirement: Same-project steps spotlight the card

For an event on the current board whose subject is a ticket, the engine SHALL scroll the
card into view, apply a spotlight ring, and show the caption in the ticker coloured by
its event group. While Follow is enabled, the board's own diff-poll SHALL suppress its
`scrollIntoView` so exactly one subsystem drives the viewport.

#### Scenario: Card is spotlighted and captioned

- **GIVEN** Follow is enabled and an event occurs on a visible card of the current board
- **WHEN** the step plays
- **THEN** the card is scrolled into view and ringed, and the ticker shows the caption

#### Scenario: Diff-poll does not fight the spotlight

- **GIVEN** Follow is enabled
- **WHEN** the diff-poll applies a card change
- **THEN** the diff-poll does not scroll the viewport itself

### Requirement: Follow mode never alters the user's view state

When the target card is absent, filtered out, or inside a collapsed section, the step
SHALL be caption-only, pulsing the section header where one exists. The engine SHALL
never clear filters or search text, never expand collapsed sections, and never toggle
drafts in order to reveal a card. Non-ticket subjects SHALL likewise play caption-only.

#### Scenario: Hidden card degrades to caption-only

- **GIVEN** the affected card is filtered out or in a collapsed section
- **WHEN** the step plays
- **THEN** the caption shows and the section header pulses, and no filter, search, or
  section state changes

#### Scenario: Non-ticket subject plays caption-only

- **GIVEN** an event whose subject is a journey or investigation
- **WHEN** the step plays
- **THEN** the ticker captions it and no card is spotlighted

### Requirement: Cross-project steps navigate through a departure overlay

For an event in another watched project, the engine SHALL validate the target project id
against the page's projects-list metadata, show a departure overlay naming the target,
hand off the pending step, and navigate to that project's board, where the arrival step
plays before the loop resumes. An unknown target SHALL degrade to a caption-only step
with no navigation. `_kitchen` sentinel events SHALL never navigate.

#### Scenario: Action elsewhere brings the board to it

- **GIVEN** Follow is enabled and an event occurs in another watched project
- **WHEN** the step plays
- **THEN** a departure overlay names the target project and the browser navigates to that
  board, where the step is spotlighted on arrival

#### Scenario: Unknown project does not navigate

- **GIVEN** an event whose project is absent from the page's projects list
- **WHEN** the step plays
- **THEN** the caption shows and no navigation occurs

#### Scenario: Kitchen events never navigate

- **GIVEN** a `_kitchen` pause or resume event
- **WHEN** the step plays
- **THEN** the ticker shows a banner and the board does not navigate

### Requirement: Playback yields to human interaction

Playback and navigation SHALL suspend while a ticket overlay is open, a drag is in
progress, a text input is focused, or the bounce overlay is open. Events SHALL keep
queueing during the suspension, bounded by coalescing and the overflow guard, and
playback SHALL resume when the interaction ends.

#### Scenario: Open overlay defers playback

- **GIVEN** the user has a ticket overlay open and Follow is enabled
- **WHEN** new events arrive
- **THEN** no spotlight or navigation occurs until the overlay is closed

#### Scenario: Typing is not interrupted

- **GIVEN** focus is in the search input
- **WHEN** a cross-project step becomes ready
- **THEN** navigation is deferred until focus leaves the input

### Requirement: Feed failures degrade quietly and never self-disable

A failed feed fetch SHALL retry on the next tick. After a run of consecutive failures the
ticker SHALL show an offline state. The mode SHALL never turn itself off. Polling SHALL
pause while the document is hidden, and on regaining visibility the cursor SHALL be
re-initialised to `latest_id` to preserve live-only semantics.

#### Scenario: Transient failure recovers silently

- **GIVEN** one feed fetch fails
- **WHEN** the next poll succeeds
- **THEN** playback continues and no error surfaces to the user

#### Scenario: Sustained failure surfaces an offline state

- **GIVEN** consecutive feed fetches fail beyond the failure threshold
- **THEN** the ticker shows an offline state and the mode remains enabled

#### Scenario: Hidden tab does not accumulate a backlog

- **GIVEN** the board tab is hidden while activity occurs
- **WHEN** the tab becomes visible again
- **THEN** the cursor is re-initialised to `latest_id` rather than replaying the gap

### Requirement: Reduced motion is honoured

Under `prefers-reduced-motion: reduce` the engine SHALL disable smooth scrolling and the
spotlight, chip, and pulse keyframes, and SHALL make the departure transition instant.
The ticker SHALL still update and cross-project navigation SHALL still occur.

#### Scenario: Motion is suppressed but function is retained

- **GIVEN** the viewer prefers reduced motion
- **WHEN** steps play
- **THEN** no keyframe animation or smooth scrolling runs, while captions still update
  and navigation still happens
