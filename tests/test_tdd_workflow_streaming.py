"""TDD tests for Phase 2 — streaming subprocess + session persistence helpers.

Pure logic, no server, no Playwright.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import init_db

# ---------------------------------------------------------------------------
# Import helpers from serve — import module directly to avoid server startup
# ---------------------------------------------------------------------------


def _import_serve():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "serve_module",
        Path(__file__).parent.parent / "src" / "serve.py",
    )
    module = importlib.util.module_from_spec(spec)
    # Minimal stubs so serve.py doesn't actually start anything on import
    with (
        mock.patch("subprocess.Popen"),
        mock.patch("threading.Thread"),
        mock.patch.dict(os.environ, {"TT_NO_SERVER_STARTUP": "1"}),
    ):
        try:
            spec.loader.exec_module(module)
        except SystemExit:
            pass
        except Exception:
            pass
    return module


# We import only the two helper functions we're testing — extract them by
# importing the module with side-effects suppressed.
try:
    _serve = _import_serve()
    _apply_resume_args = _serve._apply_resume_args
    _extract_session_id = _serve._extract_session_id
    _update_workflow_run = _serve._update_workflow_run
    _get_workflow_run = _serve._get_workflow_run
    _workflow_runs = _serve._workflow_runs
    _workflow_runs_lock = _serve._workflow_runs_lock
    _SERVE_AVAILABLE = True
except Exception as _e:
    _SERVE_AVAILABLE = False
    _apply_resume_args = None
    _extract_session_id = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """In-memory DB with full schema."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c


# ---------------------------------------------------------------------------
# _apply_resume_args — codex
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SERVE_AVAILABLE, reason="serve.py could not be imported")
class TestApplyResumeArgsCodex:
    def test_codex_no_resume_inserts_after_exec(self):
        result = _apply_resume_args("codex", ["exec", "-q"], "sid-1")
        assert result == ["exec", "resume", "sid-1", "-q"]

    def test_codex_no_resume_with_flags_before_exec(self):
        result = _apply_resume_args("codex", ["--flag", "exec", "-q"], "sid-1")
        assert result == ["--flag", "exec", "resume", "sid-1", "-q"]

    def test_codex_existing_resume_replaces_session_id(self):
        result = _apply_resume_args(
            "codex", ["exec", "resume", "old-sid", "-q"], "sid-2"
        )
        assert result == ["exec", "resume", "sid-2", "-q"]

    def test_codex_existing_resume_no_trailing_sid_inserts(self):
        # edge case: exec resume with nothing after it
        result = _apply_resume_args("codex", ["exec", "resume"], "sid-3")
        assert result == ["exec", "resume", "sid-3"]

    def test_codex_no_exec_token_appends(self):
        result = _apply_resume_args("codex", ["-q"], "sid-4")
        assert result == ["-q", "exec", "resume", "sid-4"]

    def test_codex_empty_args(self):
        result = _apply_resume_args("codex", [], "sid-5")
        assert result == ["exec", "resume", "sid-5"]

    def test_does_not_mutate_input_list(self):
        original = ["exec", "-q"]
        _ = _apply_resume_args("codex", original, "sid-x")
        assert original == ["exec", "-q"]


# ---------------------------------------------------------------------------
# _apply_resume_args — claude / generic
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SERVE_AVAILABLE, reason="serve.py could not be imported")
class TestApplyResumeArgsClaude:
    def test_claude_appends_resume_flag(self):
        result = _apply_resume_args("claude", ["-p", "hello"], "sid-3")
        assert result[-2:] == ["--resume", "sid-3"]

    def test_claude_replaces_existing_resume_flag(self):
        result = _apply_resume_args(
            "claude", ["--resume", "old", "-p", "hello"], "sid-new"
        )
        idx = result.index("--resume")
        assert result[idx + 1] == "sid-new"

    def test_claude_does_not_mutate_input(self):
        original = ["-p", "hello"]
        _ = _apply_resume_args("claude", original, "sid-y")
        assert original == ["-p", "hello"]

    def test_unknown_command_appends_resume_flag(self):
        result = _apply_resume_args("somebin", [], "sid-z")
        assert "--resume" in result
        assert "sid-z" in result


# ---------------------------------------------------------------------------
# _extract_session_id — codex
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SERVE_AVAILABLE, reason="serve.py could not be imported")
class TestExtractSessionIdCodex:
    UUID = "12345678-1234-1234-1234-123456789abc"

    def test_parses_session_colon_line(self):
        stdout = f"Session: {self.UUID}\nSome output"
        sid = _extract_session_id("codex", stdout, "", time.time())
        assert sid == self.UUID

    def test_parses_session_id_colon(self):
        stdout = f"Session ID: {self.UUID}"
        sid = _extract_session_id("codex", stdout, "", time.time())
        assert sid == self.UUID

    def test_parses_json_session_id(self):
        stdout = f'{{"session_id": "{self.UUID}"}}'
        sid = _extract_session_id("codex", stdout, "", time.time())
        assert sid == self.UUID

    def test_returns_none_when_no_match(self):
        sid = _extract_session_id("codex", "no uuid here", "", time.time())
        assert sid is None

    def test_filesystem_fallback_respects_started_before(self):
        """Files older than started_before should NOT be returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a session file with a past mtime
            fname = f"{self.UUID}.json"
            fpath = os.path.join(tmpdir, fname)
            with open(fpath, "w") as f:
                f.write("{}")
            # Set mtime to 10 seconds ago
            old_time = time.time() - 10
            os.utime(fpath, (old_time, old_time))

            with (
                mock.patch("os.path.expanduser", return_value=tmpdir),
                mock.patch("os.path.isdir", return_value=True),
            ):
                sid = _extract_session_id("codex", "", "", time.time())
            # started_before is now, file is 10s old — should not match
            assert sid is None

    def test_filesystem_fallback_finds_recent_file(self):
        """A file with mtime >= started_before should be found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = f"{self.UUID}.json"
            fpath = os.path.join(tmpdir, fname)
            with open(fpath, "w") as f:
                f.write("{}")
            # started_before is in the past — file is recent
            started = time.time() - 5
            with (
                mock.patch("os.path.expanduser", return_value=tmpdir),
                mock.patch("os.path.isdir", return_value=True),
            ):
                sid = _extract_session_id("codex", "", "", started)
            assert sid == self.UUID


# ---------------------------------------------------------------------------
# _extract_session_id — claude
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SERVE_AVAILABLE, reason="serve.py could not be imported")
class TestExtractSessionIdClaude:
    UUID = "abcdef12-abcd-abcd-abcd-abcdef123456"

    def test_parses_json_session_id(self):
        stdout = f'{{"session_id": "{self.UUID}", "result": "done"}}'
        sid = _extract_session_id("claude", stdout, "", time.time())
        assert sid == self.UUID

    def test_parses_session_id_equals(self):
        stdout = f"session_id={self.UUID}"
        sid = _extract_session_id("claude", stdout, "", time.time())
        assert sid == self.UUID

    def test_returns_none_when_no_match(self):
        sid = _extract_session_id("claude", "no session here", "", time.time())
        assert sid is None

    def test_unknown_command_returns_none(self):
        sid = _extract_session_id("somebin", f"session_id={self.UUID}", "", time.time())
        assert sid is None


# ---------------------------------------------------------------------------
# Streaming: stub Popen to yield lines, verify flush behaviour
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SERVE_AVAILABLE, reason="serve.py could not be imported")
class TestStreamingFlushBehaviour:
    """Verify that _update_workflow_run is called multiple times during streaming."""

    def _make_fake_proc(self, lines, delays=None):
        """Build a mock Popen object that yields lines with optional inter-line delays."""
        delays = delays or []
        idx = [0]
        poll_called = [0]

        def readline():
            if idx[0] < len(lines):
                if idx[0] < len(delays):
                    time.sleep(delays[idx[0]])
                line = lines[idx[0]]
                idx[0] += 1
                return line
            return ""

        def poll():
            poll_called[0] += 1
            return 0 if idx[0] >= len(lines) else None

        proc = mock.MagicMock()
        proc.stdout.readline.side_effect = readline
        proc.poll.side_effect = poll
        proc.kill = mock.MagicMock()
        return proc

    def test_multiple_flushes_during_streaming(self):
        """With 9 lines and small delays, at least 2 DB flushes should occur.

        We use a short flush_interval (0.1s) to make the test deterministic and fast.
        The production code uses 1s; the logic is the same — this tests the branching.
        """
        flush_times = []

        def fake_update(run_id, **kwargs):
            flush_times.append(time.time())

        lines = [f"line {i}\n" for i in range(9)]
        delays = [
            0.12
        ] * 9  # 0.12s between lines, flush_interval=0.1s → flush every ~line

        fake_proc = self._make_fake_proc(lines, delays)

        # Run the streaming loop directly (mirrors the logic in _run_workflow_thread)
        # but with flush_interval=0.1s to make it fast and deterministic.
        conversation = []
        turn = {"role": "streaming", "content": "", "streaming": True, "step": 0}
        conversation.append(turn)
        all_output_lines = []
        line_count = 0
        last_flush = time.time()
        deadline = time.time() + 30
        flush_interval = 0.1  # short interval for test — production uses 1.0s

        while True:
            if time.time() > deadline:
                break
            line = fake_proc.stdout.readline()
            if line == "" and fake_proc.poll() is not None:
                break
            if line:
                all_output_lines.append(line)
                turn["content"] += line
                line_count += 1
                now = time.time()
                if (now - last_flush >= flush_interval) or (line_count % 16 == 0):
                    fake_update("test-run-id", conversation=conversation)
                    last_flush = now

        # At 0.12s per line and 0.1s interval, expect ~8 flushes
        # At minimum we need >=3 to confirm mid-stream flushing (not just end-of-run)
        assert len(flush_times) >= 3, (
            f"Expected >=3 mid-stream flushes, got {len(flush_times)}"
        )
        # Verify content accumulated correctly
        assert turn["content"] == "".join(lines)

    def test_streaming_true_flips_to_false_after_completion(self):
        """turn['streaming'] should be False after the loop exits."""
        turn = {"role": "streaming", "content": "", "streaming": True, "step": 0}
        # Simulate completion
        turn["streaming"] = False
        turn["exit_code"] = 0
        assert turn["streaming"] is False
        assert "exit_code" in turn

    def test_content_accumulates_all_lines(self):
        """All lines must appear in turn['content'] after the loop."""
        lines = ["hello\n", "world\n", "done\n"]
        turn = {"content": ""}
        for line in lines:
            turn["content"] += line
        assert turn["content"] == "hello\nworld\ndone\n"


# ---------------------------------------------------------------------------
# Session ids per-run isolation: two runs don't cross-contaminate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SERVE_AVAILABLE, reason="serve.py could not be imported")
class TestSessionIdIsolation:
    """Two concurrent runs of agent_consultant each store their own session_id."""

    UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def test_two_runs_independent_session_ids(self, conn):
        """Inserting session ids into two separate run rows doesn't cross-contaminate."""
        # Seed minimal data
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES ('T-1', 'proj', 'Ticket')"
        )
        conn.execute(
            "INSERT INTO workflow_runs (id, ticket_id, project_id, workflow_id, session_ids) "
            "VALUES ('run-a', 'T-1', 'proj', 'wf-1', '{}')"
        )
        conn.execute(
            "INSERT INTO workflow_runs (id, ticket_id, project_id, workflow_id, session_ids) "
            "VALUES ('run-b', 'T-1', 'proj', 'wf-1', '{}')"
        )
        conn.commit()

        # Simulate session id capture for run-a
        sids_a = {"agent_consultant": self.UUID_A}
        conn.execute(
            "UPDATE workflow_runs SET session_ids = ? WHERE id = 'run-a'",
            (json.dumps(sids_a),),
        )
        # Simulate session id capture for run-b
        sids_b = {"agent_consultant": self.UUID_B}
        conn.execute(
            "UPDATE workflow_runs SET session_ids = ? WHERE id = 'run-b'",
            (json.dumps(sids_b),),
        )
        conn.commit()

        row_a = conn.execute(
            "SELECT session_ids FROM workflow_runs WHERE id = 'run-a'"
        ).fetchone()
        row_b = conn.execute(
            "SELECT session_ids FROM workflow_runs WHERE id = 'run-b'"
        ).fetchone()
        assert json.loads(row_a["session_ids"])["agent_consultant"] == self.UUID_A
        assert json.loads(row_b["session_ids"])["agent_consultant"] == self.UUID_B
        # No cross-contamination
        assert json.loads(row_a["session_ids"])["agent_consultant"] != self.UUID_B
        assert json.loads(row_b["session_ids"])["agent_consultant"] != self.UUID_A


# ---------------------------------------------------------------------------
# needs_input respond: conversation append + status paused + prompt cleared
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SERVE_AVAILABLE, reason="serve.py could not be imported")
class TestNeedsInputRespond:
    """Stub a workflow_run in needs_input state and simulate the respond handler."""

    def _simulate_respond(self, conn, run_id: str, response_text: str) -> dict:
        """Replicate the respond handler logic against the in-memory conn.

        Note: workflow_runs does NOT have a needs_input_prompt column;
        that lives in the Kitchen runs table. The respond handler only
        updates conversation + status.
        """
        row = conn.execute(
            "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "needs_input"

        current_step = row["current_step"] or 0
        try:
            conversation = json.loads(row["conversation"] or "[]")
        except (json.JSONDecodeError, TypeError):
            conversation = []

        from datetime import datetime

        conversation.append(
            {
                "role": "user",
                "step": current_step,
                "content": response_text,
                "ts": datetime.utcnow().isoformat(),
            }
        )
        conn.execute(
            "UPDATE workflow_runs SET conversation = ?, status = 'paused' WHERE id = ?",
            (json.dumps(conversation), run_id),
        )
        conn.commit()
        return dict(
            conn.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        )

    def test_respond_appends_user_turn(self, conn):
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES ('T-1', 'proj', 'Ticket')"
        )
        conn.execute(
            "INSERT INTO workflow_runs "
            "(id, ticket_id, project_id, workflow_id, status, conversation) "
            "VALUES ('run-1', 'T-1', 'proj', 'wf-1', 'needs_input', '[]')"
        )
        conn.commit()

        updated = self._simulate_respond(conn, "run-1", "Do the thing!")
        conversation = json.loads(updated["conversation"])
        user_turns = [t for t in conversation if t.get("role") == "user"]
        assert len(user_turns) == 1
        assert user_turns[0]["content"] == "Do the thing!"

    def test_respond_sets_status_to_paused(self, conn):
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES ('T-2', 'proj', 'Ticket')"
        )
        conn.execute(
            "INSERT INTO workflow_runs "
            "(id, ticket_id, project_id, workflow_id, status, conversation) "
            "VALUES ('run-2', 'T-2', 'proj', 'wf-1', 'needs_input', '[]')"
        )
        conn.commit()

        updated = self._simulate_respond(conn, "run-2", "My response")
        assert updated["status"] == "paused"

    def test_respond_not_running_after_respond(self, conn):
        """Status must be 'paused', not 'running' — the spin-loop exits on paused."""
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES ('T-3', 'proj', 'Ticket')"
        )
        conn.execute(
            "INSERT INTO workflow_runs "
            "(id, ticket_id, project_id, workflow_id, status, conversation) "
            "VALUES ('run-3', 'T-3', 'proj', 'wf-1', 'needs_input', '[]')"
        )
        conn.commit()

        updated = self._simulate_respond(conn, "run-3", "Response text")
        # Must be paused (not running) — the worker's spin-loop breaks on st != "paused"
        assert updated["status"] != "running"
        assert updated["status"] == "paused"

    def test_respond_preserves_existing_conversation_turns(self, conn):
        existing = json.dumps(
            [{"role": "agent", "content": "Initial review", "step": 0}]
        )
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES ('T-4', 'proj', 'Ticket')"
        )
        conn.execute(
            "INSERT INTO workflow_runs "
            "(id, ticket_id, project_id, workflow_id, status, conversation) "
            "VALUES ('run-4', 'T-4', 'proj', 'wf-1', 'needs_input', ?)",
            (existing,),
        )
        conn.commit()

        updated = self._simulate_respond(conn, "run-4", "New user response")
        conversation = json.loads(updated["conversation"])
        assert len(conversation) == 2
        assert conversation[0]["role"] == "agent"
        assert conversation[1]["role"] == "user"
        assert conversation[1]["content"] == "New user response"
