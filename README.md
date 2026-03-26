# Ticket Takeaway

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

> Markdown-native project board. Double-click. Paste. Build.

A lightweight process for people who work directly with their models. Your board is a markdown file. Double-click a ticket on the dashboard to copy a prompt, paste it into Claude Code, and build. Run as many windows as you want — the board is the coordination layer.

Two skills gate the process: **`/spec`** turns ideas into specced tickets. **`/review`** verifies completed work and handles acceptance. Between those gates, you build however you want.

For tasks that don't need your hands on the keyboard — security reviews, docs, compliance — we intend to make this compatible with agent orchestrators like [Paperclip](https://github.com/anthropics/claude-code/blob/main/AGENTS.md).

<img width="1507" alt="Ticket Takeaway dashboard rendered in a browser" src="https://github.com/user-attachments/assets/7a10b450-9f84-4c4b-9481-515d448cbe2f" />

## Stages and States

The board uses a **stage-and-state** model from Kanban methodology. **Stages** are the columns (defined by `## Section` headings in your markdown). **States** are the `Status:` values on tickets — finer detail within a stage. Same column, different next actions. If you've used JIRA or GitHub Projects, it's the same concept: column = lane, status = position within it.

| Stage (Column) | States within it | Meaning |
|----------------|-----------------|---------|
| **Ideas** | `proposed` | Unvetted — just a title or rough notion |
| **Backlog** | `proposed`, `specified`, `ready` | Being specced and queued for work |
| **WIP** | `in-progress`, `blocked` | Actively being built |
| **For Review** | `for-review`, `rework` | Code complete, awaiting sign-off |
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

**Stage change** = cut the `###` block, paste under a different `##` heading. **State change** = edit the `Status:` line.

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

## Install

### One-liner

```bash
git clone https://github.com/ytubecoder/ticket-takeaway.git ~/projects/ticket-takeaway && cd ~/projects/ticket-takeaway && mkdir -p ~/.claude/ticket-takeaway ~/.claude/skills/{ticket-takeaway,review,spec,accept} && cp src/generate.py ~/.claude/ticket-takeaway/generate.py && cp src/skills/ticket-takeaway/SKILL.md ~/.claude/skills/ticket-takeaway/SKILL.md && cp src/skills/review/SKILL.md ~/.claude/skills/review/SKILL.md && cp src/skills/spec/SKILL.md ~/.claude/skills/spec/SKILL.md && cp src/skills/accept/SKILL.md ~/.claude/skills/accept/SKILL.md && cp src/registry.example.json ~/.claude/ticket-takeaway/registry.json && echo "Done. Edit ~/.claude/ticket-takeaway/registry.json with your project paths, then run /dashboard."
```

### Or tell your agent

> Clone ticket-takeaway from https://github.com/ytubecoder/ticket-takeaway and install it. Follow the instructions in INSTALL.md.

### After install

1. Edit `~/.claude/ticket-takeaway/registry.json` — add your project's `id`, `name`, and `path`
2. Create a `PRODUCT_BACKLOG.md` in your project root (or run `/dashboard add {project} "First feature"`)
3. Add the [backlog rules](INSTALL.md#3-add-backlog-rules-to-the-projects-claudemd) to your project's `CLAUDE.md`
4. Run `/dashboard` to generate and open the board

Full deployment map and update instructions: [`INSTALL.md`](INSTALL.md)
