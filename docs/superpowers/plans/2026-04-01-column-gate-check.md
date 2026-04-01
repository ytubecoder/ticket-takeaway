# Column Move Gate Check — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate ticket column moves with an AI-powered readiness check that presents structured per-category (D,C,T,R,S) analysis, letting users edit/save suggestions per-section before confirming or canceling the move.

**Architecture:** When a move targets a top kanban column, JS intercepts and calls a new `/gate-check` endpoint. That endpoint spawns `claude -p` with the ticket's full context, parses the structured JSON response, and returns it. The frontend renders an expandable gate panel on the card with per-section editable fields and independent Save buttons. Existing `PUT` and `move` endpoints are reused for saves and the final move.

**Tech Stack:** Python 3.10+ (serve.py backend), vanilla JS/CSS (generate.py inline), Claude Code CLI (`claude -p`)

---

## JSON Contract (shared between all tasks)

**Request:** `POST /api/tickets/{id}/gate-check`
```json
{ "section": "For Review" }
```

**Response:**
```json
{
  "ticket_id": "B-05",
  "target_section": "For Review",
  "verdict": "needs-work",
  "summary": "Description and criteria look good, but tests and review flags are not set.",
  "categories": {
    "D": { "status": "ok", "current_summary": "Description is present and detailed.", "suggestion": null },
    "C": { "status": "ok", "current_summary": "3/4 criteria checked.", "suggestion": "Consider adding criterion for error handling." , "add_criteria": ["Handles network timeout gracefully"] },
    "T": { "status": "needs-work", "current_summary": "Tests flag not set.", "suggestion": "Run test suite and set T flag when passing." },
    "R": { "status": "needs-work", "current_summary": "Review flag not set.", "suggestion": "Run /sync to capture session decisions before marking reviewed." },
    "S": { "status": "ok", "current_summary": "Smoke tested flag is set.", "suggestion": null }
  }
}
```

**Verdict values:** `"ready"`, `"needs-work"`, `"blocked"`
**Category status values:** `"ok"`, `"needs-work"`

---

## Parallel Execution Graph

```
     Task 1: Backend        Task 2: CSS           Task 3: JS Logic
     serve.py (~60 lines)   generate.py (~50 lines) generate.py (~140 lines)
     ────────────────────   ─────────────────────  ──────────────────────────
     Independent             Independent            Independent (mock response)
            │                       │                       │
            └───────────────────────┴───────────────────────┘
                                    │
                            Task 4: Integration
                            Wire up + verify (~10 lines)
```

Tasks 1, 2, 3 run in parallel. Task 2 and 3 touch non-overlapping regions of generate.py (CSS ~line 933 vs JS ~line 1608+). Task 4 is a short integration step.

---

## Task 1: Backend Gate-Check Endpoint (`src/serve.py`)

**Files:**
- Modify: `src/serve.py:305` (add constant), `:345` (add helper), `:564` (add route)

- [ ] **Step 1: Add gated-sections constant and prompt builder**

Add after the `VALID_READINESS_FLAGS` constant at line 305:

```python
# Sections that require a gate check before entry
GATED_SECTIONS = {"Ideas", "Backlog", "WIP", "For Review", "Done"}


def _build_gate_prompt(ticket: dict, target_section: str) -> str:
    """Build the analysis prompt for the gate-check agent."""
    criteria_lines = []
    for c in ticket.get("acceptance_criteria", []):
        mark = "[x]" if c["checked"] else "[ ]"
        criteria_lines.append(f"- {mark} {c['text']}")
    criteria_text = "\n".join(criteria_lines) if criteria_lines else "(none)"
    total = len(ticket.get("acceptance_criteria", []))
    checked = sum(1 for c in ticket.get("acceptance_criteria", []) if c["checked"])

    flags = ticket.get("readiness_flags", [])
    deps = ticket.get("depends", [])
    deps_text = ", ".join(deps) if deps else "none"

    return f"""You are a project management assistant analyzing a ticket column move.

TICKET: {ticket['id']} — {ticket['title']}
MOVE: {ticket['section']} → {target_section}
Priority: {ticket['priority']} | Complexity: {ticket['complexity']} | Status: {ticket['status']}

CURRENT STATE:

[D] DESCRIPTION:
{ticket['description'] or '(empty)'}

[C] ACCEPTANCE CRITERIA ({checked}/{total} complete):
{criteria_text}

[T] TESTS: {'SET' if 'tests' in flags else 'NOT SET'}
[R] REVIEWED: {'SET' if 'reviewed' in flags else 'NOT SET'}
[S] SMOKE TESTED: {'SET' if 'smoke' in flags else 'NOT SET'}

DEPENDENCIES: {deps_text}

TASK: Analyze readiness for moving to {target_section}. For each category (D,C,T,R,S), assess completeness and suggest specific improvements if needed.

Respond with ONLY valid JSON (no markdown fences, no explanation) matching this exact schema:
{{
  "verdict": "ready" or "needs-work" or "blocked",
  "summary": "one-line explanation",
  "categories": {{
    "D": {{ "status": "ok" or "needs-work", "current_summary": "brief state", "suggestion": "improvement or null" }},
    "C": {{ "status": "ok" or "needs-work", "current_summary": "brief", "suggestion": "improvement or null", "add_criteria": ["new criterion 1"] }},
    "T": {{ "status": "ok" or "needs-work", "current_summary": "brief", "suggestion": "improvement or null" }},
    "R": {{ "status": "ok" or "needs-work", "current_summary": "brief", "suggestion": "improvement or null" }},
    "S": {{ "status": "ok" or "needs-work", "current_summary": "brief", "suggestion": "improvement or null" }}
  }}
}}"""
```

- [ ] **Step 2: Add gate-check runner**

Add after `_build_gate_prompt`:

```python
def _run_gate_check(project_id: str, ticket_id: str, target_section: str) -> dict:
    """Run the gate-check agent and return structured analysis."""
    import subprocess as _sp

    ticket = _get_ticket_json(project_id, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    prompt = _build_gate_prompt(ticket, target_section)

    try:
        result = _sp.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=90,
            cwd=os.path.expanduser(_get_project().get("path", "."))
        )
        # --output-format json wraps the response in {"type":"result","result":"..."}
        outer = json.loads(result.stdout)
        text = outer.get("result", result.stdout) if isinstance(outer, dict) else result.stdout
        # The agent's text response should be raw JSON
        analysis = json.loads(text) if isinstance(text, str) else text
    except _sp.TimeoutExpired:
        return {"error": "Gate check timed out", "verdict": "needs-work", "summary": "Analysis timed out — review manually."}
    except (json.JSONDecodeError, KeyError):
        return {"error": "Failed to parse agent response", "verdict": "needs-work", "summary": "Could not parse analysis — review manually."}

    # Attach metadata
    analysis["ticket_id"] = ticket_id
    analysis["target_section"] = target_section
    return analysis
```

- [ ] **Step 3: Add route handler in do_POST**

Insert after the `/move` handler block (after line 564, before the readiness flag handler):

```python
        # Gate check before column move
        m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)/gate-check$", path)
        if m:
            ticket_id = m.group(1)
            proj = _get_project()
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            section = body.get("section", "")
            if not section:
                self._send_json({"error": "Missing 'section' field"}, 400)
                return

            result = _run_gate_check(proj["id"], ticket_id, section)
            if "error" in result and "verdict" not in result:
                self._send_json(result, 400)
            else:
                self._send_json(result)
            return
```

- [ ] **Step 4: Test the endpoint with curl**

Start the server and test:

```bash
cd /home/user/projects/ticket-takeaway
python3 src/serve.py &
sleep 2

# Test gate-check endpoint
curl -s -X POST http://localhost:8787/api/tickets/B-05/gate-check \
  -H 'Content-Type: application/json' \
  -d '{"section": "For Review"}' | python3 -m json.tool

# Verify it returns structured JSON with verdict, summary, categories
# Kill server
kill %1
```

Expected: JSON response with `verdict`, `summary`, and `categories` keys.

- [ ] **Step 5: Commit**

```bash
git add src/serve.py
git commit -m "feat: add /gate-check endpoint for column move analysis

Spawns Claude CLI to analyze ticket readiness by DCTRS category
before allowing moves to top kanban columns."
```

---

## Task 2: Gate Panel CSS Styles (`src/generate.py`)

**Files:**
- Modify: `src/generate.py:933` (insert CSS after `.action-btn.danger:hover`)

- [ ] **Step 1: Add gate-checking card pulse animation**

Insert after line 933 (`.action-btn.danger:hover {{ ... }}`):

```css
/* Gate-check: card pulsing state */
@keyframes gatePulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(59,130,246,0.3); }
  50% { box-shadow: 0 0 0 6px rgba(59,130,246,0); }
}
.card.gate-checking {
  animation: gatePulse 1.5s ease-in-out infinite;
  border-color: var(--accent);
}
.card.gate-checking .card-actions { display: none !important; }
```

- [ ] **Step 2: Add gate panel container and verdict styles**

Continue adding after the pulse animation:

```css
/* Gate-check panel */
.gate-panel {
  margin-top: 8px; padding: 10px; border-radius: 6px;
  background: var(--bg-surface); border: 1px solid var(--border-default);
  animation: panelSlide 0.2s ease-out; font-size: 11px;
}
.gate-verdict {
  display: flex; align-items: center; gap: 6px; margin-bottom: 8px;
  padding-bottom: 6px; border-bottom: 1px solid var(--border-default);
}
.gate-verdict-badge {
  font-size: 9px; font-weight: 700; padding: 2px 8px; border-radius: 10px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.gate-verdict-badge.ready { background: rgba(34,197,94,0.15); color: #22c55e; }
.gate-verdict-badge.needs-work { background: rgba(234,179,8,0.15); color: #eab308; }
.gate-verdict-badge.blocked { background: rgba(239,68,68,0.15); color: #ef4444; }
.gate-verdict-summary { color: var(--text-secondary); font-size: 11px; }
```

- [ ] **Step 3: Add per-category row and editable field styles**

```css
/* Gate category rows */
.gate-category {
  padding: 6px 8px; margin: 4px 0; border-radius: 4px;
  border-left: 3px solid var(--border-default);
  background: var(--bg-card);
}
.gate-category.ok { border-left-color: #22c55e; }
.gate-category.needs-work { border-left-color: #eab308; }
.gate-cat-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 4px;
}
.gate-cat-label {
  font-weight: 700; font-size: 10px; text-transform: uppercase;
  letter-spacing: 0.3px; color: var(--text-secondary);
}
.gate-cat-status {
  font-size: 9px; font-weight: 600; padding: 1px 6px; border-radius: 8px;
}
.gate-cat-status.ok { background: rgba(34,197,94,0.12); color: #22c55e; }
.gate-cat-status.needs-work { background: rgba(234,179,8,0.12); color: #eab308; }
.gate-cat-summary { color: var(--text-secondary); margin-bottom: 3px; }
.gate-cat-suggestion {
  color: var(--accent); font-style: italic; margin-bottom: 4px;
  padding: 3px 6px; background: rgba(59,130,246,0.06); border-radius: 3px;
}
.gate-cat-edit {
  width: 100%; min-height: 28px; font-size: 11px; padding: 4px 6px;
  border-radius: 4px; border: 1px solid var(--border-default);
  background: var(--bg-page); color: var(--text-primary);
  font-family: var(--font-sans); resize: vertical; outline: none;
}
.gate-cat-edit:focus { border-color: var(--accent); }
.gate-save-btn {
  font-size: 9px; padding: 2px 8px; border-radius: 4px;
  border: 1px solid var(--border-default); background: var(--bg-page);
  color: var(--text-secondary); cursor: pointer; font-weight: 600; margin-top: 3px;
}
.gate-save-btn:hover { border-color: var(--accent); color: var(--accent); }
.gate-save-btn.saved { color: #22c55e; border-color: #22c55e; pointer-events: none; }
```

- [ ] **Step 4: Add footer button styles**

```css
/* Gate panel footer */
.gate-footer {
  display: flex; gap: 6px; margin-top: 8px; padding-top: 6px;
  border-top: 1px solid var(--border-default);
}
.gate-confirm-btn {
  font-size: 10px; padding: 4px 14px; border-radius: 4px; border: none;
  background: var(--accent); color: #fff; cursor: pointer; font-weight: 600;
  font-family: var(--font-sans);
}
.gate-confirm-btn:hover { background: #2563eb; }
.gate-cancel-btn {
  font-size: 10px; padding: 4px 14px; border-radius: 4px;
  border: 1px solid var(--border-default); background: none;
  color: var(--text-secondary); cursor: pointer; font-family: var(--font-sans);
}
.gate-cancel-btn:hover { color: var(--text-primary); border-color: var(--text-secondary); }
```

- [ ] **Step 5: Commit**

```bash
git add src/generate.py
git commit -m "feat: add CSS styles for gate-check panel

Pulse animation for checking state, verdict badges, per-category
rows with editable fields, and confirm/cancel footer."
```

---

## Task 3: JS Move Interception + Gate Panel Rendering (`src/generate.py`)

**Files:**
- Modify: `src/generate.py:1608-1615` (add apiGateCheck after apiMove)
- Modify: `src/generate.py:1764-1768` (intercept drag-drop)
- Modify: `src/generate.py:1887-1896` (intercept action buttons)
- Modify: `src/generate.py:1897` (insert gate panel functions)

**Security note:** All user-visible text from the agent response is set via `textContent` (not innerHTML) to prevent XSS. DOM elements are created with `document.createElement` and assembled programmatically.

- [ ] **Step 1: Add GATED_SECTIONS constant and apiGateCheck function**

Insert right after the `apiMove` function (after line 1615):

```javascript
    var GATED_SECTIONS = { 'Ideas': 1, 'Backlog': 1, 'WIP': 1, 'For Review': 1, 'Done': 1 };

    function apiGateCheck(ticketId, section) {
      if (!EDIT_API) return Promise.reject('No API');
      return fetch(EDIT_API + '/tickets/' + ticketId + '/gate-check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ section: section })
      }).then(function(r) { return r.json(); });
    }
```

- [ ] **Step 2: Add gate panel helper functions**

Insert after the `apiGateCheck` function:

```javascript
    function setCardGateChecking(card, checking) {
      if (checking) {
        card.classList.add('gate-checking', 'expanded');
      } else {
        card.classList.remove('gate-checking');
      }
    }

    function removeGatePanel(card) {
      var panel = card.querySelector('.gate-panel');
      if (panel) panel.remove();
      card.classList.remove('gate-checking');
    }

    function startGateCheck(ticketId, targetSection) {
      var card = document.querySelector('[data-item-id="' + ticketId + '"]');
      if (!card) return;
      removeGatePanel(card);
      setCardGateChecking(card, true);
      apiGateCheck(ticketId, targetSection).then(function(data) {
        setCardGateChecking(card, false);
        renderGatePanel(card, data, ticketId, targetSection);
      }).catch(function(err) {
        setCardGateChecking(card, false);
        showToast(card, 'Gate check failed');
      });
    }
```

- [ ] **Step 3: Add renderGatePanel function using safe DOM methods**

Insert after `startGateCheck`. All text from the agent response is set via `textContent` to prevent XSS:

```javascript
    function renderGatePanel(card, data, ticketId, targetSection) {
      removeGatePanel(card);
      card.classList.add('expanded');
      var panel = document.createElement('div');
      panel.className = 'gate-panel';

      // Verdict header
      var verdict = data.verdict || 'needs-work';
      var verdictDiv = document.createElement('div');
      verdictDiv.className = 'gate-verdict';
      var badge = document.createElement('span');
      badge.className = 'gate-verdict-badge ' + verdict;
      badge.textContent = verdict.replace(/-/g, ' ');
      var summarySpan = document.createElement('span');
      summarySpan.className = 'gate-verdict-summary';
      summarySpan.textContent = data.summary || '';
      verdictDiv.appendChild(badge);
      verdictDiv.appendChild(summarySpan);
      panel.appendChild(verdictDiv);

      // Category rows
      var catOrder = ['D', 'C', 'T', 'R', 'S'];
      var catLabels = { D: 'Description', C: 'Criteria', T: 'Tests', R: 'Reviewed', S: 'Smoke Tested' };
      var cats = data.categories || {};

      catOrder.forEach(function(key) {
        var cat = cats[key] || {};
        var status = cat.status || 'ok';
        var row = document.createElement('div');
        row.className = 'gate-category ' + status;
        row.dataset.cat = key;

        // Header
        var header = document.createElement('div');
        header.className = 'gate-cat-header';
        var label = document.createElement('span');
        label.className = 'gate-cat-label';
        label.textContent = key + ' \u2014 ' + catLabels[key];
        var statusEl = document.createElement('span');
        statusEl.className = 'gate-cat-status ' + status;
        statusEl.textContent = status.replace(/-/g, ' ');
        header.appendChild(label);
        header.appendChild(statusEl);
        row.appendChild(header);

        // Summary
        var summaryDiv = document.createElement('div');
        summaryDiv.className = 'gate-cat-summary';
        summaryDiv.textContent = cat.current_summary || '';
        row.appendChild(summaryDiv);

        // Suggestion
        if (cat.suggestion) {
          var sugDiv = document.createElement('div');
          sugDiv.className = 'gate-cat-suggestion';
          sugDiv.textContent = cat.suggestion;
          row.appendChild(sugDiv);
        }

        // Editable field for Description
        if (key === 'D') {
          var ta = document.createElement('textarea');
          ta.className = 'gate-cat-edit';
          ta.dataset.field = 'description';
          ta.placeholder = 'Edit description...';
          ta.value = card.dataset.desc || '';
          row.appendChild(ta);
        }

        // Suggested new criteria for C
        if (key === 'C') {
          var addCriteria = cat.add_criteria || [];
          if (addCriteria.length > 0) {
            var hint = document.createElement('div');
            hint.style.cssText = 'margin-top:3px;font-size:10px;color:var(--text-tertiary)';
            hint.textContent = 'Suggested additions:';
            row.appendChild(hint);
            addCriteria.forEach(function(c, i) {
              var cta = document.createElement('textarea');
              cta.className = 'gate-cat-edit';
              cta.dataset.field = 'add_criteria';
              cta.dataset.index = String(i);
              cta.style.cssText = 'min-height:22px;margin-top:2px';
              cta.value = c;
              row.appendChild(cta);
            });
          }
        }

        // Save button for D and C
        if (key === 'D' || (key === 'C' && (cat.add_criteria || []).length > 0)) {
          var saveBtn = document.createElement('button');
          saveBtn.className = 'gate-save-btn';
          saveBtn.dataset.cat = key;
          saveBtn.textContent = 'Save ' + catLabels[key];
          row.appendChild(saveBtn);
        }

        panel.appendChild(row);
      });

      // Footer
      var footer = document.createElement('div');
      footer.className = 'gate-footer';
      var confirmBtn = document.createElement('button');
      confirmBtn.className = 'gate-confirm-btn';
      confirmBtn.textContent = 'Confirm Move \u2192 ' + targetSection;
      var cancelBtn = document.createElement('button');
      cancelBtn.className = 'gate-cancel-btn';
      cancelBtn.textContent = 'Cancel';
      footer.appendChild(confirmBtn);
      footer.appendChild(cancelBtn);
      panel.appendChild(footer);

      // Wire up per-section Save buttons
      panel.querySelectorAll('.gate-save-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
          e.stopPropagation();
          var catKey = btn.dataset.cat;
          var catRow = btn.closest('.gate-category');

          if (catKey === 'D') {
            var textarea = catRow.querySelector('textarea[data-field="description"]');
            if (textarea) {
              apiPut(ticketId, { description: textarea.value }).then(function() {
                btn.textContent = 'Saved \u2714';
                btn.classList.add('saved');
              });
            }
          } else if (catKey === 'C') {
            var fields = catRow.querySelectorAll('textarea[data-field="add_criteria"]');
            var promises = [];
            fields.forEach(function(f) {
              var text = f.value.trim();
              if (text) {
                promises.push(apiPut(ticketId, { add_criteria: text }));
              }
            });
            Promise.all(promises).then(function() {
              btn.textContent = 'Saved \u2714';
              btn.classList.add('saved');
            });
          }
        });
      });

      // Wire up Confirm Move
      confirmBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        if (targetSection === 'Done') {
          fetch(EDIT_API + '/tickets/' + ticketId + '/accept', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}'
          }).then(function(r) { return r.json(); }).then(function() {
            removeGatePanel(card);
            showToast(card, 'Accepted!');
          });
        } else {
          apiMove(ticketId, targetSection).then(function() {
            removeGatePanel(card);
            showToast(card, 'Moved!');
          });
        }
      });

      // Wire up Cancel
      cancelBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        removeGatePanel(card);
      });

      // Insert panel into card (before action buttons if present)
      var actions = card.querySelector('.card-actions');
      if (actions) {
        card.insertBefore(panel, actions);
      } else {
        card.appendChild(panel);
      }
    }
```

- [ ] **Step 4: Intercept drag-drop moves**

Replace lines 1764-1768 in the drag-drop handler. Find:

```javascript
          if (section) {{
            apiMove(id, section).then(function() {{
              showToast(document.querySelector('[data-item-id="' + id + '"]'), 'Moved!');
            }});
          }}
```

Replace with:

```javascript
          if (section) {{
            if (GATED_SECTIONS[section]) {{
              startGateCheck(id, section);
            }} else {{
              apiMove(id, section).then(function() {{
                showToast(document.querySelector('[data-item-id="' + id + '"]'), 'Moved!');
              }});
            }}
          }}
```

- [ ] **Step 5: Intercept action button moves**

Replace lines 1887-1895 in the action-button handler. Find:

```javascript
        if (action === 'move') {{
          var section = btn.dataset.section;
          apiMove(id, section).then(function() {{ showToast(card, 'Moved!'); }});
        }} else if (action === 'accept') {{
          fetch(EDIT_API + '/tickets/' + id + '/accept', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: '{{}}'
          }}).then(function(r) {{ return r.json(); }}).then(function() {{ showToast(card, 'Accepted!'); }});
        }}
```

Replace with:

```javascript
        if (action === 'move') {{
          var section = btn.dataset.section;
          if (GATED_SECTIONS[section]) {{
            startGateCheck(id, section);
          }} else {{
            apiMove(id, section).then(function() {{ showToast(card, 'Moved!'); }});
          }}
        }} else if (action === 'accept') {{
          startGateCheck(id, 'Done');
        }}
```

- [ ] **Step 6: Commit**

```bash
git add src/generate.py
git commit -m "feat: add JS gate-check interception and panel rendering

Intercepts drag-drop and action-button moves to gated columns,
calls /gate-check API, renders expandable panel with per-section
editable fields and independent Save buttons. All agent text is
set via textContent to prevent XSS."
```

---

## Task 4: Integration + `add_criteria` PUT Support

**Files:**
- Modify: `src/serve.py:520-528` (add `add_criteria` handling in PUT)

- [ ] **Step 1: Add add_criteria support to the PUT handler**

The gate panel's per-section Save for criteria sends `{ "add_criteria": "new criterion text" }` via PUT. Add handling for this in the PUT handler.

Find in `do_PUT` (around line 520):

```python
        # Handle criterion toggle specially
        if "toggle_criterion" in body:
```

Insert before that block:

```python
        # Handle adding a new acceptance criterion
        if "add_criteria" in body:
            text = body["add_criteria"]
            if isinstance(text, str) and text.strip():
                with _db_lock:
                    conn = cli.get_db()
                    cli.init_db(conn)
                    proj = _get_project()
                    cli.ingest_markdown(conn, proj)
                    row = conn.execute(
                        "SELECT id FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
                        (ticket_id, project_id)
                    ).fetchone()
                    if row:
                        tid = row["id"]
                        sort_row = conn.execute(
                            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
                            (tid, project_id)
                        ).fetchone()
                        conn.execute(
                            "INSERT INTO acceptance_criteria (ticket_id, project_id, text, checked, sort_order) VALUES (?, ?, ?, 0, ?)",
                            (tid, project_id, text.strip(), sort_row["next_order"])
                        )
                        conn.commit()
                        cli.sync_to_markdown(conn, proj)
                        cli.regenerate_dashboard(proj)
                    conn.close()
                t = _get_ticket_json(project_id, ticket_id)
                self._send_json(t or {"ok": True})
                return

```

- [ ] **Step 2: End-to-end verification**

Start serve.py and test the full flow:

```bash
cd /home/user/projects/ticket-takeaway
python3 src/serve.py &
sleep 2

# 1. Open http://localhost:8787 in browser
# 2. Drag a ticket from Backlog to WIP -> card should pulse, then gate panel appears
# 3. Edit description in the D section -> click "Save Description" -> should show "Saved ✔"
# 4. Click "Cancel" -> panel dismissed, ticket stays in Backlog, description edit persisted
# 5. Drag same ticket to WIP again -> panel appears -> click "Confirm Move -> WIP" -> card moves
# 6. Drag a ticket to Icebox -> should move immediately (no gate)
# 7. Click "Start" button on a Backlog ticket -> gate panel should appear (not immediate move)
# 8. Click "Accept" on a For Review ticket -> gate panel for Done entry

kill %1
```

- [ ] **Step 3: Commit**

```bash
git add src/serve.py
git commit -m "feat: add add_criteria PUT support for gate panel saves

Allows the gate-check panel to save new acceptance criteria
suggestions via the existing PUT endpoint."
```

---

## Verification Checklist

After all tasks are merged:

1. **Gated move (drag-drop):** Drag Backlog to WIP -> card pulses -> panel appears with verdict + DCTRS rows
2. **Gated move (action button):** Click "Start" on Backlog ticket -> same gate panel flow
3. **Ungated move:** Drag to Icebox -> immediate move, no gate
4. **Per-section save:** Edit D textarea -> Save Description -> "Saved" -> cancel move -> description persisted in DB
5. **Criteria save:** Gate suggests new criteria -> Save Criteria -> new criterion appears on ticket
6. **Confirm move:** After reviewing panel -> Confirm Move -> ticket moves to target column
7. **Cancel:** Dismiss panel -> ticket stays, saved edits persist
8. **Accept flow:** "Accept" button -> gate panel for Done -> Confirm -> accepted
9. **Timeout handling:** If claude CLI takes >90s -> error response -> toast shows "Gate check failed"
10. **Malformed response:** If agent returns bad JSON -> fallback error with "needs-work" verdict
