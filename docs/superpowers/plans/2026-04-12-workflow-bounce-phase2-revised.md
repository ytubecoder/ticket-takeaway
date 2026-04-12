# Workflow Bounce Phase 2 — Full-Page Settings + Agent/Workflow Builder UI

## Context

Phase 1 built the backend: DB tables, API endpoints, execution engine, CLI. The engine works — runs complete with conversations in DB. The frontend is incomplete: drawer-based settings HTML exists for Appearance/Feedbacks/Managed Files, but the workflow agent/workflow DOM nodes referenced by JS (workflowAgentsList, workflowsList) were never added to the HTML. The drawer JS is effectively dead code.

**This phase:** Replace the 320px drawer with a full-page settings view. Build proper agent editor + workflow step builder. Fix backend bugs.

---

## Bug Fixes (do first)

### Fix 1: prompt_modifier field name mismatch
- `src/serve.py` line 1076: reads `step.get("prompt", "")` 
- CLI writes `prompt_modifier` in `src/tickets-cli.py` line ~1395
- **Fix:** `step.get("prompt_modifier", step.get("prompt", ""))`

### Fix 2: agent_name rendering mismatch
- Backend writes `agent_name` on conversation turns, but ticket detail renderer at `src/generate.py` ~line 5505 reads `turn.agent`
- **Fix:** Change to `turn.agent_name || turn.agent || turn.agent_id || 'unknown'` in `renderConversation()`

### Fix 3: Backend normalize-then-validate for args and steps
- Agent `args`: POST and PUT both accept either a JSON array or a JSON-array string. Use a shared helper `_normalize_json_array(value, field_name)` that parses string→list if needed, checks `isinstance(list)`, then returns canonical JSON string. Return 400 with clear message if invalid. Apply in both `_create_workflow_agent` and the PUT route.
- Workflow `steps`: Same approach — accept list or JSON string, parse, validate each step has `agent_id`, reject any `_project_*` agent IDs, **also verify each agent_id exists in `workflow_agents` table** (prevents saving references to deleted/missing agents), serialize to canonical JSON string. Apply in both POST and PUT routes via shared helper.
- Keep normalization in route-level helpers, not storage functions. Storage helpers (`_create_workflow_agent`, etc.) continue to assume canonical JSON strings. HTTP 400 logic stays in routes.

### Fix 4: Project agents not executable
- `_discover_project_agents()` returns agents with `id="_project_{slug}"`
- `_run_workflow_thread()` uses `_get_workflow_agent()` which only queries `workflow_agents` table
- **Fix (frontend):** Filter project agents out of workflow step dropdowns in `src/generate.py`
- **Fix (backend):** Add validation in workflow save (POST/PUT) to reject steps containing `_project_*` agent IDs with clear error message

---

## Implementation

### Step 1: Remove old drawer, add full-page settings (`src/generate.py`)

**Approach:** Replace `#settings-drawer` (320px fixed right panel) with `#settings-page` (full-width, replaces kanban). Don't maintain both — remove the drawer markup entirely to avoid duplicate IDs.

**HTML:**
- Remove `<div id="settings-drawer" ...>` and all its contents
- Add `<div id="settings-page" class="settings-page hidden">` after the kanban board, containing:
  - Settings header with title + close button
  - Stacked sections: Appearance, Feedbacks, Managed Files (moved from drawer), Agents (new), Workflows (new)
- Add a permanent `<button id="settingsBackBtn" class="settings-back-btn">` in the filter bar (don't swap filter bar innerHTML)

**CSS:**
```css
.settings-page { display: none; padding: 24px; max-width: 900px; margin: 0 auto; }
body.settings-open .settings-page { display: block; }
body.settings-open .kanban,
body.settings-open .bottom-section { display: none; }
body.settings-open .filter-btn,
body.settings-open .filter-group,
body.settings-open .filter-divider,
body.settings-open .search-input,
body.settings-open .new-ticket-btn,
body.settings-open .settings-toggle,
body.settings-open .new-ticket-panel { display: none !important; }
.settings-back-btn { display: none; }
body.settings-open .settings-back-btn { display: inline-flex; }
```
No `.hidden` class on the back button — visibility controlled entirely by `body.settings-open` CSS. Gear icon also hidden when settings open (back button replaces it).

**JS — one centralized controller:**
```js
function openSettingsPage() {
  document.body.classList.add('settings-open');
  loadSettings(); loadFeedbacksStatus(); loadManagedFiles();
  loadAgents(); loadWorkflows();
}
function closeSettingsPage() {
  document.body.classList.remove('settings-open');
}
```
- Settings gear button → `openSettingsPage()`
- Back button → `closeSettingsPage()`
- **Preserve existing settings helpers:** Current settings IIFE (Task 10) guards on `#settings-drawer` existing (~line 4536). Replace that guard with `#settings-page`. Keep `loadSettings()`, `checkFeedbacksStatus()`, `loadManagedFiles()`, and all settings event handlers — just re-target to new DOM IDs.
- Remove old drawer open/close/outside-click logic
- Remove the dead Task 10.5 IIFE (references missing DOM nodes)

### Step 2: Agent Editor UI

**In `#settings-page`, Agents section:**

Agent list (table rows):
- Name | Command | System Prompt (truncated) | [Edit] [Delete]
- Project agents shown with "Project" badge, grayed, **not selectable in workflow steps**
- "+ Add Agent" button

Agent editor form (inline, below list):
- **Name** — text input (required)
- **ID/Slug** — auto-derived from name (lowercase, replace spaces/non-alnum with hyphens, strip leading non-alnum). Must match `^[a-z0-9][a-z0-9_-]*$`. Readonly on edit. Surface 409 duplicate errors clearly.
- **Command** — text input, default "claude"
- **CLI Args** — raw JSON array textarea, validated on save (e.g. `["--model", "opus"]`). NOT a plain text field. Validated both client-side (`JSON.parse` + `Array.isArray`) and server-side (backend returns 400).
- **System Prompt** — textarea, 6 rows, resizable
- Save / Cancel

**API fields match DB exactly:** `id`, `name`, `command`, `args` (JSON string), `system_prompt`

**Client-side validation:**
- Name required
- Args must be valid JSON array (try `JSON.parse`, check `Array.isArray`)
- Warn on delete if agent is referenced by any workflow step — scan loaded workflows, show names: "Agent is used by: Review Bounce, Architecture Pass. Delete anyway?" Require confirmation.

### Step 3: Workflow Step Builder UI

**Workflow list:**
- Name | Step count | Description (truncated) | [Edit] [Delete]
- "+ Add Workflow"

**Workflow editor (inline, replaces list when editing):**
- Name, ID (auto-derived), Description inputs at top
- **Steps builder:**

```
Step 1 (Primary)                           [↑] [↓] [×]
  Agent: [dropdown — custom agents only]
  Step instructions: [textarea, 2 rows, expands on focus]

Step 2                                     [↑] [↓] [×]
  Agent: [dropdown]
  Step instructions: [textarea]

                  [+ Add Step]

              [Save Workflow] [Cancel]
```

- Agent dropdown: populated from custom agents only (filter out `source: "project"`)
- Label "Step instructions" (not "Pre-prompt" — matches actual behavior since it's appended after context)
- ↑↓ reorder by swapping in the in-memory steps array
- × removes the step
- "+ Add Step" appends a blank step

**Save:** POST (create) or PUT (update) to `/api/workflow/workflows` with full `steps` JSON array. POST already accepts `steps`.

**Validation before save:**
- At least one step
- Every step has an agent selected
- Workflow name not empty

### Step 4: Fix ticket conversation rendering + verify integration

Fix `renderConversation()` to read `turn.agent_name || turn.agent || turn.agent_id || 'unknown'`. Then verify:
- Dropdown populates from `/api/workflow/workflows`
- Run starts and polls correctly
- Conversation renders with correct `agent_name` field

---

## Files Changed

| File | What |
|------|------|
| `src/serve.py` | Fix `prompt_modifier` bug, add `args` JSON validation on agent create/update, reject `_project_*` agent IDs in workflow saves |
| `src/generate.py` | Remove drawer, add full-page settings HTML/CSS/JS, agent editor, workflow builder, fix conversation `agent_name` rendering (~500 lines net) |

No new files. No DB changes. No new API endpoints.

---

## Parallel Execution Strategy

Two agents on separate worktrees — different files, trivial merge.

### Agent A: serve.py Backend Fixes
**File:** `src/serve.py` only

1. Fix `step.get("prompt", "")` → `step.get("prompt_modifier", step.get("prompt", ""))` at ~line 1076
2. Add `_normalize_json_array(value, field_name)` route-level helper near workflow CRUD helpers (~line 780):
   - Accept string or list, parse string via `json.loads`, verify result is list, return canonical JSON string
   - Raise ValueError with clear message on invalid input
3. Add `_normalize_workflow_steps(steps_value)` route-level helper:
   - Parse steps (string or list), validate each has `agent_id`, reject `_project_*` IDs, verify each agent exists via `_get_workflow_agent()`, return canonical JSON string
4. Apply `_normalize_json_array` on `args` in POST `/api/workflow/agents` (~line 4884) and PUT `/api/workflow/agents/{id}` (~line 4094) — return 400 on failure
5. Apply `_normalize_workflow_steps` on `steps` in POST `/api/workflow/workflows` (~line 4908) and PUT `/api/workflow/workflows/{id}` (~line 4110) — return 400 on failure

### Agent B: generate.py Frontend (sequential phases within one agent)
**File:** `src/generate.py` only

**Phase B1 — Structural HTML + CSS:**
- Remove `#settings-drawer` HTML (~lines 1862-1911) and its CSS (~lines 1534-1551)
- Add `#settings-page` full-width container after kanban with sections: Appearance, Feedbacks, Managed Files, Agents (with all `wfAgent*` form IDs), Workflows (with all `wfWorkflow*` form IDs)
- Add `.settings-back-btn` in filter bar
- Add `body.settings-open` CSS rules (hide kanban/bottom-sections/filters/gear/new-ticket-panel, show back button + settings page)

**Phase B2 — Settings JS rewrite:**
- Rewrite Task 10 IIFE: guard on `#settings-page`, centralized `openSettingsPage()`/`closeSettingsPage()`, preserve `loadSettings()`/`checkFeedbacksStatus()`/`loadManagedFiles()`
- Remove dead Task 10.5 IIFE entirely

**Phase B3 — Agent editor JS:**
- List rendering with project badges, edit/delete
- Form with name, auto-slug, command, JSON args textarea, system_prompt textarea
- Save validation (name required, args valid JSON array, handle 409)
- Delete warning listing affected workflow names

**Phase B4 — Workflow step builder JS:**
- List rendering, editor with name/id/description
- Step builder: add/remove/reorder, agent dropdown (custom only), step instructions textarea
- Save validation (name, ≥1 step, all steps have agent)

**Phase B5 — Conversation fix:**
- Fix `renderConversation()` to use `turn.agent_name || turn.agent || turn.agent_id || 'unknown'`

### Merge
Agent A and B touch different files → merge both branches with zero conflicts.

---

## Verification

1. **prompt_modifier fix:** `$CLI workflow add-step` with `--prompt-modifier "text"` → run workflow → verify modifier appears in agent prompt
2. **Settings page toggle:** Gear icon → kanban hides, settings page shows. Back → kanban returns. Filter state preserved.
3. **Agent round-trip:** Create agent with args `["--model","opus"]` and system_prompt → reload page → edit → verify values unchanged
4. **Workflow builder:** Create workflow, add 3 steps, reorder step 2 to position 1, save → verify via `$CLI workflow list` that steps order is correct
5. **Project agents excluded:** Verify discovered project agents appear in agents list with badge but are NOT in workflow step dropdowns
6. **Backend args validation:** POST/PUT agent with `args: "not json"` → 400. With `args: {"not": "array"}` → 400. With `args: ["--model","opus"]` → 200 and stored as canonical JSON string.
7. **Backend steps validation:** POST/PUT workflow with a `_project_*` agent_id in steps → 400 with clear error.
8. **Args round-trip:** PUT agent with valid args → GET → PUT again → verify canonical JSON string unchanged.
9. **End-to-end:** Open ticket → select workflow → Run → conversation appears with correct agent names
10. **Agent deletion warning:** Delete an agent used in a workflow → warning lists workflow names, requires confirmation
