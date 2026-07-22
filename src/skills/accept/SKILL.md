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

### 5. Verify — Really Run It

Do not search for test files and eyeball them. Run the project's declared verify command and record the real result:

```bash
CLI=~/.claude/ticket-takeaway/tickets-cli.py
python3 $CLI verify <project> <ID>
```

This reads `[verify]` from the project's `WORKFLOW.toml` (falling back to `tests/run-tests.sh`, then a `package.json` test script, then `pytest`), runs it, and records the command, exit code, output tail, and commit sha onto the ticket. **Non-zero exits are recorded too** — a failing verify is evidence, and it is what the gate will refuse on.

If it reports no verify command, ask the user once and write the answer into the project's `WORKFLOW.toml` so the next close is deterministic:

```toml
[verify]
command = "tests/run-tests.sh"
timeout_ms = 600000
```

Then check each obligation against the diff:
- **lane A/B** → the requirement scenarios in the spec delta
- **lane C** → the ticket's acceptance criteria rows

Same rigour, same evidence standard, different source. If observable behaviour changed on a lane-C ticket and no delta exists, write one now, from the diff — `python3 $CLI spec <project> <ID> --lane C` creates the change, then use `openspec instructions specs --change <name>`.

### 6. Accept the Ticket

```bash
python3 $CLI accept <project> <ID>
```

**This is a gate, not a formality.** Before anything is written it requires a passing verify run against current HEAD, and a spec delta that passes `openspec validate --strict` (or an explicit, justified lane-C claim that nothing observable changed). On success it archives the change — merging the delta into `openspec/specs/` — *before* writing PRODUCT_SPECIFICATION.md, so the archive diff lands in the same commit as the accept.

Preview what it will decide without changing anything:

```bash
python3 $CLI gate <project> <ID>
```

**If it refuses, report the refusal to the user verbatim and stop.** The message names exactly what is missing. Do not work around it by editing flags, hand-archiving, or re-running until it passes. The override exists and is deliberate:

```bash
python3 $CLI accept <project> <ID> --force "<reason>"
```

`--force` records the reason on the ticket, in the audit log, and in PRODUCT_SPECIFICATION.md. Only use it when the user explicitly asks for it, and quote the reason they gave.

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
