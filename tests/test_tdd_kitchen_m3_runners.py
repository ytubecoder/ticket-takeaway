"""TDD tests for Kitchen M3 — Runner ABC + AgentRunner.

Hermetic. Uses tmp_path for both the DB and the workspace root, plus a real
local git repo so create_or_reuse can produce a real worktree.

The "agent" under test is just a configurable shell command (e.g. 'echo OK'
or 'false') — we're testing the runner lifecycle, not any specific CLI.
"""

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def repo(tmp_path):
    upstream = tmp_path / "upstream.git"
    work = tmp_path / "project"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(upstream)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(work)],
        check=True,
        capture_output=True,
    )
    for k, v in [("user.email", "test@example.invalid"), ("user.name", "Test")]:
        subprocess.run(
            ["git", "-C", str(work), "config", k, v], check=True, capture_output=True
        )
    (work / "README.md").write_text("# project\n")
    subprocess.run(
        ["git", "-C", str(work), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(upstream)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )
    return work


@pytest.fixture
def env(tmp_path, monkeypatch, repo):
    """A fully-wired test environment: temp DB initialized + workspaces root
    pointed at tmp_path + a workspace already created for B-1.

    Returns a dict with keys: db_file, conn_factory, workspaces, runners,
    workspace (WorkspaceInfo for ticket B-1).
    """
    import constants

    db_file = tmp_path / "tickets.db"
    monkeypatch.setattr(
        constants, "DASHBOARD_DIR", tmp_path / ".claude" / "ticket-takeaway"
    )
    (tmp_path / ".claude" / "ticket-takeaway").mkdir(parents=True, exist_ok=True)

    import db

    importlib.reload(db)
    import workspaces

    importlib.reload(workspaces)
    import runners

    importlib.reload(runners)

    # Init schema.
    c = sqlite3.connect(db_file)
    c.row_factory = sqlite3.Row
    db.init_db(c)
    c.close()

    # Seed a ticket.
    c = sqlite3.connect(db_file)
    c.execute(
        "INSERT INTO tickets (id, project_id, title, section, status, description) "
        "VALUES ('B-1', 'p', 'Test ticket', 'Backlog', 'specified', 'desc')"
    )
    c.commit()
    c.close()

    # Create a workspace.
    ws = workspaces.create_or_reuse(repo, "p", "ticket", "B-1")

    def conn_factory():
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    return {
        "db_file": db_file,
        "conn_factory": conn_factory,
        "workspaces": workspaces,
        "runners": runners,
        "workspace": ws,
        "repo": repo,
    }


def _new_run(env, runner_kind="agent", status="queued"):
    """Insert a queued run row + return its id."""
    c = env["conn_factory"]()
    cur = c.execute(
        "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, status, triggered_by) "
        "VALUES ('p', 'ticket', 'B-1', ?, ?, 'human')",
        (runner_kind, status),
    )
    rid = cur.lastrowid
    c.commit()
    c.close()
    return rid


def _events(env, run_id=None, kind=None):
    c = env["conn_factory"]()
    sql = (
        "SELECT event_kind, actor_type, actor_id, payload_json "
        "FROM activity_events WHERE 1=1"
    )
    args: list = []
    if run_id is not None:
        sql += " AND actor_id = ?"
        args.append(str(run_id))
    if kind is not None:
        sql += " AND event_kind = ?"
        args.append(kind)
    sql += " ORDER BY id"
    rows = c.execute(sql, args).fetchall()
    c.close()
    return rows


def _run_row(env, run_id):
    c = env["conn_factory"]()
    row = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    c.close()
    return row


# ---------------------------------------------------------------------------
# AgentRunner — happy path
# ---------------------------------------------------------------------------


class TestAgentRunnerHappyPath:
    def test_successful_command_succeeds(self, env):
        run_id = _new_run(env)
        config = {"agent": {"command": "echo OK"}, "hooks": {}, "_prompt_template": ""}
        outcome = (
            env["runners"]
            .AgentRunner()
            .execute(
                run_id,
                "p",
                "ticket",
                "B-1",
                env["workspace"],
                config,
                env["conn_factory"],
            )
        )
        assert outcome.final_status == "succeeded"
        row = _run_row(env, run_id)
        assert row["status"] == "succeeded"
        assert row["exit_code"] == 0
        assert row["finished_at"] is not None
        assert "OK" in (row["summary"] or "")
        # workspace_path persisted on the row.
        assert row["workspace_path"] == str(env["workspace"].path)

    def test_emits_run_succeeded_with_agent_actor(self, env):
        run_id = _new_run(env)
        config = {"agent": {"command": "echo done"}, "hooks": {}}
        env["runners"].AgentRunner().execute(
            run_id, "p", "ticket", "B-1", env["workspace"], config, env["conn_factory"]
        )
        rows = _events(env, run_id=run_id, kind="run_succeeded")
        assert len(rows) == 1
        assert rows[0]["actor_type"] == "agent"
        assert rows[0]["actor_id"] == str(run_id)
        payload = json.loads(rows[0]["payload_json"])
        assert payload["run_id"] == run_id
        assert "duration_ms" in payload

    def test_emits_workspace_created_event(self, env):
        run_id = _new_run(env)
        config = {"agent": {"command": "true"}, "hooks": {}}
        env["runners"].AgentRunner().execute(
            run_id, "p", "ticket", "B-1", env["workspace"], config, env["conn_factory"]
        )
        rows = _events(env, run_id=run_id, kind="workspace_created")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["path"] == str(env["workspace"].path)


# ---------------------------------------------------------------------------
# AgentRunner — failure modes
# ---------------------------------------------------------------------------


class TestAgentRunnerFailures:
    def test_non_zero_exit_marks_failed(self, env):
        run_id = _new_run(env)
        config = {"agent": {"command": "false"}, "hooks": {}}
        outcome = (
            env["runners"]
            .AgentRunner()
            .execute(
                run_id,
                "p",
                "ticket",
                "B-1",
                env["workspace"],
                config,
                env["conn_factory"],
            )
        )
        assert outcome.final_status == "failed"
        row = _run_row(env, run_id)
        assert row["status"] == "failed"
        assert row["error_class"] == "non_zero_exit"
        assert row["exit_code"] == 1
        # Run-failed event emitted with agent actor.
        rows = _events(env, run_id=run_id, kind="run_failed")
        assert len(rows) == 1
        assert rows[0]["actor_type"] == "agent"

    def test_missing_command_fails_cleanly(self, env):
        run_id = _new_run(env)
        config = {"agent": {"command": "   "}, "hooks": {}}
        outcome = (
            env["runners"]
            .AgentRunner()
            .execute(
                run_id,
                "p",
                "ticket",
                "B-1",
                env["workspace"],
                config,
                env["conn_factory"],
            )
        )
        assert outcome.final_status == "failed"
        row = _run_row(env, run_id)
        assert row["error_class"] == "missing_command"

    def test_unknown_command_fails(self, env):
        run_id = _new_run(env)
        config = {
            "agent": {"command": "this-binary-definitely-does-not-exist-xyz"},
            "hooks": {},
        }
        outcome = (
            env["runners"]
            .AgentRunner()
            .execute(
                run_id,
                "p",
                "ticket",
                "B-1",
                env["workspace"],
                config,
                env["conn_factory"],
            )
        )
        assert outcome.final_status == "failed"
        row = _run_row(env, run_id)
        assert row["error_class"] in ("agent_not_found", "subprocess_error")


# ---------------------------------------------------------------------------
# AgentRunner — hook integration
# ---------------------------------------------------------------------------


class TestAgentRunnerHooks:
    def test_after_create_runs_only_when_not_bootstrapped(self, env):
        # First run: workspace.bootstrapped = False from create_or_reuse → should run.
        marker = env["workspace"].path / "after_create_ran.txt"
        run_id = _new_run(env)
        config = {
            "agent": {"command": "true"},
            "hooks": {
                "after_create": f"touch {marker}",
                "before_run": "",
                "after_run": "",
            },
        }
        env["runners"].AgentRunner().execute(
            run_id, "p", "ticket", "B-1", env["workspace"], config, env["conn_factory"]
        )
        assert marker.exists()
        # Bootstrap marker should now exist so a subsequent fresh WorkspaceInfo
        # would have bootstrapped=True.
        assert (env["workspace"].path / env["workspaces"].BOOTSTRAP_MARKER).exists()

    def test_after_create_skipped_when_workspace_bootstrapped(self, env):
        # Pre-mark as bootstrapped.
        env["workspaces"].mark_bootstrapped(env["workspace"].path)
        # Re-fetch WorkspaceInfo so bootstrapped=True is captured.
        ws = env["workspaces"].create_or_reuse(env["repo"], "p", "ticket", "B-1")
        assert ws.bootstrapped is True
        marker = ws.path / "should_not_exist.txt"
        run_id = _new_run(env)
        config = {
            "agent": {"command": "true"},
            "hooks": {"after_create": f"touch {marker}"},
        }
        env["runners"].AgentRunner().execute(
            run_id, "p", "ticket", "B-1", ws, config, env["conn_factory"]
        )
        assert not marker.exists()

    def test_before_run_failure_is_fatal(self, env):
        run_id = _new_run(env)
        config = {
            "agent": {"command": "echo should-not-run"},
            "hooks": {"before_run": "exit 7"},
        }
        outcome = (
            env["runners"]
            .AgentRunner()
            .execute(
                run_id,
                "p",
                "ticket",
                "B-1",
                env["workspace"],
                config,
                env["conn_factory"],
            )
        )
        assert outcome.final_status == "failed"
        row = _run_row(env, run_id)
        assert row["error_class"] == "hook_before_run"
        # The agent subprocess never ran — no agent_output / run_succeeded events.
        assert _events(env, run_id=run_id, kind="run_succeeded") == []

    def test_after_run_failure_is_ignored(self, env):
        run_id = _new_run(env)
        config = {
            "agent": {"command": "echo done"},
            "hooks": {"after_run": "exit 9"},
        }
        outcome = (
            env["runners"]
            .AgentRunner()
            .execute(
                run_id,
                "p",
                "ticket",
                "B-1",
                env["workspace"],
                config,
                env["conn_factory"],
            )
        )
        # Run still succeeded — after_run failure logs but doesn't fail the run.
        assert outcome.final_status == "succeeded"
        # But hook_failed event should be present for after_run.
        events = _events(env, run_id=run_id, kind="hook_failed")
        assert len(events) == 1
        payload = json.loads(events[0]["payload_json"])
        assert payload["hook"] == "after_run"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestAgentRunnerCancellation:
    def test_cancel_event_before_subprocess_marks_cancelled(self, env):
        run_id = _new_run(env)
        config = {"agent": {"command": "echo never"}, "hooks": {}}
        ev = threading.Event()
        ev.set()  # request cancel before execute even starts
        outcome = (
            env["runners"]
            .AgentRunner()
            .execute(
                run_id,
                "p",
                "ticket",
                "B-1",
                env["workspace"],
                config,
                env["conn_factory"],
                cancel_event=ev,
            )
        )
        assert outcome.final_status == "cancelled"
        row = _run_row(env, run_id)
        assert row["status"] == "cancelled"
        # run_cancelled event with agent actor.
        rows = _events(env, run_id=run_id, kind="run_cancelled")
        assert len(rows) == 1
        assert rows[0]["actor_type"] == "agent"


# ---------------------------------------------------------------------------
# State invariants — every status update bumps heartbeat_at
# ---------------------------------------------------------------------------


class TestStateInvariants:
    def test_heartbeat_updated_on_status_change(self, env):
        run_id = _new_run(env)
        config = {"agent": {"command": "echo done"}, "hooks": {}}
        env["runners"].AgentRunner().execute(
            run_id, "p", "ticket", "B-1", env["workspace"], config, env["conn_factory"]
        )
        row = _run_row(env, run_id)
        assert row["heartbeat_at"] is not None
        assert row["finished_at"] is not None

    def test_subject_type_and_id_preserved_in_events(self, env):
        run_id = _new_run(env)
        config = {"agent": {"command": "echo done"}, "hooks": {}}
        env["runners"].AgentRunner().execute(
            run_id, "p", "ticket", "B-1", env["workspace"], config, env["conn_factory"]
        )
        # Every event for this run lives on subject ticket/B-1.
        c = env["conn_factory"]()
        rows = c.execute(
            "SELECT subject_type, subject_id FROM activity_events WHERE actor_id = ?",
            (str(run_id),),
        ).fetchall()
        c.close()
        assert all(
            r["subject_type"] == "ticket" and r["subject_id"] == "B-1" for r in rows
        )
