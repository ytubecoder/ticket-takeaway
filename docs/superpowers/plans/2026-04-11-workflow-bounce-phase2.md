# Workflow Bounce Phase 2 — Full-Page Settings + Agent/Workflow Builder UI

## Context

Phase 1 built the backend: DB tables (workflow_agents, workflows, workflow_runs), API endpoints (full CRUD + execution), CLI commands, and a basic settings drawer UI. The execution engine works — runs complete with agent conversations stored in DB.

**What's needed now:**
1. Settings drawer is too cramped (320px sidebar) — convert to a **full-page settings view** that replaces the kanban board
2. Agent management needs a proper editor (name, model/command, system prompt)
3. Workflow builder needs a **step-by-step line editor** — add/remove/reorder steps, each with agent selection + optional pre-prompt
4. Fix critical bug: execution engine ignores `prompt_modifier` (reads wrong field name)
5. Everything already stored in DB (migration 4) — just needs proper UI

## What already exists (reuse, don't rebuild)

### Backend (all working)
- **DB tables:** `workflow_agents`, `workflows`, `workflow_runs` (db.py migration 4)
- **API endpoints** (serve.py):
  - Agents: GET/POST/PUT/DELETE `/api/workflow/agents`
  - Workflows: GET/POST/PUT/DELETE `/api/workflow/workflows`
  - Runs: POST `/api/tickets/{id}/workflow/run`, GET `../workflow/runs`, GET `/api/workflow/runs/{id}`, POST `../cancel`, POST `../resume`
- **Execution engine:** `_run_workflow_thread()` in serve.py — sequential steps, subprocess CLI calls, disagreement detection, attachment creation
- **CLI:** `tickets-cli.py agent list/add/update/remove` + `workflow list/add/add-step/remove-step/remove`

### Frontend (partially working)
- **Settings drawer HTML:** `#settings-drawer`, 320px fixed right panel with Appearance, Feedbacks, Managed Files sections + Workflow Agents + Workflows sections (line ~1862)
- **Settings JS (Task 10.5):** `loadAgents()`, `loadWorkflows()`, agent/workflow CRUD — functional but cramped in drawer
- **Ticket detail workflow section:** `#section-workflow` with select dropdown + Run button + runs list — functional
- **Ticket detail JS (Task 11.5):** `loadWorkflowOptions()`, `loadWorkflowRuns()`, `renderRunBlock()`, polling — functional

---

## Bug Fixes (do first)

### Fix 1: prompt_modifier field name mismatch
- **File:** `src/serve.py` ~line 1070
- **Bug:** Engine reads `step.get("prompt", "")` but steps are created with key `"prompt_modifier"`
- **Fix:** Change to `step.get("prompt_modifier", step.get("prompt", ""))`

---

## Implementation Plan

### Step 1: Full-Page Settings View (`src/generate.py`)

**Approach:** When user clicks settings gear, hide the kanban board and bottom sections, show a full-page settings container in their place. The filter bar stays visible with a "Back to Board" button replacing the filter buttons.

**HTML changes:**
- Add a new `<div id="settings-page" class="settings-page hidden">` after the kanban board
- Move existing settings drawer content (Appearance, Feedbacks, Managed Files) into this page
- Add new Agents and Workflows sections (larger, with proper editors)
- Structure: tabs or stacked sections, full width

**CSS:**
```
.settings-page { display: none; padding: 20px; max-width: 900px; margin: 0 auto; }
.settings-page.visible { display: block; }
.kanban.settings-open, .bottom-section.settings-open { display: none; }
```

**JS changes:**
- Settings toggle button: instead of opening drawer, toggle `.settings-open` on kanban/bottom-sections and `.visible` on `#settings-page`
- Keep the old drawer code but redirect to new page view
- Filter bar: when settings open, hide filter buttons and show "← Back to Board" link

**Key element IDs:**
- `#settings-page` — full-page container
- `#kanban` — hide when settings open (line 1915)
- `.bottom-section` elements — hide when settings open
- `#filterBar` — modify content when settings open

### Step 2: Agent Editor UI (`src/generate.py`)

**In the full-page settings, Agents section:**

Layout: List of agents on the left, editor panel on the right (or stacked on narrow screens).

**Agent list:**
- Each row: Name | Command | [Edit] [Delete]
- Project agents (discovered) shown with gray badge, not editable
- "+ Add Agent" button at bottom

**Agent editor form (shown when Add/Edit clicked):**
- **Name** — text input (required)
- **ID/Slug** — auto-generated from name on create, shown but not editable on edit
- **Model/Command** — dropdown or text input. Options: `claude` (default), or custom CLI command
- **CLI Args** — text input for extra args (e.g., `--model opus`)
- **System Prompt** — large textarea (resizable, ~6 rows)
- **Save / Cancel** buttons

**API calls:** Same as now — POST `/api/workflow/agents` to create, PUT `.../{id}` to update, DELETE to remove.

### Step 3: Workflow Step Builder UI (`src/generate.py`)

**In the full-page settings, Workflows section:**

**Workflow list:**
- Each row: Name | Step count | [Edit] [Delete]
- "+ Add Workflow" button

**Workflow editor (shown when Add/Edit clicked):**
- **Name** — text input
- **ID/Slug** — auto-generated from name on create
- **Description** — text input (one line)
- **Steps list** — the main builder:

```
┌─────────────────────────────────────────────────────┐
│ Step 1 (Primary)                          [↑] [↓] [×] │
│ Agent: [dropdown: Architect ▾]                        │
│ Pre-prompt: [textarea: "Focus on scalability..."]     │
├─────────────────────────────────────────────────────┤
│ Step 2                                    [↑] [↓] [×] │
│ Agent: [dropdown: Code Reviewer ▾]                    │
│ Pre-prompt: [textarea: "Review for issues..."]        │
├─────────────────────────────────────────────────────┤
│                    [+ Add Step]                        │
└─────────────────────────────────────────────────────┘
```

Each step row:
- Step number badge (step 1 gets "Primary" label)
- Agent dropdown (populated from agents list via API)
- Pre-prompt textarea (optional, collapsible or small by default, expands on focus)
- Reorder buttons (↑ ↓) to move step up/down
- Delete button (×) to remove step

**Save behavior:**
- On save: PUT `/api/workflow/workflows/{id}` with `steps` as JSON array of `[{agent_id, prompt_modifier, label}]`
- The `label` can default to the agent name
- Steps are ordered by their position in the list

### Step 4: Verify Ticket-Level Integration

The existing ticket detail workflow section should work as-is:
- Select dropdown populated from `/api/workflow/workflows`
- Run button starts execution
- Runs list shows progress with polling
- Conversation displayed in collapsible blocks

No changes needed here — just verify it works after the bug fix.

---

## File Changes

| File | What | Est. Lines |
|------|------|-----------|
| `src/serve.py` | Fix prompt_modifier bug (~1 line) | ~1 |
| `src/generate.py` | Full-page settings HTML + CSS + JS, agent editor, workflow step builder | ~400 |

**No new files. No DB changes. No new API endpoints.**

---

## Verification

1. **Bug fix:** Create a workflow with prompt_modifier via CLI, run it, verify the modifier text appears in the agent's prompt
2. **Settings page:** Click gear → kanban hides, full settings page shows. Click back → kanban returns.
3. **Agent editor:** Add agent with name/command/prompt → appears in list. Edit → changes persist. Delete → removed.
4. **Workflow builder:** Create workflow → add 3 steps with agents + pre-prompts → reorder steps → save. Verify via API that steps JSON is correct.
5. **End-to-end:** Open a ticket → select the workflow → Run → see progress → conversation appears.
6. **Settings persistence:** Reload page → settings still show correct agents/workflows.
