"""Seeds 5 default system=1 workflows that replicate current Kitchen behaviour,
plus the Consultant agent and Plan Check workflow.

Call ``seed_default_agents(db)`` once at server startup (idempotent, global).
Call ``seed_default_workflows(db, project_id)`` per-project at startup (idempotent).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Prompt template helpers
# ---------------------------------------------------------------------------

def _ticket_prompt(action: str) -> str:
    return (
        f"{action} ticket {{{{ticket.id}}}}: {{{{ticket.title}}}}\n\n"
        "{{ticket.description}}\n\n"
        "Acceptance criteria:\n{{ticket.acceptance_criteria}}"
    )


# ---------------------------------------------------------------------------
# Endpoint dataclass + seed data
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    """Seed-time representation of an endpoints table row.

    Mirrors the SQL columns in migration 19. JSON-shaped fields (args,
    capabilities, session_config) are held as Python types here and
    json.dumps()'d at upsert time.
    """
    id: str
    name: str
    endpoint_type: str = "cli"
    command: Optional[str] = None
    args: list = field(default_factory=list)
    prompt_mode: str = "template"
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    timeout_s: int = 120
    capabilities: dict = field(default_factory=dict)
    session_config: dict = field(default_factory=dict)
    system: int = 0


DEFAULT_ENDPOINTS: list[Endpoint] = [
    Endpoint(
        id="claude-cli",
        name="Claude CLI",
        endpoint_type="cli",
        system=1,
        command="claude",
        args=["-p", "{prompt}", "--output-format", "json"],
        prompt_mode="template",
        capabilities={"sessions": True},
        session_config={
            "resume_args": ["-p", "{prompt}", "--output-format", "json",
                            "--resume", "{session_id}"],
            "session_id_regex": r'"session_id"\s*:\s*"([0-9a-f-]+)"',
        },
    ),
    Endpoint(
        id="codex-cli",
        name="Codex CLI",
        endpoint_type="cli",
        system=1,
        command="codex",
        args=["{prompt}"],
        prompt_mode="template",
        capabilities={"sessions": True},
        session_config={
            "resume_args": ["exec", "resume", "{session_id}"],
            "session_id_regex": r"Session(?:\s+ID)?\s*:\s*([0-9a-f-]+)",
            "session_id_fallback_dir": "~/.codex/sessions/",
        },
    ),
    Endpoint(
        id="codex-exec-readonly",
        name="Codex exec (read-only)",
        endpoint_type="cli",
        system=1,
        command="codex",
        args=["exec", "-s", "read-only", "{prompt}"],
        prompt_mode="template",
        capabilities={"sessions": False},
    ),
    Endpoint(
        id="hermes-cli",
        name="Hermes CLI",
        endpoint_type="cli",
        system=1,
        command="hermes",
        args=["chat", "-q", "{prompt}"],
        prompt_mode="template",
        capabilities={"sessions": False},
    ),
]

# Maps legacy (command, raw_args_tuple) -> canonical endpoint id.
# Used by migration #19 to pin known system runtimes to seeded ids
# instead of synthesising duplicate endpoints. raw_args_tuple is the
# value stored in workflow_agents.args BEFORE _build_agent_cmd's
# runner-side flag injection.
KNOWN_CLI_MAPPINGS: dict[tuple, str] = {
    ("claude", ()): "claude-cli",
    ("codex", ()): "codex-cli",
    ("codex", ("exec", "-s", "read-only")): "codex-exec-readonly",
    ("hermes", ()): "hermes-cli",
    ("hermes", ("chat",)): "hermes-cli",
}


# ---------------------------------------------------------------------------
# Default agents (global — no project_id)
# ---------------------------------------------------------------------------

DEFAULT_AGENTS: list[dict] = [
    # Planner: Claude. Drafts the plan in step 1, mediates Codex in step 3.
    # persist_session lets step 3 resume step 1's session — Planner remembers
    # its own plan when synthesizing the Consultant's review.
    {
        "id": "agent_planner",
        "name": "Planner",
        "endpoint_id": "claude-cli",
        "command": "claude",
        "args": "-p",
        "system_prompt": "",   # /plan-check sets none — templates carry the instructions
        "persist_session": 1,
        "system": 1,
    },
    # Consultant: Codex, sandboxed read-only (mirrors `/plan-check` exactly).
    {
        "id": "agent_consultant",
        "name": "Consultant",
        "endpoint_id": "codex-exec-readonly",
        "command": "codex",
        "args": "exec -s read-only",
        "system_prompt": "",
        "persist_session": 1,
        "system": 1,
    },
    # Orchestrator: system-flagged. Multi-turn interview agent that uses
    # interactive markers to gather ticket context from the user, then proposes
    # a structured patch (description + criteria + tags) via the 'propose'
    # marker for atomic accept/decline.
    {
        "id": "agent_orchestrator",
        "name": "Orchestrator",
        "endpoint_id": "claude-cli",
        "command": "claude",
        "args": "-p",
        "system_prompt": (
            "You are an Orchestrator agent for Ticket Takeaway. Your job is to interview "
            "the user to gather enough context to write a clear ticket description and at "
            "least one acceptance criterion.\n\n"
            "Use the following interactive markers in your stdout output to drive the UI:\n\n"
            "  1. Ask a question (free-text reply):\n"
            '     {"ask": "<your question>", "context": "<optional brief context>"}\n\n'
            "  2. Propose a structured patch for the user to accept/decline:\n"
            '     {"propose": {"description": "<proposed description>", '
            '"add_criteria": ["<criterion 1>", ...], '
            '"remove_criteria": [], '
            '"add_tags": ["<tag>", ...], '
            '"remove_tags": []}}\n\n'
            "Rules:\n"
            "- Each marker must appear on its own line as valid JSON.\n"
            "- Emit at most one marker per turn.\n"
            "- Start by asking 1-2 focused questions to understand the ticket goal.\n"
            "- After 1-3 rounds of questions, emit a 'propose' marker with your best "
            "draft. The user will accept/decline individual fields.\n"
            "- When the user accepts a proposal, you are done. Do not emit another marker.\n"
            "- Keep your prose responses concise — the ticket record is the output, not the chat.\n"
        ),
        "persist_session": 1,
        "system": 1,
    },
    # Worker: system-flagged. Single-turn implementation agent. Returns a
    # structured handoff JSON as its final stdout line so the runner can parse
    # implemented/undone/commands/issues/procedures_followed.
    {
        "id": "agent_worker",
        "name": "Worker",
        "endpoint_id": "claude-cli",
        "command": "claude",
        "args": "-p",
        "system_prompt": (
            "You are a Worker agent for Ticket Takeaway. Implement the ticket "
            "described in your prompt fully and autonomously.\n\n"
            "When you finish, output a structured handoff JSON object as the LAST "
            "line of your stdout. The object must be valid JSON on a single line with "
            "at least these keys (use empty arrays if nothing applies):\n\n"
            '{"implemented": ["<what was done>", ...], '
            '"undone": ["<what was skipped and why>", ...], '
            '"commands": [{"cmd": "<shell command>", "exit_code": 0}, ...], '
            '"issues": ["<problem encountered>", ...], '
            '"procedures_followed": ["<checklist item>", ...]}\n\n'
            "Do not add any text after the JSON line. The runner parses that line "
            "automatically — do not wrap it in backticks or code fences."
        ),
        "persist_session": 0,
        "system": 1,
    },
    # Summarizer: system-flagged. Single-turn one-liner generator. Reads a
    # ticket's title, description, criteria progress, status, section, and
    # child summary, and emits exactly one short sentence describing where
    # the work is up to. Used by the "Refresh ticket summary" system workflow
    # so the ticket detail overlay can show a Claude-Code-style status line
    # without paying the LLM cost at view time.
    {
        "id": "agent_summarizer",
        "name": "Summarizer",
        "endpoint_id": "claude-cli",
        "command": "claude",
        "args": "-p",
        "system_prompt": (
            "You are a Summarizer agent for Ticket Takeaway. You produce ONE short "
            "sentence describing where a ticket is up to right now — the tone of "
            "the brief idle summaries Claude Code shows after long pauses.\n\n"
            "Rules:\n"
            "- Output exactly one sentence. No preamble, no JSON, no markdown.\n"
            "- Aim for 8–20 words. Hard cap at 280 characters.\n"
            "- Present tense, third-person, declarative. No questions.\n"
            "- Lead with the work, not the metadata. Mention status/section only "
            "  when it adds information (e.g. 'blocked on …', 'in review with one "
            "  open bug').\n"
            "- If acceptance criteria progress is meaningful (e.g. 3 of 5 checked), "
            "  weave it in naturally rather than as a fraction dump.\n"
            "- Skip filler like 'this ticket' or 'the work involves'."
        ),
        "persist_session": 0,
        "system": 1,
    },
    # Validator: system-flagged. Adversarial acceptance-criteria checker. Ignores
    # the implementation and evaluates whether each criterion is satisfied from the
    # outside. Emits a 'propose' marker with criteria checks for user confirmation.
    {
        "id": "agent_validator",
        "name": "Validator",
        "endpoint_id": "claude-cli",
        "command": "claude",
        "args": "-p",
        "system_prompt": (
            "You are a Validator agent for Ticket Takeaway. Your role is adversarial: "
            "re-read the ticket's acceptance criteria and decide, from outside the "
            "implementation, whether each one is satisfied.\n\n"
            "Do NOT review the implementation code directly. Instead:\n"
            "1. Read each acceptance criterion.\n"
            "2. Determine if it is verifiably met based on observable behaviour or "
            "evidence in the ticket record.\n"
            "3. Emit a 'propose' marker summarising your verdict as criteria checks. "
            "List passing criteria in 'add_criteria' with a tick prefix, failing "
            "criteria in 'remove_criteria' with a cross prefix, and leave 'description', "
            "'add_tags', 'remove_tags' empty unless you have a specific recommendation.\n\n"
            "Interactive marker format (emit on its own line as valid JSON):\n"
            '{"propose": {"description": "", "add_criteria": ["✓ <criterion>", ...], '
            '"remove_criteria": ["✗ <failing criterion>", ...], '
            '"add_tags": [], "remove_tags": []}}\n\n'
            "Be conservative: only mark a criterion satisfied when you have clear evidence. "
            "When in doubt, mark it as failing and explain in parentheses."
        ),
        "persist_session": 0,
        "system": 1,
    },
]


# ---------------------------------------------------------------------------
# Plan Check prompt templates
# ---------------------------------------------------------------------------

# Step 1 — Planner (Claude) drafts an implementation plan from ticket context.
# This mirrors `/plan-check`'s assumed precondition: a plan already exists in
# Claude's context. Here we make Claude produce one, with the ticket as input,
# so the Planner is never cold when it later mediates the Consultant's review.
INITIAL_PLAN_TEMPLATE = """\
You are the Planner for ticket {{ticket.id}}. Draft a concrete implementation
plan that another reviewer can critique. Cover: approach, sequencing, risks,
dependencies, edge cases, and how each acceptance criterion will be satisfied.

# Ticket {{ticket.id}}: {{ticket.title}}

{{ticket.description}}

## Acceptance criteria
{{ticket.acceptance_criteria}}

After this plan, a Consultant will review it for completeness and risks. You
will then mediate their feedback. Write the plan as if you'll defend it.\
"""

# Step 2 — Consultant (Codex, read-only) reviews the Planner's plan.
# Verbatim from `/plan-check` Round 1, but $PLAN_CONTENTS is the Planner's
# previous turn (not raw ticket fields).
CONSULTANT_REVIEW_TEMPLATE = """\
Review the following plan for completeness, risks, and feasibility.

Assess:
- Missing requirements or edge cases
- Technical risks or blockers
- Dependencies that aren't accounted for
- Sequencing issues
- Performance or operational concerns
- Anything ambiguous or underspecified

Be specific — reference sections by name.
Flag severity: critical (blocks implementation), warning (likely problem), or note (suggestion).

Plan to review:

{{conversation.last_agent_response}}\
"""

# Step 3 — Planner (Claude, RESUMED from step 1) synthesizes the Consultant's
# findings against its own plan. This is the mediator step `/plan-check`
# describes ("Claude does NOT just pass the output through — Claude acts as an
# informed mediator"). Because session is resumed, the Planner remembers its
# original plan and can compare against it.
MEDIATION_SYNTHESIS_TEMPLATE = """\
The Consultant has reviewed your plan. Synthesize their feedback against what
you originally proposed.

For each finding, classify it:
- AGREE — the point is valid, plan should change. State what changes.
- PARTIAL — there's merit but severity is off, or it's mitigated. Explain.
- DISAGREE — Consultant is wrong, misunderstood, or over-cautious. Push back
  with reasons drawn from your context.

Then produce:
- An updated plan reflecting accepted changes
- A list of unresolved items the user must decide on
- A final verdict: READY / NEEDS REVISION / NEEDS USER INPUT

Consultant's review:

{{conversation.last_agent_response}}\
"""


# ---------------------------------------------------------------------------
# Default workflow definitions
# ---------------------------------------------------------------------------

DEFAULT_WORKFLOWS: list[dict] = [
    # 1. Spec → Backlog: Ideas ticket → Orchestrator interview → promote to Backlog.
    #    The Orchestrator uses interactive markers (ask/propose) to gather context
    #    from the user. The run stays in needs_input until the user accepts a proposal.
    #    On success: ticket moves to Backlog with the filled description + criteria.
    {
        "name": "Spec → Backlog",
        "description": (
            "Interview the user via the Orchestrator agent to fill in description and "
            "acceptance criteria for an Ideas ticket, then promote it to Backlog."
        ),
        "system": 1,
        "enabled": 1,
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "section_equals", "value": "Ideas"},
                {"kind": "automation_mode", "value": "auto"},
                {"kind": "no_active_run"},
            ]
        },
        "on_success_json": {"move_to": "Backlog"},
        "steps": [
            {
                "agent_id": "agent_orchestrator",
                "agent_name": "Orchestrator",
                "prompt_template": (
                    "You are starting an interview for ticket {{ticket.id}}: {{ticket.title}}\n\n"
                    "Current description:\n{{ticket.description}}\n\n"
                    "Current acceptance criteria:\n{{ticket.acceptance_criteria}}\n\n"
                    "Interview the user to clarify the goal and produce a crisp description "
                    "with at least one concrete acceptance criterion. Use the interactive "
                    "markers (ask/propose) as instructed in your system prompt."
                ),
                "on_failure": "pause",
                "timeout_ms": 900000,  # 15 min — multi-turn interview
            }
        ],
    },
    # 2. Backlog → WIP: Worker runs the implementation and emits a handoff JSON.
    #    The runner parses the handoff automatically (runners._try_parse_handoff).
    {
        "name": "Backlog → WIP",
        "description": "Auto-dispatch Backlog tickets to the Worker agent; captures structured handoff on completion",
        "system": 1,
        "enabled": 1,
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "section_equals", "value": "Backlog"},
                {"kind": "automation_mode", "value": "auto"},
                {"kind": "has_field", "field": "description"},
                {"kind": "criteria_count_gte", "value": 1},
                {"kind": "deps_clear"},
                {"kind": "no_active_run"},
            ]
        },
        "on_success_json": {"move_to": "WIP", "set_status": "in-progress"},
        "steps": [
            {
                "agent_id": "agent_worker",
                "agent_name": "Worker",
                "prompt_template": (
                    _ticket_prompt("Implement") + "\n\n"
                    "When done, output your structured handoff JSON as the LAST line of stdout "
                    "as instructed in your system prompt. No text after the JSON."
                ),
                "on_failure": "pause",
                "timeout_ms": 600000,
            }
        ],
    },
    # 3. WIP → Review: Worker reviews/validates then emits handoff. Handoff is
    #    automatically captured by the runner into runs.metadata_json.handoff.
    {
        "name": "WIP → Review",
        "description": "Move WIP tickets to For Review once implementation has a commit; captures handoff",
        "system": 1,
        "enabled": 1,
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "section_equals", "value": "WIP"},
                {"kind": "status_equals", "value": "in-progress"},
                {"kind": "has_field", "field": "commit_hash"},
                # The per-ticket automation toggle is the master switch for
                # every system workflow that mutates a ticket. Without this,
                # a user who toggled automation off on a specific ticket would
                # still get auto-moved on commit — surprising behaviour.
                {"kind": "automation_mode", "value": "auto"},
                # TODO: add "tests_pass" condition once a test-runner integration exists
                {"kind": "no_active_run"},
            ]
        },
        "on_success_json": {"move_to": "For Review", "set_status": "for-review"},
        "steps": [
            {
                "agent_id": "agent_worker",
                "agent_name": "Worker",
                "prompt_template": (
                    _ticket_prompt("Review the implementation for") + "\n\n"
                    "When done, output your structured handoff JSON as the LAST line of stdout "
                    "as instructed in your system prompt."
                ),
                "on_failure": "pause",
                "timeout_ms": 300000,
            }
        ],
    },
    # 4a. Parent auto-promote: when ALL children of a parent reach a terminal status
    #     (done / for-review / bug-fixed), promote the parent to For Review.
    #     Like every other section-mutating system workflow, gated by the
    #     parent ticket's per-ticket automation toggle so the user's master
    #     switch is honoured uniformly. Zero steps → applied via NoopRunner.
    {
        "name": "Parent auto-promote",
        "description": (
            "When all children of a parent ticket reach terminal status "
            "(done / for-review / bug-fixed), promote the parent to For Review. "
            "Replaces the legacy hardcoded _maybe_promote_parent hook."
        ),
        "system": 1,
        "enabled": 1,  # Preserves today's behaviour
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                # Only consider tickets that actually have children.
                {"kind": "has_children"},
                # Don't move parents that are already terminal.
                {"kind": "section_in", "values": ["Ideas", "Backlog", "WIP"]},
                # Every child must be done / for-review / bug-fixed.
                {"kind": "children_all_status_in",
                 "value": ["done", "for-review", "bug-fixed"]},
                # Honour the per-ticket automation toggle uniformly.
                {"kind": "automation_mode", "value": "auto"},
            ]
        },
        "on_success_json": {
            "move_section": "For Review",
        },
        "steps": [],  # Pure mutation — no agent step.
    },
    # 4b. Auto-accept: move tickets in For Review with status 'done' and no
    #     open child bugs to Done. DISABLED by default — never auto-accept
    #     without explicit user approval (memory: feedback_no_accept_without_user).
    {
        "name": "Auto-accept reviewed tickets",
        "description": (
            "Move tickets in For Review with status 'done' and no open child bugs "
            "to Done. Disabled by default — enable only if you want acceptance "
            "to happen without manual approval."
        ),
        "system": 1,
        "enabled": 0,
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "section_equals", "value": "For Review"},
                {"kind": "status_equals", "value": "done"},
                {"kind": "children_no_open_bugs"},
                # Honour the per-ticket automation toggle uniformly.
                {"kind": "automation_mode", "value": "auto"},
            ]
        },
        "on_success_json": {
            "accept_ticket": True,
        },
        "steps": [],  # Pure mutation — no agent step.
    },
    # 5. Bug triage: orphan bugs (no parent) in Bugs section — sets a parent
    {
        "name": "Bug triage",
        "description": "Triage orphan bugs in the Bugs backlog and link them to a parent ticket",
        "system": 1,
        "enabled": 1,
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "section_equals", "value": "Bugs"},
                {"kind": "parent_done"},   # parent_done passes when parent IS NULL
                {"kind": "automation_mode", "value": "auto"},
                {"kind": "no_active_run"},
            ]
        },
        "on_success_json": {},  # agent sets parent — no automatic section move
        "steps": [
            {
                "agent_id": None,
                "agent_name": "triager",
                "prompt_template": (
                    "Triage bug {{ticket.id}}: {{ticket.title}}\n\n"
                    "{{ticket.description}}\n\n"
                    "Find the most appropriate parent ticket and link this bug to it."
                ),
                "on_failure": "pause",
                "timeout_ms": 180000,
            }
        ],
    },
    # 6. Done → Learnings extraction: Worker summarises ticket history into the L flag.
    #    Disabled by default — opt-in like Auto-accept. When enabled, fires on any
    #    Done ticket that doesn't yet have a 'reviewed' (L-flag) readiness entry.
    {
        "name": "Done → Learnings extraction",
        "description": (
            "When a ticket lands in Done without a Learnings (L) readiness flag, "
            "ask the Worker agent to summarise lessons from the ticket's history "
            "and write them into the L flag. Disabled by default."
        ),
        "system": 1,
        "enabled": 0,
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "section_equals", "value": "Done"},
                # lacks_readiness_flag triggers when the 'reviewed' (L) flag is NOT set.
                {"kind": "lacks_readiness_flag", "flag": "L"},
                # Honour the per-ticket automation toggle uniformly.
                {"kind": "automation_mode", "value": "auto"},
                {"kind": "no_active_run"},
            ]
        },
        "on_success_json": {
            "set_readiness_content": {"flag": "reviewed", "from": "stdout"},
        },
        "steps": [
            {
                "agent_id": "agent_worker",
                "agent_name": "Worker",
                "prompt_template": (
                    "Ticket {{ticket.id}}: {{ticket.title}} has been completed.\n\n"
                    "Description:\n{{ticket.description}}\n\n"
                    "Acceptance criteria:\n{{ticket.acceptance_criteria}}\n\n"
                    "Summarise the key lessons learned from working on this ticket. "
                    "Focus on: what worked, what didn't, what would be done differently, "
                    "and any reusable patterns or pitfalls discovered. "
                    "Write the summary as plain prose (2-5 paragraphs). "
                    "Output ONLY the summary text followed by your handoff JSON on the last line."
                ),
                "on_failure": "pause",
                "timeout_ms": 300000,
            }
        ],
    },
    # 7. Sprint tag rotation (example — disabled by default).
    #    Demonstrates tag-ops on_success effects and has_tag/lacks_tag trigger predicates.
    #    Clone this to create your own tag rotation workflows.
    {
        "name": "Sprint tag rotation",
        "description": (
            "Example workflow: rotate 'sprint-current' tag to 'sprint-prev' on tickets "
            "that carry the sprint-current label. Zero steps — pure tag mutation. "
            "Disabled by default; clone to create a custom rotation trigger."
        ),
        "system": 1,
        "enabled": 0,
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "has_tag", "value": ["sprint-current"]},
                # Honour the per-ticket automation toggle uniformly.
                {"kind": "automation_mode", "value": "auto"},
                {"kind": "no_active_run"},
            ]
        },
        "on_success_json": {
            "remove_tags": ["sprint-current"],
            "add_tags": ["sprint-prev"],
        },
        "steps": [],  # Pure mutation — NoopRunner.
    },
    # 7b. Refresh ticket summary: Summarizer agent emits a one-sentence status
    #     line whenever the ticket's content hash differs from the cached one.
    #     Used to populate ticket.summary_oneliner without paying LLM cost at
    #     view time. The hash compare in summary_stale guarantees the workflow
    #     stops firing once the summary catches up — and naturally re-fires when
    #     the user (or any other workflow) mutates the ticket. The agent's
    #     stdout sentence is captured by set_summary_oneliner, which also
    #     refreshes the stored hash atomically. Honours the per-ticket
    #     automation toggle uniformly with every other system workflow: a
    #     ticket flipped to manual gets no summary refresh, even though this
    #     workflow does no business-logic mutation.
    {
        "name": "Refresh ticket summary",
        "description": (
            "Generate a one-sentence status line for any ticket whose cached "
            "summary is stale (content has changed since the last summary). "
            "Powers the idle-style summary in the ticket detail overlay."
        ),
        "system": 1,
        "enabled": 1,
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "summary_stale"},
                {"kind": "automation_mode", "value": "auto"},
                {"kind": "no_active_run"},
            ]
        },
        "on_success_json": {"set_summary_oneliner": True},
        "steps": [
            {
                "agent_id": "agent_summarizer",
                "agent_name": "Summarizer",
                "prompt_template": (
                    "Ticket {{ticket.id}} — {{ticket.title}}\n"
                    "Section: {{ticket.section}}    Status: {{ticket.status}}\n\n"
                    "Description:\n{{ticket.description}}\n\n"
                    "Acceptance criteria:\n{{ticket.acceptance_criteria}}\n\n"
                    "Write the one-sentence status line as instructed in your "
                    "system prompt. Output the sentence only — nothing else."
                ),
                "on_failure": "pause",
                "timeout_ms": 60000,
            }
        ],
    },
    # 8. Plan Check: Planner → Consultant → Planner (mediator), modelled on /plan-check.
    #    Step 1 starts Claude warm with ticket context; step 3 resumes Claude's session
    #    so it remembers its own plan when synthesizing the Consultant's review.
    #    Manual trigger only (trigger_json: null).
    {
        "name": "Plan Check",
        "description": (
            "Plan-check pattern modelled on /plan-check. "
            "Step 1: Planner (Claude) drafts a plan from the ticket. "
            "Step 2: Consultant (Codex, read-only) reviews the plan. "
            "Step 3: Planner mediates Codex's findings and produces a final verdict. "
            "Manual run only."
        ),
        "system": 1,
        "enabled": 1,
        "subject_type": "ticket",
        "trigger_json": None,
        "on_success_json": {},
        "steps": [
            {
                "agent_id": "agent_planner",
                "agent_name": "Planner",
                "prompt_template": INITIAL_PLAN_TEMPLATE,
                "on_failure": "pause",
                "timeout_ms": 300000,
            },
            {
                "agent_id": "agent_consultant",
                "agent_name": "Consultant",
                "prompt_template": CONSULTANT_REVIEW_TEMPLATE,
                "on_failure": "pause",
                "timeout_ms": 300000,
            },
            {
                "agent_id": "agent_planner",
                "agent_name": "Planner",
                "prompt_template": MEDIATION_SYNTHESIS_TEMPLATE,
                "on_failure": "pause",
                "timeout_ms": 300000,
                "use_resume": True,
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Seeder — endpoints (global, run once at startup)
# ---------------------------------------------------------------------------

def seed_default_endpoints(db: sqlite3.Connection) -> dict[str, int]:
    """Upsert DEFAULT_ENDPOINTS into the endpoints table.

    Returns {"upserted": n, "skipped_collision": m}.
    System rows always overwrite. If a system=0 row already exists with
    the same id as a DEFAULT_ENDPOINTS entry, log and skip.
    """
    upserted = 0
    skipped = 0
    for ep in DEFAULT_ENDPOINTS:
        existing = db.execute(
            "SELECT system FROM endpoints WHERE id = ?", (ep.id,)
        ).fetchone()
        if existing is not None and existing[0] == 0:
            print(f"WARN seed: skipping system endpoint {ep.id} — "
                  f"user row with same id exists, please rename")
            skipped += 1
            continue
        db.execute("""
            INSERT INTO endpoints (id, name, endpoint_type, provider, model,
                base_url, api_key_env, command, args, prompt_mode,
                timeout_s, capabilities, session_config, system)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                endpoint_type=excluded.endpoint_type,
                provider=excluded.provider,
                model=excluded.model,
                base_url=excluded.base_url,
                api_key_env=excluded.api_key_env,
                command=excluded.command,
                args=excluded.args,
                prompt_mode=excluded.prompt_mode,
                timeout_s=excluded.timeout_s,
                capabilities=excluded.capabilities,
                session_config=excluded.session_config,
                system=excluded.system
            WHERE endpoints.system = 1
        """, (
            ep.id, ep.name, ep.endpoint_type, ep.provider, ep.model,
            ep.base_url, ep.api_key_env, ep.command,
            json.dumps(ep.args), ep.prompt_mode, ep.timeout_s,
            json.dumps(ep.capabilities), json.dumps(ep.session_config),
            ep.system,
        ))
        upserted += 1
    db.commit()
    print(f"INFO seed: endpoints_upserted={upserted} endpoints_skipped_collision={skipped}")
    return {"upserted": upserted, "skipped_collision": skipped}


# ---------------------------------------------------------------------------
# Seeder — agents (global, run once at startup)
# ---------------------------------------------------------------------------

def seed_default_agents(db: sqlite3.Connection) -> dict[str, int]:
    """Insert or migrate default global agents.  Idempotent.

    - Inserts each DEFAULT_AGENTS row if missing (matched by id).
    - Migrates any existing ``agent_planchk`` row to ``agent_consultant``:
      renames the row in place and rewrites steps JSON in all workflows that
      reference the old id.

    Returns {"inserted": N, "existing": M, "migrated": K}.
    """
    inserted = 0
    existing = 0
    migrated = 0
    now = datetime.now(timezone.utc).isoformat()

    # --- migrate old agent_planchk → agent_consultant ---
    old_id = "agent_planchk"
    new_id = "agent_consultant"
    old_row = db.execute(
        "SELECT id FROM workflow_agents WHERE id = ?", (old_id,)
    ).fetchone()
    if old_row:
        # Only rename if the target id doesn't already exist
        target_exists = db.execute(
            "SELECT 1 FROM workflow_agents WHERE id = ?", (new_id,)
        ).fetchone()
        if not target_exists:
            db.execute(
                "UPDATE workflow_agents "
                "SET id = ?, name = ?, command = ?, args = ?, persist_session = 1 "
                "WHERE id = ?",
                (new_id, "Consultant", "codex", "exec", old_id),
            )
            migrated += 1
        else:
            # Target already exists — just delete the old orphaned row
            db.execute("DELETE FROM workflow_agents WHERE id = ?", (old_id,))
            migrated += 1

        # Rewrite steps JSON in all workflows referencing the old agent_id
        wf_rows = db.execute(
            "SELECT id, steps FROM workflows WHERE steps LIKE ?",
            (f'%"{old_id}"%',),
        ).fetchall()
        for wf_row in wf_rows:
            try:
                steps = json.loads(wf_row["steps"])
                changed = False
                for step in steps:
                    if step.get("agent_id") == old_id:
                        step["agent_id"] = new_id
                        changed = True
                if changed:
                    db.execute(
                        "UPDATE workflows SET steps = ? WHERE id = ?",
                        (json.dumps(steps, ensure_ascii=False), wf_row["id"]),
                    )
            except (json.JSONDecodeError, TypeError):
                pass  # Malformed steps — leave untouched

    # --- insert or sync missing agents ---
    # Detect whether workflow_agents has the system column (added in migration 17)
    # and the endpoint_id column (added in migration 19).
    _agent_cols = {
        row["name"]
        for row in db.execute("PRAGMA table_info(workflow_agents)").fetchall()
    }
    _has_system_col = "system" in _agent_cols
    _has_endpoint_id_col = "endpoint_id" in _agent_cols

    for agent in DEFAULT_AGENTS:
        row = db.execute(
            "SELECT id FROM workflow_agents WHERE id = ?", (agent["id"],)
        ).fetchone()
        if row:
            # Ensure canonical fields stay in sync with the DEFAULT_AGENTS definition.
            if _has_system_col and _has_endpoint_id_col:
                # endpoint_id deliberately omitted: seed sets it on INSERT, preserves user choice on subsequent re-seeds
                db.execute(
                    "UPDATE workflow_agents "
                    "SET name = ?, command = ?, args = ?, persist_session = ?, system = ? "
                    "WHERE id = ?",
                    (
                        agent["name"],
                        agent["command"],
                        agent["args"],
                        agent.get("persist_session", 0),
                        agent.get("system", 0),
                        agent["id"],
                    ),
                )
            elif _has_system_col:
                db.execute(
                    "UPDATE workflow_agents "
                    "SET name = ?, command = ?, args = ?, persist_session = ?, system = ? "
                    "WHERE id = ?",
                    (
                        agent["name"],
                        agent["command"],
                        agent["args"],
                        agent.get("persist_session", 0),
                        agent.get("system", 0),
                        agent["id"],
                    ),
                )
            else:
                db.execute(
                    "UPDATE workflow_agents "
                    "SET name = ?, command = ?, args = ?, persist_session = ? "
                    "WHERE id = ?",
                    (
                        agent["name"],
                        agent["command"],
                        agent["args"],
                        agent.get("persist_session", 0),
                        agent["id"],
                    ),
                )
            existing += 1
            continue

        if _has_system_col and _has_endpoint_id_col:
            db.execute(
                """
                INSERT INTO workflow_agents
                    (id, name, command, args, system_prompt, persist_session, system, endpoint_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent["id"],
                    agent["name"],
                    agent["command"],
                    agent["args"],
                    agent.get("system_prompt", ""),
                    agent.get("persist_session", 0),
                    agent.get("system", 0),
                    agent.get("endpoint_id"),
                    now,
                ),
            )
        elif _has_system_col:
            db.execute(
                """
                INSERT INTO workflow_agents
                    (id, name, command, args, system_prompt, persist_session, system, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent["id"],
                    agent["name"],
                    agent["command"],
                    agent["args"],
                    agent.get("system_prompt", ""),
                    agent.get("persist_session", 0),
                    agent.get("system", 0),
                    now,
                ),
            )
        else:
            db.execute(
                """
                INSERT INTO workflow_agents
                    (id, name, command, args, system_prompt, persist_session, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent["id"],
                    agent["name"],
                    agent["command"],
                    agent["args"],
                    agent.get("system_prompt", ""),
                    agent.get("persist_session", 0),
                    now,
                ),
            )
        inserted += 1

    # --- set agent.default setting to Orchestrator for all registered projects ---
    # This is idempotent: only sets the default if no per-project value exists yet.
    try:
        existing_default = db.execute(
            "SELECT value FROM settings WHERE key = 'agent.default'"
        ).fetchone()
        if not existing_default:
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('agent.default', 'agent_orchestrator')"
            )
    except Exception:
        pass  # settings table may not exist on very old DBs — harmless

    db.commit()
    return {"inserted": inserted, "existing": existing, "migrated": migrated}


# ---------------------------------------------------------------------------
# Seeder — workflows (per-project)
# ---------------------------------------------------------------------------

def seed_default_workflows(db: sqlite3.Connection, project_id: str) -> dict[str, int]:
    """Link *project_id* to every default system workflow. Idempotent.

    Post-migration-16 model: system workflows are first-class single rows.
    Seeding a project no longer duplicates them — it just inserts (or updates)
    a row in `workflow_projects(workflow_id, project_id, enabled)`. If a system
    workflow row for a given default name doesn't exist yet (cold-start install
    with no migration yet to copy from), we create one canonical row first.

    Workflow body (description / steps / trigger / on_success / subject_type)
    is refreshed from the seed definition so changes to DEFAULT_WORKFLOWS
    propagate. Per-project enabled state is preserved on the link row, but if
    no link exists yet we use the seed's default enabled flag.

    Returns {"linked": N, "updated_body": M, "already_linked": K}.
    """
    linked = 0
    updated_body = 0
    already_linked = 0
    now = datetime.now(timezone.utc).isoformat()

    for wf in DEFAULT_WORKFLOWS:
        steps_json = json.dumps(wf["steps"], ensure_ascii=False)
        trigger_json = json.dumps(wf["trigger_json"], ensure_ascii=False)
        on_success_json = json.dumps(wf.get("on_success_json", {}), ensure_ascii=False)

        # Find the canonical workflow row for this template (any system row with
        # matching name; post-migration there's exactly one).
        row = db.execute(
            "SELECT id, description, steps, trigger_json, on_success_json, subject_type "
            "FROM workflows WHERE name = ? AND system = 1",
            (wf["name"],),
        ).fetchone()

        if row:
            wf_id = row[0] if not hasattr(row, "keys") else row["id"]
            cur = (
                row[1] if not hasattr(row, "keys") else row["description"],
                row[2] if not hasattr(row, "keys") else row["steps"],
                row[3] if not hasattr(row, "keys") else row["trigger_json"],
                row[4] if not hasattr(row, "keys") else row["on_success_json"],
                row[5] if not hasattr(row, "keys") else row["subject_type"],
            )
            target = (wf["description"], steps_json, trigger_json, on_success_json, wf["subject_type"])
            if cur != target:
                db.execute(
                    "UPDATE workflows SET description=?, steps=?, trigger_json=?, "
                    "on_success_json=?, subject_type=?, updated_at=? WHERE id=?",
                    (*target, now, wf_id),
                )
                updated_body += 1
        else:
            # Cold start — no canonical row yet, mint one.
            wf_id = f"sys::{wf['name'].lower().replace(' ', '-').replace('→', 'to')}"
            db.execute(
                """
                INSERT INTO workflows
                    (id, name, description, steps, created_at, updated_at,
                     system, enabled, trigger_json, on_success_json, subject_type, project_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    wf_id,
                    wf["name"],
                    wf["description"],
                    steps_json,
                    now,
                    now,
                    wf["system"],
                    wf["enabled"],
                    trigger_json,
                    on_success_json,
                    wf["subject_type"],
                ),
            )

        # Link this project to the canonical workflow if not already linked.
        existing_link = db.execute(
            "SELECT enabled FROM workflow_projects WHERE workflow_id = ? AND project_id = ?",
            (wf_id, project_id),
        ).fetchone()
        if existing_link is None:
            db.execute(
                "INSERT INTO workflow_projects (workflow_id, project_id, enabled) VALUES (?, ?, ?)",
                (wf_id, project_id, wf["enabled"]),
            )
            linked += 1
        else:
            already_linked += 1

    db.commit()
    return {"linked": linked, "updated_body": updated_body, "already_linked": already_linked}
