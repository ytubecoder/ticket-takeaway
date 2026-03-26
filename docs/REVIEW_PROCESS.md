# Review Process Specification

## Overview

The review process is initiated by double-clicking the "For Review" column heading on the dashboard. This copies a prompt to clipboard that, when pasted into a Claude Code session, runs a structured review of all tickets in the For Review column.

---

## 1. Entry Point: Dashboard Column Heading Double-Click

When you double-click "For Review" on the dashboard:
- Copies a prompt to clipboard: `Run /review {project}` (or similar)
- Green "Copied!" toast appears on the column heading
- The prompt is designed to be pasted into a Claude Code instance that has the review skill loaded

**All column headings** support double-click to copy a contextual prompt:

| Column | Prompt Copied |
|--------|--------------|
| Ideas | `Review ideas for {project} and help prioritize` |
| Backlog | `Help spec the next backlog items for {project}` |
| WIP | `Show me current WIP status for {project}` |
| For Review | `/review {project}` |

---

## 2. Review Skill (`/review`)

A new skill that orchestrates the review process.

### What It Does

1. **Read `PRODUCT_BACKLOG.md`** — collect all items in `## For Review`
2. **Sort and group items**:
   - Sort oldest first (by ID number — lower = older)
   - Group proximate tickets: features that share UI areas, similar functionality, or dependencies
   - Present groups as review batches
3. **For each batch**, walk through:
   - Show the ticket(s) in the batch with their descriptions and acceptance criteria
   - Demo/verify: check the codebase for the implementation, run relevant tests
   - Ask the user for feedback or acceptance
4. **Handle feedback** → create sub-ticket (bug) linked to the parent feature
5. **Handle acceptance** → run `/sync`, summarize, move to Done/PRODUCT_SPECIFICATION.md

### Grouping Logic

Group tickets that are proximate — meaning they:
- Share the same UI area (e.g., both touch the command center, both are form-related)
- Have dependencies on each other
- Were developed in the same release/sprint
- Touch similar code paths

Present each group as: "Review batch 1: Command Center features (B-12, B-15, B-22)"

---

## 3. Feedback → Sub-Ticket (Bug)

When the user gives feedback (not acceptance), the review skill:

### Creates a Bug Sub-Ticket

1. **Create bug entry** in `## Bugs` section of `PRODUCT_BACKLOG.md`:
   ```markdown
   ### BUG-{N}: {Brief description of issue}
   Priority: {high|medium|low} | Complexity: S | Status: bug
   Parent: {parent-ticket-ID}
   {Detailed feedback from user}
   - [ ] {Fix criterion 1}
   - [ ] {Fix criterion 2}
   ```

2. **Link from parent ticket** — add a line to the parent's per-feature working file:
   ```
   docs/features/{parent-ID}/REVIEW.md:
   ## Review Feedback
   - BUG-{N}: {description} (Status: bug)
   ```

3. **Update parent ticket status** to `rework` (stays in For Review section but status changes)

### Sub-Ticket Display on Dashboard

The dashboard should show parent→child relationships:
- Bug tickets with a `Parent: {ID}` field are visually indented or tagged under their parent in the For Review column
- The parent card shows a badge: "2 bugs" (count of linked open bugs)
- When all linked bugs are resolved, the parent can be re-reviewed

---

## 4. Acceptance Flow

When the user accepts a batch:

1. **Run `/sync`** — extract learnings from `docs/features/{ID}/`
2. **Run test criteria** — verify acceptance criteria checkboxes (lean into `/tdd` for test generation if tests don't exist)
3. **Summarize into `PRODUCT_SPECIFICATION.md`** — include:
   - Feature description
   - Bug count during development
   - Key decisions
   - Feedback received during review (summarized)
4. **Move ticket** from `## For Review` to `## Done` in backlog
5. **Clean up** `docs/features/{ID}/` working files
6. **Commit** with appropriate message referencing the ticket IDs

---

## 5. Bug Resolution Cycle

After feedback creates bug sub-tickets:

1. **Bug appears** in `## Bugs` section of backlog and on the dashboard
2. **Another agent** (or the developer) picks up the bug via double-click copy (`I want to work on BUG-{N}: {title}`)
3. **Agent fixes the bug**, updates status to `bug-fixed`
4. **Bug is verified** — removed from Bugs section
5. **Parent ticket's bug count decreases** — when all bugs are resolved, parent is ready for re-review
6. **Review process repeats** for the parent ticket until accepted with no bugs

---

## 6. Test Criteria Integration

Each feature ticket should have acceptance criteria (`- [ ]` checkboxes). During review:

1. **Check for existing tests** — search the codebase for tests covering the feature
2. **If no tests exist** — use `/tdd` skill to generate test specs from the acceptance criteria
3. **Run tests** — verify they pass
4. **Mark criteria** — update `- [ ]` to `- [x]` for verified items
5. **Fail review** if critical criteria don't have passing tests

---

## 7. Per-Feature Working Files During Review

```
docs/features/{ID}/
  PLAN.md              # From development phase
  NOTES.md             # Development notes
  BUGS.md              # Bugs found during development
  TESTS.md             # Test plan and results
  REVIEW.md            # NEW: Review feedback, linked bugs, review history
```

### REVIEW.md Format

```markdown
# Review — {ID}: {Title}

## Review Sessions

### Session 1 — {date}
Reviewer: {user}
Result: feedback

Feedback:
- BUG-05: Button alignment off on mobile
- BUG-06: Missing loading state on form submit

### Session 2 — {date}
Reviewer: {user}
Result: accepted

Notes:
- All bugs resolved
- Tests passing
```

---

## 8. Dashboard Changes Required

### Column heading double-click
- Each column heading gets the same double-click-to-copy behavior as cards
- The prompt copied is column-specific (see table in section 1)
- Same green "Copied!" toast

### Sub-ticket display
- Bug tickets with `Parent: {ID}` show as indented/nested under their parent
- Parent cards show a bug count badge when linked bugs exist
- The `Parent:` field is parsed from the ticket metadata line

### Ticket metadata extension
Add optional `Parent:` field to the ticket format:
```markdown
### BUG-05: Button alignment off on mobile
Priority: high | Complexity: S | Status: bug
Parent: B-12
{description}
```

---

## 9. Implementation Phases

### Phase 1: Dashboard column heading prompts
- Add double-click to column headings in `generate.py`
- Column-specific prompt text

### Phase 2: Review skill (`/review`)
- Create `~/.claude/skills/review/SKILL.md`
- Implement sorting, grouping, and review walk-through
- Feedback → bug sub-ticket creation
- Acceptance → sync + commit flow

### Phase 3: Sub-ticket linking
- Parse `Parent:` field in generate.py
- Visual nesting in dashboard
- Bug count badges on parent cards

### Phase 4: Test criteria integration
- `/tdd` integration for test generation from acceptance criteria
- Test verification during review

### Phase 5: Bug resolution monitoring
- Agent workflow for bug pickup and resolution
- Re-review cycle automation
