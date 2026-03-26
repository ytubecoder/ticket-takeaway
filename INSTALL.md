# Installation Guide

This document explains how to deploy the Ticket Takeaway system from this repository to a new machine.

## Prerequisites

- Python 3.10+
- Claude Code CLI installed
- Git (for code stats collection)

## Quick Install

```bash
# 1. Copy the generator script
mkdir -p ~/.claude/dashboard
cp src/generate.py ~/.claude/dashboard/generate.py
chmod +x ~/.claude/dashboard/generate.py

# 2. Install the dashboard skill
mkdir -p ~/.claude/skills/dashboard
cp src/skills/dashboard/SKILL.md ~/.claude/skills/dashboard/SKILL.md

# 3. Install the review skill
mkdir -p ~/.claude/skills/review
cp src/skills/review/SKILL.md ~/.claude/skills/review/SKILL.md

# 4. Install the spec skill
mkdir -p ~/.claude/skills/spec
cp src/skills/spec/SKILL.md ~/.claude/skills/spec/SKILL.md

# 5. Create a registry (edit paths for your machine)
cp src/registry.example.json ~/.claude/dashboard/registry.json
# Then edit ~/.claude/dashboard/registry.json to set your project paths
```

## File Deployment Map

| Source (in this repo) | Deployed Location | Purpose |
|----------------------|-------------------|---------|
| `src/generate.py` | `~/.claude/dashboard/generate.py` | Dashboard HTML generator script |
| `src/skills/dashboard/SKILL.md` | `~/.claude/skills/dashboard/SKILL.md` | `/dashboard` skill for Claude Code |
| `src/skills/review/SKILL.md` | `~/.claude/skills/review/SKILL.md` | `/review` skill for Claude Code |
| `src/skills/spec/SKILL.md` | `~/.claude/skills/spec/SKILL.md` | `/spec` skill for Claude Code |
| `src/registry.example.json` | `~/.claude/dashboard/registry.json` | Project registry (edit for your projects) |

## Per-Project Setup

For each project you want to track:

### 1. Add to registry

Edit `~/.claude/dashboard/registry.json`:

```json
{
  "projects": [
    {
      "id": "myproject",
      "name": "My Project",
      "path": "~/projects/myproject",
      "description": "What this project does",
      "active": true
    }
  ]
}
```

### 2. Create PRODUCT_BACKLOG.md in the project root

```markdown
# Product Backlog — My Project

## WIP

## For Review

## Backlog

## Ideas

## Bugs

## Icebox

## Done

## Won't Do
```

### 3. Add backlog rules to the project's CLAUDE.md

Add this section to the project's `CLAUDE.md`:

```markdown
## Product Backlog Rules

`PRODUCT_BACKLOG.md` is the single source of truth for all active feature work.
The Ticket Takeaway dashboard (`/dashboard`) reads directly from this file.

**Closed-loop workflow — every feature status change must update PRODUCT_BACKLOG.md:**

1. **Starting work on a feature:** Move the item from `## Backlog` to `## WIP`, set `Status: in-progress`
2. **Feature blocked:** Update status to `Status: blocked` (stays in `## WIP`)
3. **Code complete, ready for review:** Move from `## WIP` to `## For Review`, set `Status: for-review`
4. **Feature accepted:** Run `/dashboard accept {project} {ID}` — moves item to `PRODUCT_SPECIFICATION.md`
5. **New feature idea:** Add to `## Ideas` or `## Backlog` in `PRODUCT_BACKLOG.md` (or use `/dashboard add`)

**This is mandatory.** Do not complete feature work without updating the backlog file.
```

### 4. Create PRODUCT_SPECIFICATION.md (optional, for accepted features)

```markdown
# Product Specification — My Project

Accepted and shipped features.
```

### 5. Generate the dashboard

```bash
cd /path/to/myproject
python3 ~/.claude/dashboard/generate.py
# Opens docs/sdlc-dashboard.html in browser
```

Or use the Claude Code skill:
```
/dashboard
```

## Verification

After installation, verify everything works:

```bash
# Should generate HTML and open browser
cd /path/to/your/project
python3 ~/.claude/dashboard/generate.py

# Should show the dashboard skill
claude /dashboard

# Should show the review skill
claude /review
```

## Updating

To update the system, pull latest from this repo and re-copy:

```bash
cd ~/projects/ticket-takeaway
git pull
cp src/generate.py ~/.claude/dashboard/generate.py
cp src/skills/dashboard/SKILL.md ~/.claude/skills/dashboard/SKILL.md
cp src/skills/review/SKILL.md ~/.claude/skills/review/SKILL.md
cp src/skills/spec/SKILL.md ~/.claude/skills/spec/SKILL.md
```

The registry is NOT overwritten on update (it contains your local project paths).
