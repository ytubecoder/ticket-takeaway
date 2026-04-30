"""Seeds 5 default system=1 workflows that replicate current Kitchen behaviour.

Call ``seed_default_workflows(db, project_id)`` at server startup (idempotent).
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
]


# ---------------------------------------------------------------------------
# Seeder
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
                 system, enabled, trigger_json, on_success_json, subject_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        inserted += 1

    db.commit()
    return {"inserted": inserted, "existing": existing}
