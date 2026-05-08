"""Seeds 5 default system=1 workflows that replicate current Kitchen behaviour,
plus the Consultant agent and Plan Check workflow.

Call ``seed_default_agents(db)`` once at server startup (idempotent, global).
Call ``seed_default_workflows(db, project_id)`` per-project at startup (idempotent).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


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
# Default agents (global — no project_id)
# ---------------------------------------------------------------------------

DEFAULT_AGENTS: list[dict] = [
    # Planner: Claude. Drafts the plan in step 1, mediates Codex in step 3.
    # persist_session lets step 3 resume step 1's session — Planner remembers
    # its own plan when synthesizing the Consultant's review.
    {
        "id": "agent_planner",
        "name": "Planner",
        "command": "claude",
        "args": "-p",
        "system_prompt": "",   # /plan-check sets none — templates carry the instructions
        "persist_session": 1,
    },
    # Consultant: Codex, sandboxed read-only (mirrors `/plan-check` exactly).
    {
        "id": "agent_consultant",
        "name": "Consultant",
        "command": "codex",
        "args": "exec -s read-only",
        "system_prompt": "",
        "persist_session": 1,
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
    # 1. Spec → Backlog: Ideas ticket with description + criteria → promote to Backlog
    {
        "name": "Spec → Backlog",
        "description": "Promote Ideas tickets that have a description and acceptance criteria to Backlog",
        "system": 1,
        "enabled": 1,
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "section_equals", "value": "Ideas"},
                {"kind": "automation_mode", "value": "auto"},
                {"kind": "has_field", "field": "description"},
                {"kind": "criteria_count_gte", "value": 1},
                {"kind": "no_active_run"},
            ]
        },
        "on_success_json": {"move_to": "Backlog"},
        "steps": [
            {
                "agent_id": None,
                "agent_name": "specifier",
                "prompt_template": _ticket_prompt("Refine the specification for"),
                "on_failure": "pause",
                "timeout_ms": 300000,
            }
        ],
    },
    # 2. Backlog → WIP: criteria-led eligibility — acceptance criteria are
    #    the bar. Users wanting stricter test gating can still add the
    #    `tests_covered` predicate (linked journey or no_test_required) to
    #    their own workflows.
    {
        "name": "Backlog → WIP",
        "description": "Auto-dispatch Backlog tickets that are fully ready to start work",
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
                "agent_id": None,
                "agent_name": "implementer",
                "prompt_template": _ticket_prompt("Implement"),
                "on_failure": "pause",
                "timeout_ms": 600000,
            }
        ],
    },
    # 3. WIP → Review: ticket in WIP, in-progress, and has a commit hash
    {
        "name": "WIP → Review",
        "description": "Move WIP tickets to For Review once implementation has a commit",
        "system": 1,
        "enabled": 1,
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "section_equals", "value": "WIP"},
                {"kind": "status_equals", "value": "in-progress"},
                {"kind": "has_field", "field": "commit_hash"},
                # TODO: add "tests_pass" condition once a test-runner integration exists
                {"kind": "no_active_run"},
            ]
        },
        "on_success_json": {"move_to": "For Review", "set_status": "for-review"},
        "steps": [
            {
                "agent_id": None,
                "agent_name": "reviewer",
                "prompt_template": _ticket_prompt("Review the implementation for"),
                "on_failure": "pause",
                "timeout_ms": 300000,
            }
        ],
    },
    # 4a. Parent auto-promote: when ALL children of a parent reach a terminal status
    #     (done / for-review / bug-fixed), promote the parent to For Review.
    #     System workflow — bypasses automation_mode filter so it fires against
    #     any ticket that has children. Zero steps → applied via NoopRunner.
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
    # 6. Plan Check: Planner → Consultant → Planner (mediator), modelled on /plan-check.
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
    for agent in DEFAULT_AGENTS:
        row = db.execute(
            "SELECT id FROM workflow_agents WHERE id = ?", (agent["id"],)
        ).fetchone()
        if row:
            # Ensure canonical fields (like persist_session) stay in sync with
            # the DEFAULT_AGENTS definition — handles rows landed via migration.
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
                agent["system_prompt"],
                agent.get("persist_session", 0),
                now,
            ),
        )
        inserted += 1

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
