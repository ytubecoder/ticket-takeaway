import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_workflow_step_agent_id_overrides_project_agent_config(tmp_path):
    import db
    import kitchen

    db_path = tmp_path / "tickets.db"

    def get_conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO workflow_agents "
            "(id, name, command, args, system_prompt, persist_session) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "agent_consultant",
                "Consultant",
                "codex",
                "exec -s read-only",
                "Be careful.",
                1,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    config = {"agent": {"command": "claude -p", "base_ref": "origin/main"}}
    workflow_meta = {
        "steps": [
            {
                "agent_id": "agent_consultant",
                "prompt_template": "Review {{ticket.id}}",
                "timeout_ms": 123000,
            }
        ],
        "step_index": 0,
    }

    kitchen._apply_workflow_step_config(config, workflow_meta, get_conn)

    assert config["_prompt_template"] == "Review {{ticket.id}}"
    assert config["agent"]["command"] == "codex"
    assert config["agent"]["args"] == "exec -s read-only"
    assert config["agent"]["system_prompt"] == "Be careful."
    assert config["agent"]["persist_session"] == 1
    assert config["agent"]["runner_type"] == "codex"
    assert workflow_meta["current_agent"] == {
        "id": "agent_consultant",
        "name": "Consultant",
        "command": "codex",
    }


def test_workflow_step_without_agent_id_keeps_existing_agent_config(tmp_path):
    import db
    import kitchen

    db_path = tmp_path / "tickets.db"

    def get_conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        return conn

    config = {"agent": {"command": "claude -p"}}
    workflow_meta = {"steps": [{"prompt_template": "Do {{subject.id}}"}], "step_index": 0}

    kitchen._apply_workflow_step_config(config, workflow_meta, get_conn)

    assert config["_prompt_template"] == "Do {{subject.id}}"
    assert config["agent"] == {"command": "claude -p"}
    assert "current_agent" not in workflow_meta
