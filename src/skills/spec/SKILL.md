---
name: spec
description: Specify ideas into backlog-ready tickets — write descriptions, acceptance criteria, and optionally suggest TDD test cases. Walks through Ideas tickets to get them past the specification gate.
user_invocable: true
---

# Spec — Idea to Backlog

Take raw ideas and turn them into specified, backlog-ready tickets. Walk through Ideas (or a specific ticket), help the user write a description and acceptance criteria, and move the ticket forward once the gate is met.

**Architecture:** `PRODUCT_BACKLOG.md` (Ideas/Backlog section) --> spec conversation --> updated `PRODUCT_BACKLOG.md` with description + criteria

---

## Mode Detection

| Invocation | Mode |
|---|---|
| `/spec` (no args) | **spec-all** — walk through all Ideas tickets |
| `/spec {ID}` | **spec-one** — specify a single ticket by ID |

---

## Mode 1: spec-all

### Step 1: Read the Backlog

Read `PRODUCT_BACKLOG.md` in the current project directory. If not found in cwd, look up the project via `~/.claude/dashboard/registry.json` and use the registered path.

**Always read fresh — never use cached content.**

### Step 2: Collect Ideas

Parse the `## Ideas` section AND scan `## Backlog` for any tickets with `Status: proposed` (ideas that were placed directly in Backlog).

Collect all `###` entries.

If no items found, report: "No ideas to spec. Add some with `/dashboard add {project} \"title\"`." and stop.

### Step 3: Sort and Present

1. **Sort oldest first** — by numeric part of ID (e.g., I-1 before I-5)
2. **Present the list**:
   ```
   Ideas to spec:
   1. I-01: {title}
   2. I-02: {title}
   3. B-03: {title} (in Backlog, still proposed)

   Walk through all, or pick one? (all / {ID})
   ```

If the user picks a specific ID, switch to spec-one mode for that ticket.

### Step 4: Walk Each Idea

For each idea, in order:

#### 4a. Present What Exists

Show the ticket as-is:
- ID and title
- Any existing description (may be empty)
- Any existing acceptance criteria (may be empty)
- Priority and complexity if set

#### 4b. Explore the Idea

Have a brief conversation to understand what the user wants. Ask:
- **"What should this do?"** — if no description exists
- **"Who is this for and what problem does it solve?"** — to ground the spec
- **"Any constraints or dependencies?"** — to surface blockers early

Keep this conversational, not interrogative. If the user gives a one-liner, that's fine — work with what they give. If they want to go deep, go deep.

#### 4c. Draft the Spec

Based on the conversation, write:

1. **Description** — 1-3 sentences explaining what the feature does and why
2. **Acceptance criteria** — concrete checkboxes defining "done". Aim for 3-6 criteria. Each should be:
   - Observable (you can see or test that it works)
   - Specific (not "works well" but "returns results within 200ms")
   - Independent (each criterion can be verified on its own)

Present the draft to the user:
```
Here's what I've got for {ID}:

{description}
- [ ] {criterion 1}
- [ ] {criterion 2}
- [ ] {criterion 3}

Anything to add, change, or remove?
```

#### 4d. Suggest Test Cases (Optional)

After the criteria are agreed on, offer:

```
Want me to suggest some test cases for these criteria? You can run /tdd {ID} later
to generate full test specs, but here are some high-level ones to consider:

- {test idea 1 — derived from criterion 1}
- {test idea 2 — edge case from criterion 2}
- {test idea 3 — integration concern}

These are just suggestions — take what's useful, ignore the rest.
```

Do NOT run `/tdd` automatically. Just plant the seed. The user can run it themselves when they're ready to build.

#### 4e. Set Priority and Complexity

If not already set (or still at defaults), ask:
- **Priority**: "High, medium, or low?" — default to medium if the user doesn't care
- **Complexity**: "S, M, L, or XL?" — default to M if the user doesn't care

Don't belabor this. If the user says "whatever" or "medium", move on.

#### 4f. Confirm and Write

Present the final ticket:
```markdown
### {ID}: {Title}
Priority: {priority} | Complexity: {complexity} | Status: specified
{Description}
- [ ] {Criterion 1}
- [ ] {Criterion 2}
- [ ] {Criterion 3}
```

Ask: **"Good to save? Move to Backlog?"**

### Step 5: Update the File

On confirmation:

1. **Update the ticket in PRODUCT_BACKLOG.md** — replace the existing `###` block with the fully specced version
2. **Move to `## Backlog`** if the user agreed (remove from `## Ideas`, add to `## Backlog`)
3. **Set status to `specified`**

### Step 6: Continue

Proceed to the next idea. Repeat until all are processed or the user says stop.

### Step 7: Regenerate Dashboard

```bash
python3 ~/.claude/dashboard/generate.py
```

Report summary:
```
Specced {N} tickets: {ID1}, {ID2}, {ID3}
{M} moved to Backlog.
```

---

## Mode 2: spec-one {ID}

Same flow as spec-all but scoped to a single ticket:

1. Read `PRODUCT_BACKLOG.md` fresh
2. Find the ticket by ID in `## Ideas` or `## Backlog` (case-insensitive match)
3. If not found, report: "{ID} not found in Ideas or Backlog." and stop
4. If already `specified` or `ready`, report current state and ask: "This is already specced. Want to revise it?"
5. Run steps 4a through 4f for this ticket
6. Update the file and regenerate dashboard

---

## The Specification Gate

A ticket meets the specification gate when it has:

1. A **description** (at least one sentence explaining what and why)
2. At least one **acceptance criterion** (`- [ ]` item)

That's the minimum. The more the user invests in the spec, the better the build phase will go — but we don't block on perfection. A ticket with a one-line description and two criteria is better than an idea sitting in limbo.

---

## Rules

- **Always read PRODUCT_BACKLOG.md fresh** at the start — never cache between invocations
- **Don't force the user to spec everything** — if they want to skip an idea, skip it
- **Don't auto-run /tdd** — suggest test cases inline but let the user decide when to generate full test specs
- **Respect what the user gives you** — if they write a one-liner, don't demand three paragraphs. Work with what you get.
- **Case-insensitive ID matching** — `i-01` matches `I-01`
- **After any changes to PRODUCT_BACKLOG.md**, regenerate the dashboard:
  ```bash
  python3 ~/.claude/dashboard/generate.py
  ```
- **Status values**: `proposed` (just an idea), `specified` (has description + criteria), `ready` (fully specced + unblocked)
- **Moving from Ideas to Backlog is optional** — the user may want to keep it in Ideas until they're sure. Ask, don't assume.
