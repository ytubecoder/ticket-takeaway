"""Tests for Workflow Bounce runner adapter schema fields."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import db


def test_workflow_agents_schema_has_runner_adapter_fields(tmp_path):
    db_file = tmp_path / "tickets.db"
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row

    db.init_db(conn)

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(workflow_agents)")}
    assert {"runner_type", "command_template", "prompt_mode"}.issubset(cols)

    conn.execute(
        "INSERT INTO workflow_agents (id, name, command, args, system_prompt) VALUES (?, ?, ?, ?, ?)",
        ("a1", "Agent", "claude", "[]", ""),
    )
    row = conn.execute("SELECT runner_type, command_template, prompt_mode FROM workflow_agents WHERE id = 'a1'").fetchone()
    assert dict(row) == {
        "runner_type": "claude",
        "command_template": "",
        "prompt_mode": "arg",
    }
