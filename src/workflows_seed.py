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
    {
        "id": "agent_consultant",
        "name": "Consultant",
        "command": "codex",
        "args": "exec",
        "system_prompt": "",   # plan-check sets none — prompt template carries instructions
        "persist_session": 1,
    },
]


# ---------------------------------------------------------------------------
# Plan Check prompt templates
# ---------------------------------------------------------------------------

ROUND_1_REVIEW_TEMPLATE = """\
Review the following plan files for completeness, risks, and feasibility.

For each file, assess:
- Missing requirements or edge cases
- Technical risks or blockers
- Dependencies that aren't accounted for
- Sequencing issues
- Performance or operational concerns
- Anything ambiguous or underspecified

Be specific — reference sections by name.
Flag severity: critical (blocks implementation), warning (likely problem), or note (suggestion).

Plan files:

# Ticket {{ticket.id}}: {{ticket.title}}

{{ticket.description}}

## Acceptance criteria
{{ticket.acceptance_criteria}}\
"""

ROUND_2_FOLLOWUP_TEMPLATE = """\
This is round 2 of 2.

Prior round findings:
{{conversation.last_agent_response}}

Focus this round on:
- Anything from prior round still ambiguous
- New issues that surface given the prior findings
- A final verdict: Ready / Needs revision

Re-flag severity for any remaining items.\
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
    # 2. Backlog → WIP: full eligibility — replicates current Kitchen _ticket_eligibility
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
                {"kind": "tests_covered"},
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
    # 4. Review → Done: disabled by default — never auto-accept without user approval
    {
        "name": "Review → Done",
        "description": "Accept tickets in For Review (disabled by default — requires explicit user approval)",
        "system": 1,
        "enabled": 0,  # Memory: never auto-accept without user approval
        "subject_type": "ticket",
        "trigger_json": {
            "all_of": [
                {"kind": "section_equals", "value": "For Review"},
                {"kind": "status_equals", "value": "for-review"},
                {"kind": "automation_mode", "value": "auto"},
                {"kind": "no_active_run"},
            ]
        },
        "on_success_json": {"move_to": "Done", "set_status": "done"},
        "steps": [
            {
                "agent_id": None,
                "agent_name": "acceptor",
                "prompt_template": _ticket_prompt("Accept and finalise"),
                "on_failure": "pause",
                "timeout_ms": 300000,
            }
        ],
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
    # 6. Plan Check: iterative second-opinion review via Codex (manual only)
    {
        "name": "Plan Check",
        "description": (
            "Iterative second-opinion review modelled on /plan-check. "
            "Codex reviews the ticket across 2 rounds; warm session resumed between rounds. "
            "Manual run only."
        ),
        "system": 1,
        "enabled": 1,
        "subject_type": "ticket",
        "trigger_json": None,          # no auto-fire — manual trigger only
        "on_success_json": {},         # surfacing IS the value, no auto move/status
        "steps": [
            {
                "agent_id": "agent_consultant",
                "agent_name": "Consultant",
                "prompt_template": ROUND_1_REVIEW_TEMPLATE,
                "on_failure": "pause",
                "timeout_ms": 300000,
            },
            {
                "agent_id": "agent_consultant",
                "agent_name": "Consultant",
                "prompt_template": ROUND_2_FOLLOWUP_TEMPLATE,
                "on_failure": "pause",
                "timeout_ms": 300000,
                "use_resume": True,    # consumed in Phase 2 runner change
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
    """Insert any missing system workflows for *project_id*.  Idempotent.

    Matches existing rows by (project_id, name, system=1).  Does NOT
    overwrite existing system workflows.

    Returns {"inserted": N, "existing": M}.
    """
    inserted = 0
    existing = 0
    now = datetime.now(timezone.utc).isoformat()

    for wf in DEFAULT_WORKFLOWS:
        # Check by (project_id, name, system=1) in the workflows table.
        # The workflows table uses TEXT id (UUID-style), not project_id, so
        # we store project_id via a convention: name is unique per project for
        # system workflows.  We match on name + system flag.
        row = db.execute(
            "SELECT id FROM workflows WHERE name = ? AND system = 1 "
            "AND id LIKE ?",
            (wf["name"], f"{project_id}::%"),
        ).fetchone()

        if row:
            existing += 1
            continue

        wf_id = f"{project_id}::sys::{wf['name'].lower().replace(' ', '-').replace('→', 'to')}"

        steps_with_project = wf["steps"]  # keep as-is; agent_id resolved at runtime

        db.execute(
            """
            INSERT INTO workflows
                (id, name, description, steps, created_at, updated_at,
                 system, enabled, trigger_json, on_success_json, subject_type, project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wf_id,
                wf["name"],
                wf["description"],
                json.dumps(steps_with_project, ensure_ascii=False),
                now,
                now,
                wf["system"],
                wf["enabled"],
                json.dumps(wf["trigger_json"], ensure_ascii=False),
                json.dumps(wf.get("on_success_json", {}), ensure_ascii=False),
                wf["subject_type"],
                project_id,
            ),
        )
        inserted += 1

    db.commit()
    return {"inserted": inserted, "existing": existing}
