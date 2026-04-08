# Scenario Runner + Drafting Workflow — Implementation Plan

> **For agentic workers:** Follow this plan in order. Keep execution deterministic. Natural-language drafting is advisory and reviewable; it must not become the runtime test engine.

**Goal:** Build a manifest-driven scenario runner that can execute multi-actor click paths with deterministic dummy data and screenshots, then layer on a reviewed drafting flow so a user can say things like "simulate a user signing up" and get candidate scenarios to approve.

**Architecture:** Two-track rollout. Track A builds the deterministic runner, selectors, artifacts, and screenshot publishing on top of the existing `pytest` + Playwright harness. Track B adds a draft-generation workflow that produces candidate scenario manifests from natural-language prompts, but those drafts are reviewed before they are saved or run. Keep the LLM out of the execution-critical path.

**Tech Stack:** Python 3.10+, `pytest`, Playwright sync API, stdlib JSON/filesystem/process APIs, existing `serve.py` settings page, existing dashboard renderer in `src/generate.py`.

**Spec:** `docs/superpowers/specs/2026-04-06-scenario-runner-screenshot-pipeline-design.md`

**Branch:** `scenario-runner`

---

## Assumptions

These assumptions are strong enough to plan against:

1. **Pilot target is Ticket Takeaway itself.**
   The first scenarios should exercise the existing dashboard and settings surfaces in this repo. Do not block the runner on fully generic cross-app support.

2. **Natural-language prompts generate drafts, not runnable truth.**
   If the user says "simulate a user signing up", the system should propose 1-3 candidate manifests or flows to review. It should not freehand a brittle test and run it unchecked.

3. **Actor names are workflow labels for now.**
   `scheduler`, `agent`, `reviewer` map to separate browser contexts and seeded state. They are not real permissioned accounts unless a target app actually supports auth later.

4. **Signup-like flows require app-specific hooks.**
   For arbitrary apps, "sign up" is only runnable if the target app exposes deterministic selectors and test-friendly auth prereqs like disposable emails, bypassed captchas, or OTP test hooks. The drafting workflow can still propose the scenario before those hooks exist.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `.gitignore` | Modify | Ignore raw scenario artifacts only |
| `tests/conftest.py` | Modify | Shared browser/server/scenario fixtures |
| `tests/scenario_runner.py` | Create | Manifest loader, executor, capture, summary writing |
| `tests/test_scenarios.py` | Create | Generic test entrypoint for manifest-driven scenarios |
| `tests/scenarios/*.json` | Create | Checked-in scenario manifests |
| `src/generate.py` | Modify | Stable `data-testid` hooks on dashboard UI |
| `src/serve.py` | Modify | Scenario list/run/status endpoints, settings page section |
| `src/scenarios.py` | Create | Scenario discovery, schema validation, run registry, publish logic |
| `src/scenario_drafting.py` | Create | Draft generation + review-state helpers |
| `docs/scenarios/gallery/` | Create | Published screenshot assets + index manifest |

Files NOT modified in v1 unless needed by testability:

- `src/db.py`
- `src/actions.py`
- `src/tickets-cli.py`

---

## Guardrails

- [ ] The runner must execute from checked-in manifests, not from free-form prose.
- [ ] Published screenshots must only update on successful publish runs.
- [ ] Raw run artifacts must be gitignored.
- [ ] The settings page may start runs, but manifest authoring stays file-based in v1.
- [ ] LLM-generated drafts must require explicit review/approval before becoming checked-in manifests.
- [ ] Do not promise generic third-party signup automation until the target app exposes deterministic test hooks.

---

## Phase 1 — Testability Foundation

### Task 1A: Add Stable `data-testid` Hooks to the Dashboard

**Files:**
- Modify: `src/generate.py`
- Verify with: `tests/test_smoke_ui.py`

- [ ] **Step 1: Inventory the UI elements needed by the first scenario pack**

Cover at least:

- board root
- new ticket button/panel/inputs/submit
- column containers
- card root by ticket id
- detail overlay open/close
- quick-edit fields
- settings toggle

- [ ] **Step 2: Add deterministic `data-testid` attributes**

Examples:

- `data-testid="new-ticket-btn"`
- `data-testid="ticket-card-B-12"`
- `data-testid="detail-overlay"`
- `data-testid="detail-close"`

- [ ] **Step 3: Update existing smoke tests to prefer `data-testid` selectors where practical**

This is not just cleanup. It proves the hooks are sufficient before the scenario runner depends on them.

- [ ] **Step 4: Run UI smoke tests**

Run:
```bash
python3 -m pytest tests/test_smoke_ui.py -v
```

Expected: all pass.

---

### Task 1B: Create Scenario Discovery + Validation Layer

**Files:**
- Create: `src/scenarios.py`
- Create: `tests/scenarios/`

- [ ] **Step 1: Define manifest schema contract in code**

Required top-level fields:

- `id`
- `title`
- `tags`
- `actors`
- `seed`
- `steps`

- [ ] **Step 2: Implement manifest discovery**

Read all `tests/scenarios/*.json`, validate them, and return normalized scenario metadata for the runner and settings UI.

- [ ] **Step 3: Fail fast on malformed manifests**

Validation errors should name the file, field, and reason. Do not allow fuzzy fallback parsing.

- [ ] **Step 4: Add one tiny fixture manifest to prove discovery works**

Suggested file:

- `tests/scenarios/_smoke_manifest.json`

This should be a minimal single-actor scenario used only to validate loading.

---

### Task 1C: Build Core Runner

**Files:**
- Create: `tests/scenario_runner.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_scenarios.py`

- [ ] **Step 1: Add scenario fixtures**

Extend the existing Playwright/server setup with:

- scenario output directory fixture
- actor context factory
- deterministic viewport fixture

- [ ] **Step 2: Implement ordered step execution**

V1 actions:

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

- [ ] **Step 3: Enforce settled-UI capture behavior**

Before a screenshot:

- wait for target visibility if provided
- wait for loading indicators to clear on the active surface
- wait one animation frame after the last UI mutation

- [ ] **Step 4: Add generic `pytest` scenario entrypoint**

`tests/test_scenarios.py` should discover manifests and run them as parametrized tests.

- [ ] **Step 5: Add failure reporting**

On failure, write:

- failure screenshot
- step index/name
- actor name
- assertion/action error

---

## Phase 2 — Deterministic Seed Data + Pilot Scenarios

### Task 2A: Add Seed Helpers

**Files:**
- Modify: `tests/conftest.py`
- Possibly create: `tests/scenario_seed.py`

- [ ] **Step 1: Add deterministic ticket creation helpers**

Support:

- explicit ids for showcase runs
- stable titles/descriptions
- stable section/status
- stable readiness content

- [ ] **Step 2: Track all created records for cleanup**

Teardown must run even when a scenario fails midway.

- [ ] **Step 3: Keep seeding test-only**

Use Python helpers or direct DB/test API access. Do not add a public production seeding API unless needed later.

---

### Task 2B: Convert One Existing Journey to Manifest-Driven Execution

**Files:**
- Create: `tests/scenarios/quick-edit-detail-overlay.json`
- Possibly retire or slim: `tests/test_e2e_quick_edit.py`

- [ ] **Step 1: Pick the smallest valuable UI-realistic journey**

Use the detail overlay edit/persist flow first. It already maps well to deterministic data and screenshots.

- [ ] **Step 2: Express the journey as a manifest**

Must include:

- one actor
- seed ticket
- overlay open
- edit
- save/assert
- at least one capture

- [ ] **Step 3: Run the manifest through the generic runner**

Expected: green run with raw artifacts.

---

### Task 2C: Add a Multi-Actor Handoff Scenario

**Files:**
- Create: `tests/scenarios/ticket-lifecycle-ui.json`

- [ ] **Step 1: Create a scheduler-to-agent flow**

Suggested structure:

- scheduler creates or prepares a ticket
- agent reloads and picks it up
- reviewer verifies a later state

- [ ] **Step 2: Prove actor switching works**

The scenario must change actors at least once and continue against shared server state.

- [ ] **Step 3: Capture at least two publishable frames**

Examples:

- board overview
- detail overlay or ticket handoff moment

---

## Phase 3 — Artifact Pipeline

### Task 3A: Raw Run Artifacts

**Files:**
- Modify: `.gitignore`
- Modify: `tests/scenario_runner.py`

- [ ] **Step 1: Ignore raw output path**

Add:
```gitignore
.artifacts/scenarios/
```

- [ ] **Step 2: Write per-run artifact folders**

Structure:

- `.artifacts/scenarios/{run-id}/manifest.json`
- `.artifacts/scenarios/{run-id}/summary.json`
- `.artifacts/scenarios/{run-id}/screenshots/*.png`
- `.artifacts/scenarios/{run-id}/trace.zip`

- [ ] **Step 3: Include compact run summary**

Summary should contain:

- scenario id
- status
- duration
- failed step if any
- published slots if any

---

### Task 3B: Published Screenshot Gallery

**Files:**
- Create: `docs/scenarios/gallery/`
- Modify: `src/scenarios.py`

- [ ] **Step 1: Add publish-slot support**

Captures may declare `publish_slot`. Only these captures are eligible for tracked gallery output.

- [ ] **Step 2: Copy publishable screenshots on successful publish runs**

Stable destinations:

- `docs/scenarios/gallery/{publish_slot}.png`

- [ ] **Step 3: Write gallery index**

Create:

- `docs/scenarios/gallery/index.json`

Store:

- slot name
- source scenario id
- source capture name
- updated timestamp

- [ ] **Step 4: Guarantee failure isolation**

If a scenario fails, no gallery assets are overwritten.

---

## Phase 4 — Settings Page Integration

### Task 4A: Scenario Listing + Run Status Endpoints

**Files:**
- Modify: `src/serve.py`
- Reuse: `src/scenarios.py`

- [ ] **Step 1: Add scenario listing endpoint**

Endpoint:

- `GET /api/scenarios`

Return:

- id
- title
- tags
- last-run metadata if present

- [ ] **Step 2: Add scenario run endpoint**

Endpoint:

- `POST /api/scenarios/run`

Request:

- scenario id
- run mode: `test` or `publish`

- [ ] **Step 3: Add run-status endpoint**

Endpoint:

- `GET /api/scenarios/runs/{id}`

Return:

- status
- progress
- artifact paths
- summary when complete

- [ ] **Step 4: Keep runs off the request thread**

Use a background worker or subprocess model. The server must stay responsive while a scenario runs.

---

### Task 4B: Add a "Scenarios" Section to Project Settings

**Files:**
- Modify: `src/serve.py`

- [ ] **Step 1: Render scenario list in `/{project}/settings`**

Show:

- scenario title
- tags
- latest status
- run button
- run + publish button

- [ ] **Step 2: Wire polling UI**

The page should poll run status and reveal links to raw artifacts on completion.

- [ ] **Step 3: Keep authoring out of scope**

Do not add a manifest editor in the browser yet.

---

## Phase 5 — Natural-Language Drafting Workflow

This phase is where prompts like "simulate a user signing up" become useful.

### Task 5A: Add Draft Generation Model

**Files:**
- Create: `src/scenario_drafting.py`

- [ ] **Step 1: Define draft input contract**

Input should capture:

- free-form goal text
- optional actor hints
- optional target surface
- optional tags like `signup`, `checkout`, `handoff`

- [ ] **Step 2: Define draft output contract**

Output is **not** a run result. It is a reviewed proposal:

- summary of intent
- assumptions
- missing prerequisites
- 1-3 candidate scenario manifests

- [ ] **Step 3: Include prerequisite detection**

For signup-like prompts, explicitly call out blockers such as:

- captcha
- OTP/email dependency
- lack of deterministic selectors
- lack of seed/test-account support

The draft should say "proposable but not runnable yet" when needed.

---

### Task 5B: Add Draft Generation Endpoint or CLI Hook

**Files:**
- Modify: `src/serve.py`
- Reuse: `src/scenario_drafting.py`

- [ ] **Step 1: Add draft-generation entrypoint**

Either:

- `POST /api/scenarios/draft`

or a thin CLI wrapper invoked by the settings page.

- [ ] **Step 2: Feed the drafter real repo context**

At minimum:

- existing scenario manifests
- available `data-testid` coverage if known
- known routes/pages
- current project type and assumptions

- [ ] **Step 3: Keep the draft reviewable**

Return candidate manifests and rationale. Do not auto-save them into `tests/scenarios/`.

---

### Task 5C: Review + Approve Drafts

**Files:**
- Modify: `src/serve.py`

- [ ] **Step 1: Show generated candidates in settings**

For each candidate, show:

- title
- actors
- step outline
- assumptions
- prerequisite warnings

- [ ] **Step 2: Add approval action**

On approval:

- save to `tests/scenarios/{slug}.json`
- require a real filename and human-readable title

- [ ] **Step 3: Require editability before first run**

The user should be able to tweak the candidate manifest before the first execution if needed. The system should not encourage blind acceptance.

---

## Phase 6 — README/Homepage Consumption

### Task 6A: Use Published Gallery Slots

**Files:**
- Modify: `README.md` later
- Reuse: `docs/scenarios/gallery/index.json`

- [ ] **Step 1: Replace manual screenshot references with stable gallery assets**

Do this only after at least one showcase scenario is stable for multiple runs.

- [ ] **Step 2: Keep docs consumption passive**

The README reads stable files. It does not trigger scenario runs.

---

## Verification

- [ ] A manifest-driven scenario can run end-to-end via `pytest` with no bespoke Python test code for that scenario.
- [ ] At least one scenario switches actors mid-run and still passes.
- [ ] Raw run artifacts are produced for success and failure cases.
- [ ] Publish mode writes stable screenshots only on success.
- [ ] The settings page can list scenarios and launch a run without blocking the server.
- [ ] A natural-language prompt can generate candidate manifests plus prerequisite warnings.
- [ ] A draft like "simulate a user signing up" is reviewable even if the target app is not yet fully automatable.

---

## Recommended Order of Execution

1. Finish Phase 1 completely before touching drafting.
2. Finish one manifest-driven scenario in Phase 2 before adding gallery publishing.
3. Add publishing in Phase 3 only after captures are stable.
4. Add settings integration in Phase 4 once local CLI/test invocation is solid.
5. Add natural-language drafting in Phase 5 as a separate, review-first layer.

This order matters. If drafting comes first, the team will generate scenario ideas faster than the runner can execute them reliably, and confidence in the whole feature will drop.
