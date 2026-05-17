"""Compatibility checkpoint — Task 13.

After migration #19, every system agent (id starts with 'agent_') MUST
resolve its argv via an endpoint, NOT via the compat fallback path.
Any compat-path warning for a system agent means the migration didn't
wire that agent correctly.

This is a GATE: if any test in this file fails, fix the migration
before proceeding to UI work (Tasks 19-20).
"""
import json
import logging
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def migrated_db_with_system_agents(tmp_path):
    """Build a DB with migrations 1-18 + sample system agent rows,
    then run migration 19 to wire them to endpoints."""
    import db as ttdb
    import workflows_seed as ws

    db_path = tmp_path / "checkpoint.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    # Stub migration 20 during init so we can seed legacy rows first
    with patch.object(ttdb, "_apply_migration_20", lambda c: None,
                      create=True):
        ttdb.init_db(conn)

    # Seed the 6 system agents as they exist pre-migration (no endpoint_id)
    legacy_agents = [
        ("agent_planner",      "Planner",      "claude", "[]", "...", 1, 1),
        ("agent_consultant",   "Consultant",   "codex",
         '["exec", "-s", "read-only"]', "...", 1, 1),
        ("agent_orchestrator", "Orchestrator", "claude", "[]", "...", 1, 1),
        ("agent_worker",       "Worker",       "claude", "[]", "...", 0, 1),
        ("agent_summarizer",   "Summarizer",   "claude", "[]", "...", 0, 1),
        ("agent_validator",    "Validator",    "claude", "[]", "...", 0, 1),
    ]
    conn.executemany("""
        INSERT INTO workflow_agents
            (id, name, command, args, system_prompt, persist_session, system)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, legacy_agents)
    conn.commit()

    # Now run migration 20 for real
    from db import _apply_migration_20
    _apply_migration_20(conn)
    return conn


SYSTEM_AGENT_IDS = [
    "agent_planner",
    "agent_consultant",
    "agent_orchestrator",
    "agent_worker",
    "agent_summarizer",
    "agent_validator",
]


@pytest.mark.parametrize("agent_id", SYSTEM_AGENT_IDS)
def test_system_agent_uses_endpoint_not_compat(
    migrated_db_with_system_agents, agent_id, caplog
):
    """Every system agent must resolve via its endpoint, never via
    the compat path."""
    from runners import _resolve_argv_for_agent, _seen_compat_agents
    _seen_compat_agents.clear()   # reset between parametrized runs

    conn = migrated_db_with_system_agents
    row = conn.execute(
        "SELECT * FROM workflow_agents WHERE id = ?", (agent_id,)
    ).fetchone()
    assert row is not None, f"agent {agent_id} not in DB"
    agent = dict(row)
    assert agent["endpoint_id"] is not None, \
        f"agent {agent_id} has NULL endpoint_id after migration"

    caplog.set_level(logging.WARNING, logger="runner")
    with caplog.at_level(logging.WARNING):
        ep, argv = _resolve_argv_for_agent(conn, agent, "test prompt")

    # The KEY assertion: no compat-path warning for this agent
    compat_warnings = [
        r for r in caplog.records
        if "using compat command" in r.message and agent_id in r.message
    ]
    assert len(compat_warnings) == 0, (
        f"agent {agent_id} triggered compat path: {compat_warnings}"
    )

    # And the argv should be sensible (starts with the expected command)
    assert argv[0] in ("claude", "codex"), (
        f"agent {agent_id} produced unexpected command: {argv}"
    )
    # The endpoint should be a canonical seeded id
    assert ep.id in ("claude-cli", "codex-cli", "codex-exec-readonly"), (
        f"agent {agent_id} routed to non-canonical endpoint {ep.id!r}"
    )


def test_no_system_agent_has_null_endpoint_id(migrated_db_with_system_agents):
    """Belt-and-suspenders: query every system agent and assert
    endpoint_id is set."""
    conn = migrated_db_with_system_agents
    nulls = conn.execute("""
        SELECT id FROM workflow_agents
        WHERE system = 1 AND endpoint_id IS NULL
    """).fetchall()
    assert len(nulls) == 0, (
        f"{len(nulls)} system agents have NULL endpoint_id post-migration: "
        f"{[r['id'] for r in nulls]}"
    )
