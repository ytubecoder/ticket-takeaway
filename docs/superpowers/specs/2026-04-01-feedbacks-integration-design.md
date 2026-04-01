# Feedbacks Integration Design

**Date:** 2026-04-01
**Status:** Draft
**Scope:** Cross-package integration between ticket-takeaway and feedbacks

## Overview

Integrate the feedbacks screen+voice capture tool with ticket-takeaway's SDLC workflow. The integration is **one-way** (ticket-takeaway depends on feedbacks, never the reverse) and **optional** (both packages work fully standalone).

Two use cases:
1. **Review stage** — during `/review`, offer feedbacks for visual bug capture on tickets
2. **General feedback** — standalone feedbacks sessions push context into the agent, which can act on it however makes sense

## Principles

- **Feedbacks owns its output.** Sessions save to feedbacks' native location (`~/projects/feedbacks/sessions/`). Feedbacks has no awareness of ticket-takeaway.
- **Ticket-takeaway is the orchestrator.** It detects feedbacks, starts it, analyzes sessions, and links them to tickets when appropriate.
- **One-way dependency.** Ticket-takeaway → feedbacks. Never the reverse.
- **Optional integration.** If feedbacks isn't installed, all feedbacks-related steps are silently skipped. If ticket-takeaway isn't installed, feedbacks works as a standalone capture tool.
- **Skills ship from their repos.** The feedbacks skill ships from the feedbacks repo. The wrapper skill ships from the ticket-takeaway repo. Both deploy to `~/.claude/skills/feedbacks/` — ticket-takeaway's version is a superset.

## Architecture

```
feedbacks repo                          ticket-takeaway repo
├── skills/feedbacks/SKILL.md           ├── src/skills/feedbacks/SKILL.md
│   (base: setup, start, analyze)       │   (wrapper: base + context push)
│                                       │
├── server.py (HTTP + whisper)          ├── src/skills/review/SKILL.md
├── index.html (capture UI)             │   (updated: uses wrapper for feedbacks steps)
├── start.sh (launcher)                 │
└── sessions/ (native output)           ├── install.py (deploys wrapper skill)
                                        └── INSTALL.md (updated deployment map)
```

### Deployment precedence

| Installed | Skill at `~/.claude/skills/feedbacks/` |
|---|---|
| feedbacks only | Base skill (setup, start, analyze) |
| ticket-takeaway only | Wrapper skill (includes base, feedbacks detection reports "not installed") |
| Both | Wrapper overwrites base (superset — includes base + ticket-takeaway awareness) |

## Changes: feedbacks repo

### 1. Add `skills/feedbacks/SKILL.md`

Move the existing skill content from `~/.claude/skills/feedbacks/SKILL.md` into the feedbacks repo at `skills/feedbacks/SKILL.md`. This becomes the canonical source.

The skill covers three modes:
- **setup** — install whisper.cpp, download model
- **start** — launch the capture app at `:8080`, start whisper at `:8081`
- **analyze** — ingest a session (session.md + images), produce structured analysis

No ticket-takeaway awareness. No ticket creation. Pure capture and analysis.

### 2. Add install instructions

Update `README.md` with a skill installation section:

```bash
# Install the /feedbacks skill for Claude Code
cp -r skills/feedbacks ~/.claude/skills/feedbacks
```

Or add an `install.sh` that does this alongside any other setup.

### 3. No runtime changes

`server.py`, `index.html`, `start.sh` — no modifications. Feedbacks remains independent.

## Changes: ticket-takeaway repo

### 1. Add `src/skills/feedbacks/SKILL.md` (wrapper skill)

A superset of the base feedbacks skill that adds ticket-takeaway context awareness.

#### Modes (same as base, with additions)

| Invocation | Behavior |
|---|---|
| `/feedbacks setup` | Delegates to base skill setup logic |
| `/feedbacks start` | Delegates to base skill start. No ticket context. |
| `/feedbacks start {ticket-id}` | Starts feedbacks with `FEEDBACKS_OUTPUT_DIR={project}/.feedbacks/{ticket-id}/`. Session lands directly in the ticket's feedbacks directory. |
| `/feedbacks analyze` | Runs base analysis. After completion, pushes session path and summary into agent context. Does NOT offer ticket creation or auto-link. |
| `/feedbacks` (no args) | Auto-detect (same as base skill logic) |

#### Context push after analysis

After the base analyze completes, the wrapper adds to the agent context:

> "User feedback captured at `{session_path}`. Summary: {summary from summary.json or analysis}"

This is informational only. The agent can:
- Create a ticket if the content warrants it
- Link it to an existing ticket if relevant
- Do nothing — not all feedback becomes a ticket

#### Relationship to base skill

The wrapper skill is a **complete replacement**, not an overlay. It contains all the base skill content (setup, start, analyze instructions) plus the ticket-takeaway additions. This avoids cross-skill invocation complexity — there's only ever one `/feedbacks` skill installed at a time.

When writing the wrapper, copy the base skill's setup/start/analyze sections verbatim, then add the wrapper-specific behavior (context push, ticket-linked output dir) in the appropriate places.

#### Feedbacks detection

The wrapper detects feedbacks via:
```bash
ls ~/projects/feedbacks/start.sh 2>/dev/null
```

If not found, report: "Feedbacks is not installed. Install from https://github.com/ytubecoder/feedbacks for screen+voice capture."

### 2. Update `src/skills/review/SKILL.md`

The existing `/review` skill already documents feedbacks integration at two points. Formalize these to use the wrapper skill:

#### Step 4a (presenting tickets for review)

Unchanged — check `.feedbacks/{ticket-id}/` for prior sessions. If found, invoke `/feedbacks analyze {session_path}` on the latest one. Present analysis as additional review context.

#### Step 1b (giving feedback on a ticket)

When the user provides feedback and feedbacks is detected:

1. Offer: *"Want to record visual feedback? You can point at the UI and narrate the issue."*
2. If yes: invoke `/feedbacks start {ticket-id}` — wrapper sets `FEEDBACKS_OUTPUT_DIR={project}/.feedbacks/{ticket-id}/`
3. Session saves directly to `.feedbacks/{ticket-id}/feedbacks-{timestamp}/`
4. When user returns, invoke `/feedbacks analyze` on the latest session in that directory
5. Use findings to enrich the bug sub-ticket (description, acceptance criteria, referenced screenshots)

If feedbacks is not installed, skip silently — do not mention it.

### 3. Update `INSTALL.md`

Add to the deployment map:

| Source | Deployed Location | Purpose |
|---|---|---|
| `src/skills/feedbacks/SKILL.md` | `~/.claude/skills/feedbacks/SKILL.md` | `/feedbacks` wrapper skill |

### 4. Update `install.py`

Add the feedbacks skill to the deployment list alongside the existing skills (review, accept, spec, ticket-takeaway).

## Session Directory Structure

### Ticket-linked sessions (via `/review`)

```
{project_root}/.feedbacks/
├── B-05/
│   └── feedbacks-2026-04-01-14-30-00/
│       ├── session.md
│       ├── player.html
│       ├── meta.json          # ticketId: "B-05"
│       ├── summary.json
│       ├── debug.log
│       └── images/
│           ├── 001.png
│           └── ...
└── B-12/
    └── feedbacks-2026-04-01-15-00-00/
        └── ...
```

These are created by the wrapper skill setting `FEEDBACKS_OUTPUT_DIR` when starting feedbacks with a ticket ID.

### Standalone sessions (via `/feedbacks` alone)

```
~/projects/feedbacks/sessions/
└── feedbacks-2026-04-01-16-00-00/
    ├── session.md
    ├── player.html
    ├── meta.json              # no ticketId
    └── images/
        └── ...
```

These stay in feedbacks' native location. If later linked to a ticket, the agent or user can copy:
```bash
cp -r ~/projects/feedbacks/sessions/feedbacks-{timestamp} {project}/.feedbacks/{ticket-id}/
```

This is a manual/agent decision, not automated by the skill.

## What's NOT in scope

- **No changes to feedbacks' runtime** (server.py, index.html)
- **No new CLI subcommands** in tickets-cli.py
- **No automatic ticket creation** from standalone sessions
- **No reverse dependency** (feedbacks knowing about ticket-takeaway)
- **No session state tracking** beyond what meta.json already provides
