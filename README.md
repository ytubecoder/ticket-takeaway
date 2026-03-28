# Ticket Takeaway

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: Source Available](https://img.shields.io/badge/license-Source%20Available-yellow)
![GitHub release](https://img.shields.io/github/v/release/ytubecoder/ticket-takeaway)
![Works with Claude Code](https://img.shields.io/badge/works%20with-Claude%20Code-blueviolet)
![Works with Codex CLI](https://img.shields.io/badge/works%20with-Codex%20CLI-orange)

```
                         _______________________________________________
                        /                                              /|
                       /  TICKET TAKEAWAY                             / |
                      /    ═══════════════                           /  |
                     /     ☐ grab  ☐ paste  ☐ build  ☐ ship        /   |
                    /_______________________________________________/    |
                    |                                               |    |       /spec
     ___            |  ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐     |    |      ───▶
    /   \    .--.   |  │ IDEAS │ │BACKLOG│ │  WIP  │ │REVIEW │     |   /      build
   | o o |  |    |  |  │ ░░░░  │ │ ░░░░░ │ │ ▓▓▓▓▓ │ │ ████  │     |  /      ───▶
    \ _ /   |    |  |  │ ░░    │ │ ░░░   │ │ ▓▓▓   │ │       │     | /      /review
     |||    '----'  |  └───────┘ └───────┘ └───────┘ └───────┘     |/      ───▶
    /   \           |_______________________________________________|      /accept
```

> SQLite-backed project board. Double-click. Paste. Build.

A lightweight process for people who work directly with their models to create software. Your kanban board is backed by SQLite and auto-generates markdown files. Double-click a ticket on the dashboard to copy a prompt, paste it into Claude Code, the prompt will take into account the status and aim to do the next step to keep your feature flowing. Run as many windows as you want — the board is the coordination layer.

Agents can edit PRODUCT_BACKLOG.md directly — the CLI picks up changes via read-before-write sync. No data loss either way.

Two skills gate the process: **`/spec`** turns ideas into specced tickets. **`/review`** verifies completed work and handles acceptance. Between those gates, you build however you want.

For tasks that don't need your hands on the keyboard — security reviews, docs, compliance — we intend to make this compatible with agent orchestrators like [Paperclip](https://github.com/paperclipai/paperclip).

<img width="1507" alt="Ticket Takeaway dashboard rendered in a browser" src="https://github.com/user-attachments/assets/7a10b450-9f84-4c4b-9481-515d448cbe2f" />

## Stages and States

The board uses a **stage-and-state** model from Kanban methodology. **Stages** are the columns (defined by `## Section` headings in your markdown). **States** are the `Status:` values on tickets — finer detail within a stage. Same column, different next actions. If you've used JIRA or GitHub Projects, it's the same concept: column = lane, status = position within it.

| Stage (Column) | States within it | Meaning |
|----------------|-----------------|---------|
| **Ideas** | `proposed` | Unvetted — just a title or rough notion |
| **Backlog** | `proposed`, `specified`, `ready` | Being specced and queued for work |
| **WIP** | `in-progress`, `blocked`, `rework` | Actively being built |
| **For Review** | `for-review` | Code complete, awaiting sign-off |
| **Done** | `done`, `released` | Accepted or shipped |

Side lanes: **Bugs** (`bug`, `bug-fixed`), **Icebox** (`icebox`), **Won't Do** (`wont-do`) — reachable from any stage.

## How a Ticket Progresses

```
  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
  │  IDEAS   │───▶│ BACKLOG  │───▶│   WIP    │───▶│  REVIEW  │───▶│   DONE   │
  └──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘
     /spec           ready?        double-click      /review       /dashboard
   specifies       unblocked?      & build          verifies        accept
```

| Step | What happens | Gate |
|------|-------------|------|
| **1. Idea** | Add a ticket to `## Ideas`. Needs only an ID and title. | — |
| **2. Spec** | Run `/spec` — writes description + acceptance criteria. Moves to Backlog. | Description + at least one `- [ ]` criterion |
| **3. Ready** | Dependencies met, criteria are actionable. Status → `ready`. | Nothing blocking the start of work |
| **4. Build** | Double-click the card, paste into Claude Code. Ticket moves to `## WIP`. | Ticket is `ready` |
| **5. Complete** | All criteria addressed. Ticket moves to `## For Review`. | Implementation covers every criterion |
| **6. Review** | Run `/review` — walks through criteria, creates bug sub-tickets if needed. | All criteria verified |
| **7. Accept** | `/accept` moves ticket to `PRODUCT_SPECIFICATION.md`. | Review passed |

**Stage change** = `tickets-cli.py move <project> <id> <section>`. **State change** = `tickets-cli.py update <project> <id> --status <status>`. Or just edit the markdown — the CLI absorbs direct edits.

## `/spec` — Ideas to Backlog

Run `/spec` to walk through all ideas, or `/spec {ID}` for a specific one. The skill:

1. Reads `## Ideas` (and any `proposed` tickets in Backlog)
2. Helps you write a description and acceptance criteria for each
3. Suggests test cases (non-mandatory — use `/tdd` later if you want full specs)
4. Sets priority and complexity
5. Moves the ticket to `## Backlog` with status `specified`

Double-clicking an idea card on the dashboard copies `/spec {ID}` to your clipboard.

## `/review` — For Review to Done

Run `/review` to walk through all completed work, or `/review {ID}` for one ticket. The skill:

1. Batches related tickets and presents them oldest-first
2. Verifies each acceptance criterion (use Chrome DevTools MCP tools to inspect the running app)
3. Creates `BUG-` sub-tickets from feedback, linked to the parent via `Parent:` field
4. Bugs get fixed and verified before the parent can be re-reviewed
5. On acceptance: `/accept` moves the ticket to `PRODUCT_SPECIFICATION.md`, summarizes development notes, and cleans up working files

Double-clicking the For Review column header copies `/review` to your clipboard.

**Compatible with:** [Claude Code](https://claude.ai/code) · [Codex CLI](https://github.com/openai/codex) · Any AI coding agent that reads markdown

## Install

### One-liner (from your project directory)

```bash
git clone https://github.com/ytubecoder/ticket-takeaway.git ~/projects/ticket-takeaway && python3 ~/projects/ticket-takeaway/install.py --register
```

This installs the CLI, generator, and skills, registers your project, and seeds the DB from your existing `PRODUCT_BACKLOG.md` (if you have one).

### Or tell your agent

> Clone https://github.com/ytubecoder/ticket-takeaway to ~/projects/ticket-takeaway and run `python3 ~/projects/ticket-takeaway/install.py --register`. This will install the Ticket Takeaway dashboard system and register this project. If we have a PRODUCT_BACKLOG.md it will import existing tickets into the SQLite database automatically.

### Upgrade

```bash
cd ~/projects/ticket-takeaway && git pull && python3 install.py
```

The installer copies the latest CLI, generator, and skills. The registry and DB are preserved — only system files are updated. If upgrading from the markdown-only version (v0.1.x), the `seed` step runs automatically and imports your existing tickets.

### After install

1. Run `/dashboard` to generate and open the board
2. Add tickets: `python3 ~/.claude/ticket-takeaway/tickets-cli.py add <project> "First feature"`
3. Or just add `### B-01: My Feature` to `PRODUCT_BACKLOG.md` — the CLI will pick it up

Full deployment map: [`INSTALL.md`](INSTALL.md)
