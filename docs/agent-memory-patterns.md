# Agent Memory Patterns

Notes from reviewing Hermes Agent and comparing it to Ticket Takeaway's workflow.

## Position

Hermes is useful to study, but Ticket Takeaway should keep durable memory agent-agnostic.
Hermes can be an optional worker in Workflow Bounce, but it should not become the
canonical store for product decisions, review learnings, or project history.

## What To Borrow

- **Curated memory, not transcript hoarding.** Save compact facts, decisions, and reusable workflow lessons. Do not treat every session log as future prompt material.
- **Separate human memory from project memory.** User preferences and working style should follow the person across projects. Architecture, domain rules, ticket history, and repo-specific gotchas should stay scoped to the project.
- **Retrieval before injection.** Long-lived memory should be searchable and filtered by the active ticket, files, feature area, and current workflow step before being placed into a prompt.
- **Procedural memory as skills.** Repeatable workflows belong in skills or commands, not prose notes. If the lesson says "when doing X, run Y then verify Z", it is a candidate for a skill.
- **Session search as backup recall.** Raw session logs are useful for lookup and summarization, but should not be the main prompt context.
- **Explicit promotion path.** `/review`, `/sync`, and `/accept` should decide which working notes become durable memory, which become accepted feature history, and which are discarded.
- **Bounded always-on context.** Always-loaded files should stay small and stable. Large or volatile memory should be retrieved only when relevant.
- **Staleness and supersession.** Memories need a way to be replaced, invalidated, or tied to a feature/version so old lessons do not keep steering new work incorrectly.

## External Systems Reviewed

### Beads

Beads (`bd`) is best understood as a distributed, dependency-aware issue graph for
agents. It uses Dolt as the source of truth, gives agents JSON-first commands,
tracks blockers, supports `ready` work discovery, and has sync/merge semantics for
multi-agent or multi-machine work.

Useful patterns:

- `ready` query: show only unblocked tasks an agent can safely start.
- Atomic claim: prevent multiple agents from silently working the same task.
- Dependency/link graph: `blocks`, `parent-child`, `discovered-from`, `related`,
  `duplicates`, `supersedes`, and threaded messages.
- Auto-priming and sync hooks: inject a compact work summary at session start and
  sync before compaction/session end.
- Compaction: closed or stale work should decay into summaries rather than stay as
  full active context forever.
- Merge-aware storage: if Ticket Takeaway ever needs multi-branch/multi-agent
  concurrent editing, Beads' Dolt-backed model is a useful reference.

Fit for Ticket Takeaway:

Beads overlaps with Ticket Takeaway's ticket board more than it complements memory.
It is not a replacement for global/user memory, and adopting it wholesale would
replace a large part of Ticket Takeaway's current source-of-truth model. Borrow
its coordination patterns, not the product boundary.

### PLUR

PLUR is closest to a shared personal/agent memory layer. It stores learned
knowledge as YAML "engrams" plus timestamped "episodes", exposes MCP tools, and
injects relevant memories across Claude Code, Cursor, Windsurf, OpenClaw, Hermes,
and other MCP-compatible clients.

Useful patterns:

- Atomic engrams: one actionable learned fact per memory, not long narrative notes.
- Scope hierarchy: `global`, project, agent, command, or domain scopes.
- Memory types: behavioral, terminological, procedural, architectural.
- Polarity: distinguish "do this" from "do not do this."
- Contraindications: record when a memory should not apply.
- Activation and decay: useful memories strengthen; unused memories fade from
  injection without necessarily being deleted.
- Feedback loop: injected memories can receive positive, neutral, or negative
  feedback to improve future retrieval.
- Candidate promotion: raw observations should pass quality gates before becoming
  active memory.
- Episodes separate history from guidance: "what happened" is not the same as
  "what the agent should remember to do."
- YAML as source of truth with optional generated indexes.

Fit for Ticket Takeaway:

PLUR is an augment to local project files, not a replacement for them. It is a
strong candidate for cross-project human/practice memory. Ticket Takeaway can
borrow the engram schema ideas and can optionally read/write PLUR later, but the
ticket board and accepted product history should remain canonical inside the
project.

### ByteRover

ByteRover is closest to a project knowledge-base layer. It stores a local
`.brv/context-tree/` of markdown files, organizes knowledge by domain/topic,
generates summaries and abstracts, supports query/curate workflows, and can
optionally push to ByteRover cloud for team or multi-machine sync.

Useful patterns:

- Hierarchical context tree instead of one flat `MEMORY.md`.
- Auto-generated `context.md` files that define the purpose and boundary of each
  domain/topic.
- `_index.md` rollups: summaries propagate upward so queries can start broad and
  drill down.
- Abstract and overview siblings: store small and medium summaries next to larger
  knowledge files for token-efficient injection.
- Frontmatter scoring: importance, recency, maturity, access count, update count.
- Maturity lifecycle: draft -> validated -> core, with hysteresis to avoid churn.
- Archive stubs: stale low-value knowledge leaves a searchable "ghost cue" while
  the full content moves out of the active set.
- Tiered query path: exact/fuzzy cache, BM25 direct answer, LLM prefetch, then a
  slower agentic loop only when needed.
- Curation operations: add, update, upsert, merge, delete.
- Existing context ingestion: curate from `MEMORY.md`, docs folders, or internal
  files rather than forcing a big-bang migration.
- Review workflow and version control for memory changes.

Fit for Ticket Takeaway:

ByteRover is an augment or import/source layer for project knowledge. Its local
markdown tree model maps well to Ticket Takeaway's docs-first style, but adopting
it directly would introduce a second project-memory authority. Borrow the context
tree, scoring, summaries, archive stubs, and curation/review ideas. Treat
ByteRover integration as optional import/export/query, not the canonical store.

### ContextLattice

ContextLattice is closest to an ops-grade memory fabric and task orchestrator. It
exposes an HTTP-first local orchestrator, accepts memory writes through one ingress
contract, fans out to specialized sinks, performs federated retrieval, and uses
feedback/telemetry to improve retrieval quality.

Useful patterns:

- Single memory ingress: every write passes through one validated `/memory/write`
  contract before fanout.
- Compact writes only: save summaries, decisions, diffs, and outcomes rather than
  full transcripts.
- Durable outbox: memory writes should survive downstream store failures and retry
  fanout instead of losing the source event.
- Multi-sink fanout: raw ledger, fast search indexes, vector stores, rollups, and
  deeper archival stores each serve different retrieval needs.
- Staged retrieval: return fast sources first, then allow slower/deeper sources to
  complete asynchronously.
- Explicit retrieval lifecycle: callers can see whether a result is partial,
  running, succeeded, failed, or degraded.
- Feedback submission: retrieval results should receive outcome feedback so ranking
  improves over time.
- Degraded-memory mode: agents should continue working when memory is unavailable,
  but report that memory was degraded.
- Role/API-key split: separate admin/orchestrator operations from worker/agent
  operations.
- Retention and low-value cleanup: memory systems need storage pressure controls,
  not just unlimited accumulation.
- External runner routing: task queues can target Codex, Claude Code, OpenCode,
  Hermes, or internal workers by agent id.

Fit for Ticket Takeaway:

ContextLattice is not a replacement for the ticket-visible Learnings section. It is
a candidate backend/fanout layer after Ticket Takeaway captures a learning item.
The right integration would be event-driven:

1. Ticket Learnings remains the human-visible staging and correction surface.
2. Confirmed learning items emit compact memory events with stable ids.
3. A local memory orchestrator can index/fan out those events to project/global
   memory stores.
4. Ticket Takeaway keeps the canonical source link and promotion state.
5. If the external memory fabric is unavailable, Ticket Takeaway still works and
   records that external memory sync is degraded.

Borrow its ingestion, durability, lifecycle, feedback, and degraded-mode patterns.
Do not adopt its operational footprint as a prerequisite for Ticket Takeaway's core
workflow.

## Memory Taxonomy

Ticket Takeaway already has memory-like artifacts. The distinction is not whether a
file is called `MEMORY.md`; it is how the information is governed and retrieved.

| Layer | Scope | Examples | Best Use | Risk |
| --- | --- | --- | --- | --- |
| Agent instructions | Tool/project | `CLAUDE.md`, `AGENTS.md` | Stable operating rules, commands, repo conventions | Too broad; always consumes context |
| Human practice memory | User/global | preferred review style, UI taste, workflow defaults | Cross-project consistency | Can overfit unrelated projects |
| Project memory | Project | architecture decisions, recurring bugs, local gotchas | Relevant context for future tickets | Needs retrieval and pruning |
| Ticket state | Ticket/project | description, criteria, status, readiness flags | Current workflow coordination | Should not become long-term clutter |
| Feature working notes | Ticket/ephemeral | `docs/features/{ID}/NOTES.md`, `TESTS.md`, `REVIEW.md` | In-progress investigation | Must be promoted or deleted on accept |
| Accepted product history | Project/permanent | `PRODUCT_SPECIFICATION.md` | Shipped behavior and decisions | Poor at fine-grained retrieval |
| Session logs | Agent/session | `.claude/SESSION_LOG.md`, chat transcripts | Audit trail and search fallback | Noisy if injected directly |
| Skills | Global or project | `/review`, `/accept`, repo-specific workflows | Reusable procedure | Can become stale if not tested |

## Why A Separate Project Memory Layer

`CLAUDE.md` and `AGENTS.md` are memory in the broad sense, but they are **instruction
memory**: static, always-on, and written for the agent before it knows the active
ticket. They are good for rules like "use the Ticket Takeaway CLI for writes" and
"run `/sync` before acceptance cleanup."

Project memory is different. It should be **retrieved memory**: source-linked,
queryable, scoped to the current ticket or files, and compact enough to inject only
when useful. That lets the system remember facts like:

- "The dashboard must remain usable over `file://`; server edit features are progressive enhancement."
- "Avoid native `alert()` and `confirm()`; use the custom modal/toast patterns."
- "When changing scenario runner behavior, update both `tests/scenario_runner.py` and `run_journey.py`."

Those are too specific and numerous for `CLAUDE.md`, but too durable to leave buried
in session logs.

## Candidate Shape For Ticket Takeaway

- Add a memory store with records for `scope` (`global`, `project`, `ticket`, `file`),
  `project_id`, `source_ticket`, `source_path`, `tags`, `fact`, `created_at`, and
  `superseded_by`.
- Index memory with FTS so prompts can pull a small set of relevant records for the
  active ticket and touched files.
- Add promotion points to `/sync`, `/review`, and `/accept`:
  - durable product behavior goes to `PRODUCT_SPECIFICATION.md`
  - reusable project lessons go to project memory
  - reusable procedure goes to a skill
  - noisy investigation remains in session logs or gets deleted
- Add global/user memory for cross-project practice, but keep it separate from project
  memory and visible to the user.
- Keep `CLAUDE.md`/`AGENTS.md` as the small always-on operating manual. Memory should
  not turn those files into a dumping ground.

## Ticket-Visible Learning Loop

Ticket Takeaway's memory loop should be visible to the human while work is still
active. Per-ticket learnings should appear in the ticket's **Learnings** section
as they are discovered, not only at closure.

The Learnings section should act as a reviewable staging area:

- Agents append candidate learnings during planning, implementation, testing,
  feedback capture, and review.
- The human can correct, update, add, reject, or delete learnings directly on the
  ticket while the context is fresh.
- The ticket view should make it clear which learnings are proposed by an agent,
  confirmed by the human, rejected, or already promoted.
- Promoted project/global memories should remain linked back to the source ticket
  and, when possible, to the source file or review artifact that produced them.
- Closing or accepting a ticket should finalize the staging area: keep accepted
  product behavior in `PRODUCT_SPECIFICATION.md`, promote reusable lessons into
  project/global memory, and discard rejected or purely ephemeral notes.

This means memory is **effective before closure**, but closure is the point where
the system stops expecting active human review and performs final promotion,
summarization, and cleanup.

Implementation implication: avoid a single freeform blob if possible. The UI can
still render as a readable Learnings section, but the data model should be able to
track individual learning items with status, scope, source, and promotion state.
That makes human review practical and prevents one corrected item from requiring a
rewrite of the whole section.

## Recommended Direction

Use a layered model:

1. **Always-on instruction files** stay small: `CLAUDE.md`, `AGENTS.md`, and skill
   instructions define operating rules.
2. **Ticket Takeaway remains canonical** for ticket state, accepted product history,
   and project-specific review learnings.
3. **Project memory becomes retrieved context**: compact records, source-linked,
   searchable, scoped by ticket/files/domains, and promoted through `/sync`,
   `/review`, and `/accept`.
4. **Global/user memory is separate**: preferences, communication style, recurring
   workflow defaults, and personal practices that should follow the human across
   projects.
5. **External memory tools are optional adapters**:
   - Beads can inform coordination and dependency graph features.
   - PLUR can provide shared global/user memory across agents.
   - ByteRover can inform or augment project knowledge trees and markdown ingestion.
   - ContextLattice can provide memory event fanout, federated retrieval, and
     degraded-mode orchestration for heavier local deployments.

The product boundary should be: local files and Ticket Takeaway DB are the
inspectable source of truth; shared memory systems may index, retrieve, mirror, or
sync selected knowledge, but should not silently become the only place important
project decisions live.
