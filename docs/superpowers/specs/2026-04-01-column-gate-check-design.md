# Column Move Gate Check — Design Spec

**Date:** 2026-04-01
**Status:** Draft

## Problem

Tickets move freely between dashboard columns with no quality validation. The DCTRS readiness indicators (Description, Criteria, Tests, Review, Smoke) exist visually but aren't enforced. Incomplete tickets land in columns they aren't ready for.

## Solution

When a ticket enters a top kanban column (Ideas, Backlog, WIP, For Review, Done), a Claude Code CLI agent analyzes the ticket's readiness by DCTRS category and returns structured recommendations. The user sees an expandable panel on the card with per-category editable sections, can save edits independently, and then confirm or cancel the move.

## Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Gate engine | Claude Code CLI subprocess | Flexible, extensible, handles nuance. No hardcoded rules to maintain. |
| Wait UX | Inline card pulsing (non-blocking) | User can keep browsing while check runs |
| Review UX | Expandable card panel | Keeps context on the board, not a separate modal |
| Save model | Per-section Save (independent of move) | User can save edits and cancel move — edits persist |
| Gate scope | Top columns only | Bottom sections (Bugs, Icebox, Won't Do) are triage, not progression |
| Gate trigger | On entry to column (any direction) | Whether coming forward or backward, the destination column's expectations apply |

## Data Flow

```
User action (drag/button) → JS intercepts
  → Card enters pulsing ".gate-checking" state
  → POST /api/tickets/{id}/gate-check { target_section }
  → serve.py loads ticket context, spawns claude CLI
  → Agent returns JSON { verdict, summary, categories: { D, C, T, R, S } }
  → Panel renders on card with editable sections
  → Per-section Save → PUT /api/tickets/{id} (immediate, independent)
  → Confirm Move → POST /api/tickets/{id}/move
  → Cancel → panel dismissed, saved edits persist
```

## Gate-Check Endpoint

`POST /api/tickets/{id}/gate-check`

**Request:** `{ "target_section": "For Review" }`

**Response:**
```json
{
  "verdict": "ready | needs-work | blocked",
  "summary": "One-line explanation",
  "categories": {
    "D": { "status": "ok|needs-work", "current_summary": "...", "suggestion": "..." },
    "C": { "status": "ok|needs-work", "current_summary": "...", "suggestion": "...", "add_criteria": ["..."] },
    "T": { "status": "ok|needs-work", "current_summary": "...", "suggestion": "..." },
    "R": { "status": "ok|needs-work", "current_summary": "...", "suggestion": "..." },
    "S": { "status": "ok|needs-work", "current_summary": "...", "suggestion": "..." }
  }
}
```

## Agent Prompt

The prompt includes:
- Ticket ID, title, priority, complexity, current status
- Current and target section
- Full description text
- All acceptance criteria with checked/unchecked status
- Readiness flag states (T, R, S)
- Dependency tickets with their statuses
- Child bug tickets with their statuses
- Instruction to return structured JSON only

## Frontend Panel

- **Verdict header:** Color-coded badge (green/amber/red) + summary
- **Per-DCTRS row:** Status icon, editable field, agent suggestion hint, Save button
  - D: textarea for description
  - C: checkbox list + suggested new criteria
  - T/R/S: toggle + suggestion text
- **Footer:** "Confirm Move →" button, "Cancel" link

## Gated Columns

| Entering | Gated? |
|----------|--------|
| Ideas | Yes |
| Backlog | Yes |
| WIP | Yes |
| For Review | Yes |
| Done | Yes |
| Bugs | No |
| Icebox | No |
| Won't Do | No |

## Files Modified

- `src/serve.py` — New `/gate-check` endpoint (~50 lines)
- `src/generate.py` — Move interception JS, gate panel CSS/JS (~200 lines)
- `src/tickets-cli.py` — No changes

## Verification

1. Gated move: drag Backlog→WIP → panel appears → save section → confirm → moves
2. Ungated move: drag to Icebox → immediate move, no gate
3. Cancel with saved edits: gate fires → edit section → save → cancel → edits persist, no move
4. Action buttons: "▶ Start" triggers gate (not immediate move)
5. Error handling: CLI timeout → graceful error state on card
6. Malformed response: agent returns bad JSON → fallback error message
