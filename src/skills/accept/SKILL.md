---
name: accept
description: Accept a reviewed ticket — move it from For Review to Done, summarize into PRODUCT_SPECIFICATION.md, clean up working files. The final gate in the Ticket Takeaway process.
user_invocable: true
---

# Accept — For Review to Done

Accept a ticket that has passed review. Moves it to Done, writes a permanent summary to `PRODUCT_SPECIFICATION.md`, and cleans up ephemeral working files.

**Architecture:** `PRODUCT_BACKLOG.md` (For Review) --> accept --> `PRODUCT_SPECIFICATION.md` + cleanup

---

## Mode Detection

| Invocation | Mode |
|---|---|
| `/accept {ID}` | Accept a specific ticket by ID |
| `/accept` (no args) | List For Review tickets and ask which to accept |

---

## Steps

### 1. Read the Backlog

Read `PRODUCT_BACKLOG.md` in the current project directory. If not found in cwd, look up the project via `~/.claude/ticket-takeaway/registry.json` and use the registered path.

**Always read fresh — never use cached content.**

### 2. Find the Ticket

If an ID was given, find it in `## For Review` (case-insensitive match). If not found, report: "{ID} not found in For Review section." and stop.

If no ID was given, list all tickets in `## For Review` and ask: "Which ticket to accept?"

### 3. Check for Open Bugs

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py list --project <project> --section bugs
```
Look for entries with parent matching {ID} that do NOT have `Status: bug-fixed`.

If open bugs exist:
```
{N} open bug(s) linked to {ID}. Resolve them first or force-accept?
```
Wait for user decision. If they don't force-accept, stop.

### 4. Run /sync

If `docs/features/{ID}/` exists, run `/sync` to extract learnings before cleanup. This step is **mandatory** — never skip it.

### 5. Verify Acceptance Criteria

- Check unchecked criteria in the ticket
- Run tests if available (search for test files related to the feature)
- Report verification status to the user

### 6. Accept the Ticket

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py accept <project> <ID>
```

This moves the ticket to Done, appends to PRODUCT_SPECIFICATION.md, and syncs the markdown.

### 7. Clean Up

Delete `docs/features/{ID}/` directory if it exists (working files are captured by sync + spec summary).

### 8. Commit

Stage changes and commit:
```
feat: accept {ID}: {Title}
```

### 9. Regenerate Dashboard

```bash
python3 ~/.claude/ticket-takeaway/generate.py
```

### 10. Report

```
{ID} accepted → Done. Committed.
```

---

## Rules

- **Always read PRODUCT_BACKLOG.md fresh** at the start
- **The `/sync` step before acceptance is mandatory** — never skip it
- **If `docs/features/{ID}/` doesn't exist, that's fine** — not all tickets have working files
- **Case-insensitive ID matching** — `b-05` matches `B-05`
- **After any changes to PRODUCT_BACKLOG.md**, regenerate the dashboard
