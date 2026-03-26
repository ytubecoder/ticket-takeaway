# Ticket Takeaway — Architecture & Philosophy

## What It Is

A **control panel for Claude Code** — a lightweight coordination layer that lets you manually manage multiple agents across multiple projects without leaving the CLI mindset. It respects the terminal-first workflow while solving the context-loss problem inherent to running parallel CLI sessions.

The core insight: when you're running 3 agents on different features across 2 projects, you need a single place to see "what's happening" and "what's next" — without building a heavy app that fights the CLI. The dashboard is that place.

---

## ASCII Architecture

```
 YOU (Human Operator)
  │
  │  "Stay close to the model"
  │  ┌─────────────────────────────────────────────────────────────────┐
  │  │                    CLAUDE CODE (CLI)                           │
  │  │                                                                 │
  │  │   Session A          Session B          Session C               │
  │  │   ┌──────────┐      ┌──────────┐      ┌──────────┐            │
  │  │   │ Agent on  │      │ Agent on  │      │ Agent on  │            │
  │  │   │ B-05:     │      │ B-03:     │      │ BUG-02:   │            │
  │  │   │ Frontend  │      │ API Layer │      │ Auth Fix  │            │
  │  │   └────┬─────┘      └────┬─────┘      └────┬─────┘            │
  │  │        │                  │                  │                   │
  │  │        │  /dashboard      │  /dashboard      │  /dashboard      │
  │  │        │  status proj     │  status proj     │  status proj     │
  │  │        │  B-05 wip        │  B-03 review     │  BUG-02 done     │
  │  │        │                  │                  │                   │
  │  └────────┼──────────────────┼──────────────────┼─────────────────┘
              │                  │                  │
              ▼                  ▼                  ▼
  ┌───────────────────────────────────────────────────────────────────┐
  │                     SKILLS LAYER                                  │
  │                                                                   │
  │   ~/.claude/skills/dashboard/SKILL.md    (5 modes)                │
  │   ┌─────────────────────────────────────────────────────────┐     │
  │   │  generate  │  status  │  accept  │  add  │  show        │     │
  │   └─────────────────────────────────────────────────────────┘     │
  │                                                                   │
  │   ~/.claude/skills/review/SKILL.md       (batched review)         │
  │   ┌─────────────────────────────────────────────────────────┐     │
  │   │  /review           │  /review {ID}                      │     │
  │   │  (walk all items)  │  (single item)                     │     │
  │   └─────────────────────────────────────────────────────────┘     │
  │                                                                   │
  │   Skills = instructions Claude reads & executes.                  │
  │   They tell Claude HOW to edit markdown and WHEN to regenerate.   │
  └──────────────────────────────┬────────────────────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
              ▼                  ▼                  ▼
  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
  │   Project A      │  │   Project B      │  │   Project C      │
  │                  │  │                  │  │                  │
  │ PRODUCT_         │  │ PRODUCT_         │  │ PRODUCT_         │
  │ BACKLOG.md       │  │ BACKLOG.md       │  │ BACKLOG.md       │
  │ ┌──────────────┐ │  │ ┌──────────────┐ │  │ ┌──────────────┐ │
  │ │ ## Ideas     │ │  │ │ ## Ideas     │ │  │ │ ## Ideas     │ │
  │ │ ## Backlog   │ │  │ │ ## Backlog   │ │  │ │ ## Backlog   │ │
  │ │ ## WIP    ◄──┼─┤  │ │ ## WIP       │ │  │ │ ## WIP       │ │
  │ │ ## Review    │ │  │ │ ## Review ◄──┼─┤  │ │ ## Review    │ │
  │ │ ## Done      │ │  │ │ ## Done      │ │  │ │ ## Done      │ │
  │ │ ## Bugs   ◄──┼─┼──┼─┼──────────────┼─┤  │ │ ## Bugs      │ │
  │ └──────────────┘ │  │ └──────────────┘ │  │ └──────────────┘ │
  │                  │  │                  │  │                  │
  │ PRODUCT_         │  │ PRODUCT_         │  │ PRODUCT_         │
  │ SPECIFICATION.md │  │ SPECIFICATION.md │  │ SPECIFICATION.md │
  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
           │                     │                     │
           └─────────────────────┼─────────────────────┘
                                 │
                                 ▼
  ┌───────────────────────────────────────────────────────────────────┐
  │                  REGISTRY + GENERATOR                             │
  │                                                                   │
  │   ~/.claude/dashboard/registry.json                               │
  │   ┌─────────────────────────────────────────────────────────┐     │
  │   │ { "projects": [                                         │     │
  │   │     { "id": "proj-a", "path": "~/projects/proj-a" },   │     │
  │   │     { "id": "proj-b", "path": "~/projects/proj-b" },   │     │
  │   │     { "id": "proj-c", "path": "~/projects/proj-c" }    │     │
  │   │ ] }                                                     │     │
  │   └─────────────────────────────────────────────────────────┘     │
  │                          │                                        │
  │                          ▼                                        │
  │   ~/.claude/dashboard/generate.py                                 │
  │   ┌─────────────────────────────────────────────────────────┐     │
  │   │  1. Read registry → find all projects                  │     │
  │   │  2. Parse each PRODUCT_BACKLOG.md → Ticket objects      │     │
  │   │  3. Parse PRODUCT_SPECIFICATION.md → Done items         │     │
  │   │  4. Collect git stats (commits, LOC, releases)          │     │
  │   │  5. Render self-contained HTML (all CSS/JS inline)      │     │
  │   │  6. Write → {project}/docs/sdlc-dashboard.html          │     │
  │   │  7. Open browser                                        │     │
  │   └─────────────────────────────────────────────────────────┘     │
  └──────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
  ┌───────────────────────────────────────────────────────────────────┐
  │                    DASHBOARD (Browser)                            │
  │                                                                   │
  │   ┌─ Header ──────────────────────────────────────────────────┐   │
  │   │  Ticket Takeaway  │ Total: 12  WIP: 3  Review: 2  Done: 5 │   │
  │   │  proj-a v2.1.0   │ ████████████░░░░ 62%  │ ▂▄█▆▃▅▇▄▂▅▃▆ │   │
  │   └───────────────────────────────────────────────────────────┘   │
  │   ┌─ Filter Bar (sticky) ────────────────────────────────────┐   │
  │   │  [All] [Backlog 4] [WIP 3] [Review 2] [Ideas 3]  🔍     │   │
  │   └───────────────────────────────────────────────────────────┘   │
  │   ┌─ Kanban ─────────────────────────────────────────────────┐   │
  │   │                                                           │   │
  │   │  Ideas    │ Backlog   │ WIP        │ For Review           │   │
  │   │  ┌──────┐ │ ┌──────┐  │ ┌──────┐   │ ┌──────┐            │   │
  │   │  │I-01  │ │ │B-07  │  │ │B-05  │   │ │B-03  │            │   │
  │   │  │idea..│ │ │ready │  │ │in-prg│   │ │review│            │   │
  │   │  └──────┘ │ └──────┘  │ └──────┘   │ └──────┘            │   │
  │   │  ┌──────┐ │ ┌──────┐  │ ┌──────┐   │                     │   │
  │   │  │I-02  │ │ │B-08  │  │ │B-06  │   │  <- double-click    │   │
  │   │  │idea..│ │ │spec'd│  │ │blockd│   │     copies work     │   │
  │   │  └──────┘ │ └──────┘  │ └──────┘   │     prompt to       │   │
  │   │           │           │            │     clipboard        │   │
  │   └───────────────────────────────────────────────────────────┘   │
  │   ┌─ Collapsed Sections ─────────────────────────────────────┐   │
  │   │  > Done (5)  │  > Bugs (2)  │  > Icebox (1)             │   │
  │   └───────────────────────────────────────────────────────────┘   │
  └───────────────────────────────────────────────────────────────────┘
```

---

## The Feedback Loop

```
  ┌────────────────────────────────────────────────────────────┐
  │                                                            │
  │    DASHBOARD                         CLI                   │
  │    (Browser)                    (Claude Code)              │
  │                                                            │
  │    ┌──────────┐   double-click   ┌──────────────┐         │
  │    │          │ ──── copy ─────> │ paste prompt  │         │
  │    │  See     │   work prompt    │ into new      │         │
  │    │  what's  │                  │ session       │         │
  │    │  next    │                  └──────┬───────┘         │
  │    │          │                         │                  │
  │    │          │                         v                  │
  │    │          │                  ┌──────────────┐         │
  │    │          │                  │ Agent works   │         │
  │    │          │                  │ on feature    │         │
  │    │          │                  └──────┬───────┘         │
  │    │          │                         │                  │
  │    │          │                         v                  │
  │    │          │   /dashboard     ┌──────────────┐         │
  │    │  Board   │ <── regenerate ─ │ /dashboard    │         │
  │    │  updates │                  │ status B-05   │         │
  │    │          │                  │ review        │         │
  │    └──────────┘                  └──────────────┘         │
  │         │                                                  │
  │         │          next card                               │
  │         └──────────────────────────────────────────────>   │
  │                                                            │
  └────────────────────────────────────────────────────────────┘
```

---

## How It Respects the CLI

The system is designed around a few deliberate constraints:

**1. Markdown is the database.**
No JSON state files, no SQLite, no sync daemons. The `PRODUCT_BACKLOG.md` in each project IS the board. Git handles versioning and conflict resolution. Any agent can read or edit it with standard tools.

**2. Skills are just instructions.**
The SKILL.md files don't contain executable code. They're markdown documents that tell Claude Code what to do: "read this file, find this section, move this block, then run the generator." Claude interprets them the same way it interprets any prompt -- staying close to the model.

**3. The generator is stateless.**
`generate.py` reads markdown, outputs HTML, exits. No running process, no server, no state to corrupt. Run it whenever you want a fresh view. It takes under a second.

**4. The dashboard is read-only output.**
The HTML file is a snapshot. It doesn't write back to the markdown. The only interactive feature is double-click-to-copy, which puts a work prompt on your clipboard -- bridging the visual board back to the CLI where the real work happens.

**5. No central coordinator.**
There's no orchestrator process deciding which agent works on what. You look at the board, pick a card, paste the prompt, and go. Multiple agents don't conflict because they work on different tickets and each ticket's state lives in a different `##` section of the markdown.

---

## The Multi-Agent Workflow

```
Morning:
  1.  /dashboard              -> browser opens, see all projects
  2.  double-click B-07       -> "I want to work on B-07: Auth Flow" copied
  3.  open new terminal       -> paste prompt, agent starts working
  4.  double-click B-08       -> "I want to work on B-08: Search" copied
  5.  open another terminal   -> paste prompt, second agent starts

During work:
  6.  Agent A finishes B-07   -> runs /dashboard status proj B-07 review
  7.  Agent B hits a blocker  -> updates Status: blocked in backlog
  8.  /dashboard              -> board refreshes, you see B-07 moved to Review,
                                 B-08 showing blocked indicator

Review:
  9.  /review proj            -> walks through For Review items in batches
  10. Accept B-07             -> moves to Done, summarized in PRODUCT_SPECIFICATION.md
  11. Feedback on B-03        -> bug sub-ticket created, status -> rework

End of day:
  12. /dashboard show         -> terminal summary of all projects, no browser needed
```

---

## Layer Summary

| Layer | What | Where | Role |
|-------|------|-------|------|
| **You** | Human operator | Terminal(s) | Pick work, review, accept |
| **Claude Code** | CLI sessions | Multiple terminals | Execute features, call skills |
| **Skills** | SKILL.md files | `~/.claude/skills/` | Instructions for how Claude edits state |
| **Markdown** | PRODUCT_BACKLOG.md | Each project root | Source of truth for all work items |
| **Generator** | generate.py | `~/.claude/dashboard/` | Stateless markdown -> HTML renderer |
| **Registry** | registry.json | `~/.claude/dashboard/` | Which projects to track |
| **Dashboard** | HTML file | `{project}/docs/` | Read-only visual snapshot |

---

## Key Design Principle

> The dashboard doesn't replace the CLI -- it complements it. You stay in the terminal doing real work. The dashboard is a glanceable map that answers "where am I across all my work?" and provides a bridge back to the CLI via copy-to-clipboard prompts. It's the minimum viable coordination layer for running multiple agents productively.
