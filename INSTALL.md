# Installation Guide

## Prerequisites

- Python 3.10+
- Claude Code CLI installed
- Git (for code stats collection)

## Quick Install

From your project directory:

```bash
git clone https://github.com/ytubecoder/ticket-takeaway.git ~/projects/ticket-takeaway
python3 ~/projects/ticket-takeaway/install.py --register
```

This will:
1. Copy `tickets-cli.py`, `generate.py` to `~/.claude/ticket-takeaway/`
2. Copy `generate.py` to `~/.claude/dashboard/` (with path fix)
3. Install skills (`/dashboard`, `/review`, `/spec`, `/accept`) to `~/.claude/skills/`
4. Create `registry.json` (or preserve existing)
5. Register the current project
6. Seed the SQLite DB from your `PRODUCT_BACKLOG.md` (if it exists)

### Custom registration

```bash
python3 ~/projects/ticket-takeaway/install.py --register --id myproject --name "My Project" --path /path/to/project
```

### Install without registering a project

```bash
python3 ~/projects/ticket-takeaway/install.py
```

## File Deployment Map

| Source (in this repo) | Deployed Location | Purpose |
|----------------------|-------------------|---------|
| `src/tickets-cli.py` | `~/.claude/ticket-takeaway/tickets-cli.py` | CLI for all ticket CRUD |
| `src/generate.py` | `~/.claude/ticket-takeaway/generate.py` | Dashboard HTML generator |
| `src/generate.py` | `~/.claude/dashboard/generate.py` | Dashboard copy (DASHBOARD_DIR patched) |
| `src/static/*` | `~/.claude/ticket-takeaway/static/*` | PWA manifest, service worker, icons |
| `src/skills/ticket-takeaway/SKILL.md` | `~/.claude/skills/ticket-takeaway/SKILL.md` | `/dashboard` skill |
| `src/skills/review/SKILL.md` | `~/.claude/skills/review/SKILL.md` | `/review` skill |
| `src/skills/spec/SKILL.md` | `~/.claude/skills/spec/SKILL.md` | `/spec` skill |
| `src/skills/accept/SKILL.md` | `~/.claude/skills/accept/SKILL.md` | `/accept` skill |
| `src/skills/feedbacks/SKILL.md` | `~/.claude/skills/feedbacks/SKILL.md` | `/feedbacks` wrapper skill (superset of base feedbacks skill) |

Runtime data (not overwritten on upgrade):
| File | Purpose |
|------|---------|
| `~/.claude/ticket-takeaway/tickets.db` | SQLite database (source of truth) |
| `~/.claude/ticket-takeaway/registry.json` | Project registry |

## Per-Project Setup

### 1. Register the project

The installer does this automatically with `--register`. Or manually edit `~/.claude/ticket-takeaway/registry.json`:

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

### 2. Seed tickets (if you have a PRODUCT_BACKLOG.md)

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py seed --project myproject
```

This parses your existing `PRODUCT_BACKLOG.md` and imports all tickets into the SQLite DB. The installer runs this automatically.

### 3. Add backlog rules to the project's CLAUDE.md

Add this section to the project's `CLAUDE.md`:

```markdown
## Ticket Operations

Use the Ticket Takeaway CLI for ticket changes. PRODUCT_BACKLOG.md is auto-generated from SQLite — you can edit it directly and the CLI will absorb your changes, but the CLI is the preferred way.

CLI=~/.claude/ticket-takeaway/tickets-cli.py

- Start work: python3 $CLI move <project> <id> wip
- Blocked: python3 $CLI update <project> <id> --status blocked
- Code complete: python3 $CLI move <project> <id> review
- Accept: python3 $CLI accept <project> <id>
- New ticket: python3 $CLI add <project> "title"
- Dashboard: /dashboard
```

### 4. Create PRODUCT_SPECIFICATION.md (optional)

```markdown
# Product Specification — My Project

Accepted and shipped features.
```

### 5. Generate the dashboard

```bash
python3 ~/.claude/ticket-takeaway/generate.py
```

Or use the Claude Code skill: `/dashboard`

## Upgrading

```bash
cd ~/projects/ticket-takeaway
git pull
python3 install.py
```

The installer updates system files (CLI, generator, skills) but preserves your registry and database. If upgrading from v0.1.x (markdown-only), run `seed` to import existing tickets:

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py seed
```

## Verification

```bash
# List tickets
python3 ~/.claude/ticket-takeaway/tickets-cli.py list

# Generate dashboard
python3 ~/.claude/ticket-takeaway/generate.py

# Test the skill
/dashboard
```
