---
name: review
description: Review features in the For Review column — walk through tickets in batches, collect feedback or acceptance, create bug sub-tickets, and manage the acceptance flow.
user_invocable: true
---

# Review — Feature Acceptance & Feedback

Orchestrate structured review of completed features. Walk through For Review tickets in batches, collect user feedback or acceptance, create bug sub-tickets when issues are found, and manage the full acceptance flow into PRODUCT_SPECIFICATION.md.

**Architecture:** `PRODUCT_BACKLOG.md` (For Review section) --> review batches --> feedback/accept --> `PRODUCT_BACKLOG.md` + `PRODUCT_SPECIFICATION.md`

---

## Mode Detection

| Invocation | Mode |
|---|---|
| `/review` (no args) | **review-all** — review all For Review tickets |
| `/review {ID}` | **review-one** — review a single ticket by ID |

---

## Mode 1: review-all

### Step 1: Read the Backlog

Read `PRODUCT_BACKLOG.md` in the current project directory. If not found in cwd, look up the project via `~/.claude/ticket-takeaway/registry.json` and use the registered path.

**Always read fresh — never use cached content.**

### Step 2: Collect For Review Items

Parse the `## For Review` section. Collect all `###` entries.

If no items found, report: "Nothing to review." and stop.

### Step 3: Sort and Group

1. **Sort oldest first** — by numeric part of ID (e.g., B-5 before B-12)
2. **Group into review batches** by proximity:
   - Read ticket descriptions, acceptance criteria, and any module/area indicators
   - Group tickets that share UI areas, similar functionality, or dependencies
   - Solo tickets are their own batch
3. **Present batches**:
   ```
   Review batch 1: {theme} ({ID1}, {ID2})
   Review batch 2: {theme} ({ID3})
   ```

### Step 4: Walk Each Batch

For each batch, in order:

#### 4a. Present the Tickets

Show each ticket's full content:
- ID and title
- Priority, complexity, status
- Full description
- Acceptance criteria (`- [ ]` / `- [x]` items)

#### 4b. Check the Codebase

- Search for implementations related to the feature (grep for related components, routes, functions)
- Check for existing tests covering the acceptance criteria

#### 4c. Suggest & Confirm Test Criteria

For each ticket in the batch:

1. **Review existing acceptance criteria** — read the `- [ ]` items on the ticket
2. **Suggest additional criteria** if the existing ones are incomplete:
   - Look at the implementation found in 4b to identify untested edge cases
   - Propose new criteria: "I'd also suggest testing: {criterion}"
3. **Ask the user to confirm** the full set of test criteria:
   - "Here are the acceptance criteria for {ID}. Any to add, remove, or modify?"
4. **Save confirmed criteria** — update the ticket's `- [ ]` items in `PRODUCT_BACKLOG.md` with the agreed set
5. **Check for tests** — if no test files exist for these criteria, suggest: "No tests found. Consider running `/tdd {ID}` to generate them from these criteria."
6. **Run existing tests** if they exist — report pass/fail

This ensures every reviewed ticket has a confirmed, documented set of test criteria saved directly on the ticket record before the accept/reject decision.

#### 4d. Ask the User

Prompt: **"Accept, give feedback, or skip this batch?"**

Handle per ticket within the batch — the user can accept some and give feedback on others.

### Step 5: Handle Response

See **On Feedback**, **On Acceptance**, and **On Skip** sections below.

### Step 6: Continue

After handling one batch, proceed to the next. Repeat until all batches are processed.

---

## Mode 2: review-one {ID}

Same flow as review-all but scoped to a single ticket:

1. Read `PRODUCT_BACKLOG.md` fresh
2. Find the ticket by ID in `## For Review` (case-insensitive match)
3. If not found, report: "{ID} not found in For Review section." and stop
4. Present the ticket with full details
5. Check the codebase for implementations and tests
6. Ask: "Accept, give feedback, or skip?"
7. Handle the response

---

## On Feedback

When the user provides feedback on a ticket:

### 1. Collect the Issue

Ask the user to describe the issue if they haven't already.

### 2. Create Bug Sub-Ticket

Auto-generate bug ID: scan all `### BUG-{N}` entries in PRODUCT_BACKLOG.md, find the highest N, increment by 1.

Add the bug entry to the `## Bugs` section of PRODUCT_BACKLOG.md:

```markdown
### BUG-{N}: {Brief description}
Priority: {priority} | Complexity: S | Status: bug
Parent: {parent-ticket-ID}
{User's feedback description}
- [ ] {Fix criterion derived from feedback}
```

**Priority inference:** Default to the parent ticket's priority. If the user specifies severity, use that instead.

### 3. Create/Update Review File

Create or append to `docs/features/{parent-ID}/REVIEW.md`:

```markdown
## Review Feedback
### Session — {YYYY-MM-DD}
Result: feedback
- BUG-{N}: {description}
```

If the file already exists, append a new session entry.

### 4. Update Parent Status

In PRODUCT_BACKLOG.md, change the parent ticket's status from `for-review` to `rework`:

Change `Status: for-review` to `Status: rework` on the metadata line. The ticket stays in the `## For Review` section.

### 5. Report

```
Created BUG-{N} linked to {parent-ID}. Parent status -> rework.
```

### 6. Continue

Proceed to the next ticket in the batch.

---

## On Acceptance

When the user accepts a ticket:

### 1. Check for Open Bugs

Search `## Bugs` section for entries with `Parent: {ID}` that do NOT have `Status: bug-fixed`.

If open bugs exist:
```
{N} open bug(s) linked to {ID}. Resolve them first or force-accept.
```
Wait for user decision. If they don't force-accept, skip this ticket.

### 2. Run /sync

If `docs/features/{ID}/` exists, run `/sync` to extract learnings before cleanup. This step is **mandatory** — never skip it.

### 3. Verify Acceptance Criteria

- The test criteria should already be confirmed from step 4c of the review walk-through (they were saved to the ticket)
- Check `- [ ]` items in the ticket — note any unchecked criteria
- Run tests if available (search for test files related to the feature)
- Report verification status to the user

### 4. Move to Done

In PRODUCT_BACKLOG.md:
- Remove the ticket from `## For Review`
- Add it under `## Done` with `Status: done`

### 5. Summarize to PRODUCT_SPECIFICATION.md

Append a summary to `PRODUCT_SPECIFICATION.md` including:
- Feature description
- Development notes: bug count during development, key decisions
- Pull from `docs/features/{ID}/REVIEW.md` and `docs/features/{ID}/NOTES.md` if they exist

### 6. Clean Up

Delete `docs/features/{ID}/` directory if it exists (working files are captured by sync + spec summary).

### 7. Commit

Stage changes and commit:
```
feat: accept {ID}: {Title}
```

### 8. Regenerate Dashboard

```bash
python3 ~/.claude/ticket-takeaway/generate.py
```

### 9. Report

```
{ID} accepted -> Done. Committed.
```

---

## On Skip

No changes. Move to the next batch.

---

## Rules

- **Always read PRODUCT_BACKLOG.md fresh** at the start — never cache between invocations
- **Bug IDs use the `BUG-` prefix** with auto-incrementing number (scan existing BUG-N entries to find the next number)
- **Bug sub-tickets always include `Parent: {parent-ID}`** on the line after the metadata line
- **The `/sync` step before acceptance is mandatory** — never skip it
- **If `docs/features/{ID}/` doesn't exist, that's fine** — not all tickets have working files
- **After any changes to PRODUCT_BACKLOG.md**, regenerate the dashboard:
  ```bash
  python3 ~/.claude/ticket-takeaway/generate.py
  ```
- **Present review batches oldest first**, grouped by proximity
- **Case-insensitive ID matching** — `b-05` matches `B-05`
- **Acceptance criteria checkboxes**: `- [ ]` = unchecked, `- [x]` = checked
- **Status values**: `for-review` (awaiting review), `rework` (feedback given, needs fixes), `bug` (open bug), `bug-fixed` (resolved bug), `done` (accepted)
