# Scenario Runner + Screenshot Pipeline - Design Spec

**Date:** 2026-04-06
**Status:** Draft
**Scope:** Declarative multi-actor click-path scenarios, deterministic dummy data, Playwright execution, step screenshots, and optional README/homepage asset publishing. This extends the existing `pytest` + Playwright harness; it does not replace TDD tests or introduce a separate browser automation stack.

## Problem

Ticket Takeaway already has the right broad test buckets:

- `tests/test_tdd_*.py` for logic
- `tests/test_smoke_*.py` for broad surface coverage
- `tests/test_e2e_*.py` for journeys

But the current E2E layer is mostly API-driven and the UI smoke layer asserts "clicks do not explode" rather than "this exact user journey happened in this exact order." There is also no repeatable screenshot pipeline. The README still relies on a manually captured dashboard image.

We need a way to describe:

- a named use case or click path
- the actors involved and their order
- the dummy data required for the path
- the exact clicks and assertions
- the screenshots to capture at each meaningful step

The same source of truth should serve two purposes:

1. regression coverage for real workflows
2. stable screenshot generation for README/homepage eye candy

## Proposal

Add a **Scenario Runner** on top of the existing Playwright test harness.

A scenario is a checked-in manifest that declares:

- metadata: id, title, tags
- actors: `scheduler`, `agent`, `reviewer`, etc.
- deterministic seed data
- ordered UI steps
- assertions
- capture points
- publish targets for selected screenshots

The runner executes the scenario against the live local server, produces raw artifacts for debugging, and optionally publishes curated screenshots to stable asset paths for docs/marketing use.

## Placement Recommendation

Do **not** start with a new top-level screen.

V1 should live in two places:

1. **Source of truth:** checked-in scenario manifests under `tests/scenarios/`
2. **Operational UI:** a "Scenarios" section on the existing project settings page at `/{project}/settings`

Why:

- this is test infrastructure, not daily board interaction
- authoring belongs in version-controlled files, not an in-browser editor
- the settings page already exists and is the right place for run buttons, status, and artifact links

If scenario usage grows beyond "engineers maintaining a catalog," then a dedicated `/scenarios` page can be added later. That should be a v2 decision, not a v1 prerequisite.

## Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Authoring format | JSON manifests in `tests/scenarios/*.json` | No new dependency like YAML; easy to diff, load, validate, and generate from tools later |
| Execution engine | Generic `pytest` + Playwright runner | Reuses `tests/conftest.py`, existing browser/server fixtures, and current team workflow |
| Actor model | Named Playwright browser contexts | Gives clean role separation now, and a future path to real auth later |
| Role semantics | Actor labels first, credentials later | The app has no auth system today, so "scheduler" and "agent" are workflow roles, not permissioned identities |
| Data setup | Deterministic seed helpers | Stable screenshots and reliable assertions require known ticket titles, statuses, and content |
| Selector strategy | Add `data-testid` coverage to dashboard UI | Existing main-board tests still rely heavily on CSS selectors and DOM structure |
| Screenshot strategy | Explicit capture points, not every click | Keeps artifacts useful and stable instead of noisy |
| Artifact split | Raw runs in gitignored `.artifacts/scenarios/`; published images in `docs/scenarios/gallery/` | Separates test debugging output from committed eye-candy assets |
| Publish behavior | Explicit run mode or per-capture flag; never publish on failed runs | README/homepage assets must not flap because a regression run half-failed |

## Scenario Model

Each scenario manifest should contain five sections:

1. **Metadata**
2. **Actors**
3. **Seed data**
4. **Ordered steps**
5. **Publish hints**

### Example Manifest

```json
{
  "id": "scheduler-agent-handoff",
  "title": "Scheduler creates a ticket and agent picks it up",
  "tags": ["e2e", "regression", "showcase"],
  "viewport": { "width": 1440, "height": 1024 },
  "actors": {
    "scheduler": { "label": "Scheduler" },
    "agent": { "label": "Agent" }
  },
  "seed": {
    "tickets": [
      {
        "id": "B-201",
        "title": "Inbox cleanup pass",
        "section": "Backlog",
        "status": "ready",
        "description": "Tighten the board before the weekly review.",
        "criteria": [
          "Filters still work after edits",
          "Detail overlay saves description changes"
        ]
      }
    ]
  },
  "steps": [
    {
      "actor": "scheduler",
      "action": "open",
      "path": "/ticket-takeaway",
      "capture": { "name": "board-home", "publish_slot": "gallery-home" }
    },
    {
      "actor": "scheduler",
      "action": "click",
      "target": { "testid": "new-ticket-btn" }
    },
    {
      "actor": "scheduler",
      "action": "fill",
      "target": { "testid": "new-ticket-title" },
      "value": "Capture-ready handoff ticket"
    },
    {
      "actor": "scheduler",
      "action": "click",
      "target": { "testid": "new-ticket-submit" },
      "assert": { "text_visible": "Capture-ready handoff ticket" },
      "capture": { "name": "ticket-created", "publish_slot": "readme-workflow-1" }
    },
    {
      "actor": "agent",
      "action": "reload"
    },
    {
      "actor": "agent",
      "action": "click",
      "target": { "testid": "ticket-card-B-201" }
    },
    {
      "actor": "agent",
      "action": "double_click",
      "target": { "testid": "ticket-card-B-201" },
      "capture": { "name": "agent-picks-up-ticket", "publish_slot": "readme-workflow-2" }
    }
  ]
}
```

## Step Semantics

V1 should support a small, disciplined action set:

- `open`
- `reload`
- `click`
- `double_click`
- `fill`
- `select`
- `press`
- `wait_for`
- `assert_visible`
- `assert_text`
- `capture`

Principles:

- default to **real UI interactions**
- use helper hooks only for data setup or temporary gaps in UI testability
- keep steps serial and human-readable
- every capture step should wait for a settled UI before taking the image

Settled UI means:

- target element visible
- no active loading spinners for the current surface
- one animation frame of stability after the final DOM mutation

## Actor Model

Each actor gets its own Playwright `BrowserContext` and `Page`.

That gives us:

- isolated local storage
- isolated clipboard/session state
- realistic handoff modeling
- room for real login flows later

Important limitation: **actors are not permissioned app roles yet.** Ticket Takeaway currently has no auth system. In v1, actor names are workflow labels and separate browser contexts only. If authentication lands later, an actor block can grow optional credentials:

```json
{
  "agent": {
    "label": "Agent",
    "login": {
      "username": "agent-demo",
      "password_env": "TT_SCENARIO_AGENT_PASSWORD"
    }
  }
}
```

## Dummy Data and Seeding

Stable screenshot output requires stable data. The current E2E tests use timestamped titles, which is correct for isolation but bad for repeatable marketing images.

V1 should add deterministic seed helpers that can create:

- explicit ticket ids for showcase scenarios
- fixed titles and descriptions
- fixed readiness content
- stable section/status placement

Recommendation:

- keep this in Python test helpers, not a public production API
- allow direct DB seeding or a test-only helper layer behind `pytest`
- guarantee cleanup after each scenario run

The runner should record every created ticket id and remove it during teardown even on failure.

## Screenshot Model

A scenario does **not** capture every single click. It captures meaningful states.

Each capture record should support:

- `name`: stable filename slug
- `publish_slot`: optional stable destination alias
- `full_page`: optional, default false
- `mask`: optional selectors to hide volatile content

Example:

```json
{
  "capture": {
    "name": "detail-overlay-open",
    "publish_slot": "homepage-detail",
    "full_page": false,
    "mask": ["[data-testid='clock']", "[data-testid='session-id']"]
  }
}
```

For README/homepage use, captures should be:

- deterministic
- cleanly framed
- free of timestamps and transient IDs when possible
- taken at a stable viewport
- based on showcase-specific seed data, not whatever happened to be in the DB

## Artifact Layout

### Raw Run Artifacts (gitignored)

`/.artifacts/scenarios/{run-id}/`

- `manifest.json`
- `summary.json`
- `trace.zip`
- `console.log`
- `screenshots/01-board-home.png`
- `screenshots/02-ticket-created.png`
- `failure.png` if the run aborts

### Published Assets (tracked)

`/docs/scenarios/gallery/`

- `gallery-home.png`
- `readme-workflow-1.png`
- `readme-workflow-2.png`
- `index.json`

`index.json` is a tiny manifest for downstream consumers like the README updater or a future homepage/gallery page.

`.gitignore` should be updated to ignore only the raw artifact directory, not the published gallery.

## Runner Flow

```text
Load scenario manifest
  -> start dashboard server fixture
  -> seed deterministic dummy data
  -> create browser context per actor
  -> execute ordered steps
  -> assert expected UI state at each checkpoint
  -> capture screenshots for named steps
  -> write raw artifacts
  -> if run mode includes publish and scenario passed:
       copy publishable captures to docs/scenarios/gallery/
       update docs/scenarios/gallery/index.json
  -> cleanup seeded data
```

## Settings Page UX

Add a "Scenarios" section to `/{project}/settings` with:

- a list of discovered scenarios
- tags shown as small badges: `smoke`, `e2e`, `regression`, `showcase`
- last run status
- `Run` button
- `Run + Publish Screenshots` button
- link to latest artifact folder

Important scope boundary:

- the settings page **runs** scenarios
- the settings page does **not author** scenarios in v1
- editing manifests remains file-based

## Minimal API Surface

If the settings page needs to run scenarios from the browser, `serve.py` should expose:

- `GET /api/scenarios` -> list manifests and recent status
- `POST /api/scenarios/run` -> start background run for one scenario or a tagged pack
- `GET /api/scenarios/runs/{id}` -> poll status and artifact paths

This should be a thin orchestration layer around the existing Python test runner, not a second execution engine.

## Initial Scenario Catalog

The first pass should convert existing value, not invent entirely new territory.

Recommended seed scenarios:

| Scenario ID | Purpose | Actors | Tags |
|-------------|---------|--------|------|
| `ticket-lifecycle-ui` | Real board journey from create to done | `scheduler`, `agent`, `reviewer` | `e2e`, `regression` |
| `quick-edit-detail-overlay` | Edit fields in overlay and confirm persistence | `agent` | `e2e`, `regression`, `showcase` |
| `bug-fix-handoff` | Parent + bug child flow with handoff | `agent`, `reviewer` | `e2e`, `regression` |
| `homepage-gallery-pack` | Curated board/detail shots only | `scheduler`, `agent` | `showcase` |

This keeps continuity with the current `test_e2e_*` files while finally giving the team a screenshot-capable UI journey layer.

## Selector Work Required

This feature will be brittle unless the dashboard gets stable test selectors.

V1 should add `data-testid` coverage for at least:

- new ticket controls
- ticket card root by id
- major column containers
- filter controls
- detail overlay open/close affordances
- quick-edit inputs
- settings drawer toggle
- any action used by the seed scenarios

The main board currently exposes very few `data-testid` hooks compared with the settings page. That needs to change before the scenario runner is reliable.

## Implementation Slices

### Slice 1: Harness

- add manifest loader and validator
- add actor/context abstraction
- add deterministic seeding helpers
- add raw artifact writer

### Slice 2: UI-Realistic Journeys

- add `data-testid` hooks in generated dashboard HTML
- convert one existing E2E journey into manifest-driven execution
- prove capture and cleanup work end-to-end

### Slice 3: Publishing

- add publish slots and gallery output
- add `docs/scenarios/gallery/index.json`
- wire README/homepage assets to stable gallery paths

### Slice 4: Settings Integration

- add scenario listing/run endpoints in `serve.py`
- add settings page scenario section
- poll background run status from the browser

## Files Likely Added or Changed

- `.gitignore`
- `tests/conftest.py`
- `tests/scenario_runner.py`
- `tests/test_scenarios.py`
- `tests/scenarios/*.json`
- `src/generate.py`
- `src/serve.py`
- `docs/scenarios/gallery/`
- `README.md` later, once stable publish slots exist

## Non-Goals

V1 should explicitly avoid:

- free-form natural language scenario execution at runtime
- a browser-based scenario editor
- visual diff baselines or pixel-perfect approval tests
- cloud browser grids
- automatic README rewriting on every successful test run
- pretending actor labels are real auth roles before the app supports auth

## Verification

1. A checked-in manifest can drive a full multi-step UI journey without bespoke test code for that scenario.
2. A scenario can switch actors mid-run and continue against shared server state.
3. The runner emits raw artifacts for every run and failure artifacts on abort.
4. A passing showcase run can publish selected screenshots to stable paths under `docs/scenarios/gallery/`.
5. A failed run never updates published gallery assets.
6. At least one existing `test_e2e_*` journey is migrated to the new runner successfully.
7. The settings page can launch a run and show status without blocking the server thread.

## Recommendation

Treat this as **test infrastructure first, gallery tooling second**.

If the team builds it that way, the README/homepage screenshots become a cheap byproduct of a real regression harness. If the team starts from "marketing screenshots" and bolts tests on afterward, it will drift into a separate, fragile automation path almost immediately.
