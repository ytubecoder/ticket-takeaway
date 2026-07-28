"""TDD tests for Kitchen M5 — evidence rotation pipeline.

Hermetic. Uses a temp evidence root + monkey-patched datetime.now seam
(via the `now` arg to rotate_evidence) so we can drive the
live → summarised → pruned ladder deterministically.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Init schema in tmp DB; evidence dir lives in tmp_path."""
    import constants

    db_file = tmp_path / "tickets.db"
    monkeypatch.setattr(constants, "DASHBOARD_DIR", tmp_path / ".claude" / "tt")
    (tmp_path / ".claude" / "tt").mkdir(parents=True, exist_ok=True)

    import importlib

    import db

    importlib.reload(db)
    import evidence

    importlib.reload(evidence)

    c = sqlite3.connect(db_file)
    c.row_factory = sqlite3.Row
    db.init_db(c)
    c.close()

    def conn_factory():
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        return c

    return {
        "db_file": db_file,
        "conn_factory": conn_factory,
        "evidence": evidence,
        "evidence_root": tmp_path / "evidence",
    }


def _seed_run(
    env,
    run_id,
    evidence_status="live",
    days_ago=0,
    evidence_dir: Path | None = None,
    summary="ok",
    error_class=None,
    error_message=None,
):
    finished = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    c = env["conn_factory"]()
    c.execute(
        "INSERT INTO runs (id, project_id, subject_type, subject_id, runner_kind, "
        " status, summary, error_class, error_message, started_at, finished_at, "
        " heartbeat_at, evidence_dir, evidence_status, triggered_by) "
        "VALUES (?, 'p', 'ticket', 'B-1', 'agent', 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, 'human')",
        (
            run_id,
            summary,
            error_class,
            error_message,
            finished,
            finished,
            finished,
            str(evidence_dir) if evidence_dir else None,
            evidence_status,
        ),
    )
    c.commit()
    c.close()


def _make_evidence_dir(root: Path, run_id: int, with_files=True) -> Path:
    d = root / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    if with_files:
        # Plain transcript over 1 KiB so it actually gets gzipped.
        (d / "transcript.txt").write_text("x" * 2048)
        (d / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (d / "stdout.log").write_text(
            "short log"
        )  # under 1 KiB — should NOT be gzipped
    return d


# ---------------------------------------------------------------------------
# rotate_evidence — happy paths
# ---------------------------------------------------------------------------


class TestRotateEvidence:
    def test_recent_run_is_skipped(self, env):
        d = _make_evidence_dir(env["evidence_root"], 1)
        _seed_run(env, 1, evidence_status="live", days_ago=5, evidence_dir=d)
        counts = env["evidence"].rotate_evidence(
            env["conn_factory"], live_days=30, summarised_days=60
        )
        assert counts["summarised"] == 0
        assert counts["pruned"] == 0
        # Still live.
        c = env["conn_factory"]()
        row = c.execute("SELECT evidence_status FROM runs WHERE id=1").fetchone()
        c.close()
        assert row["evidence_status"] == "live"
        # Files untouched.
        assert (d / "transcript.txt").exists()

    def test_old_live_run_transitions_to_summarised(self, env):
        d = _make_evidence_dir(env["evidence_root"], 2)
        _seed_run(
            env,
            2,
            evidence_status="live",
            days_ago=35,
            evidence_dir=d,
            summary="all good",
        )
        counts = env["evidence"].rotate_evidence(
            env["conn_factory"], live_days=30, summarised_days=60
        )
        assert counts["summarised"] == 1
        c = env["conn_factory"]()
        row = c.execute("SELECT evidence_status FROM runs WHERE id=2").fetchone()
        c.close()
        assert row["evidence_status"] == "summarised"
        # summary.md present + readable.
        summary = (d / "summary.md").read_text()
        assert "Run #2" in summary
        assert "all good" in summary
        # Large transcript gzipped, original gone.
        assert (d / "transcript.txt.gz").exists()
        assert not (d / "transcript.txt").exists()
        # Small log left alone.
        assert (d / "stdout.log").exists()
        # Screenshot left alone (PNG).
        assert (d / "screenshot.png").exists()
        # index.txt enumerates the post-summarise contents.
        idx = (d / "index.txt").read_text().splitlines()
        assert "summary.md" in idx
        assert "transcript.txt.gz" in idx

    def test_very_old_summarised_run_gets_pruned(self, env):
        d = _make_evidence_dir(env["evidence_root"], 3)
        # Pre-state: summarised, summary.md already there.
        (d / "summary.md").write_text("# Run #3\n\nWas summarised earlier.\n")
        _seed_run(env, 3, evidence_status="summarised", days_ago=100, evidence_dir=d)
        counts = env["evidence"].rotate_evidence(
            env["conn_factory"], live_days=30, summarised_days=60
        )
        assert counts["pruned"] == 1
        c = env["conn_factory"]()
        row = c.execute("SELECT evidence_status FROM runs WHERE id=3").fetchone()
        c.close()
        assert row["evidence_status"] == "pruned"
        # Only summary.md remains.
        remaining = sorted(p.name for p in d.iterdir())
        assert remaining == ["summary.md"]

    def test_run_with_no_evidence_dir_still_transitions_status(self, env):
        # Some runs (e.g. AgentRunner that produced no artifacts) have no
        # evidence_dir set. We still want their status to age out cleanly.
        _seed_run(env, 4, evidence_status="live", days_ago=35, evidence_dir=None)
        counts = env["evidence"].rotate_evidence(env["conn_factory"])
        assert counts["summarised"] == 1
        c = env["conn_factory"]()
        row = c.execute("SELECT evidence_status FROM runs WHERE id=4").fetchone()
        c.close()
        assert row["evidence_status"] == "summarised"

    def test_terminal_pruned_runs_are_skipped(self, env):
        # Already-pruned rows should be a no-op (not re-counted).
        _seed_run(env, 5, evidence_status="pruned", days_ago=200, evidence_dir=None)
        counts = env["evidence"].rotate_evidence(env["conn_factory"])
        assert counts["summarised"] == 0
        assert counts["pruned"] == 0
        assert (
            counts["skipped"] == 0
        )  # query excludes pruned, doesn't even surface them

    def test_unfinished_run_is_skipped(self, env):
        # Runs with finished_at = NULL aren't considered.
        c = env["conn_factory"]()
        c.execute(
            "INSERT INTO runs (id, project_id, subject_type, subject_id, runner_kind, "
            " status, started_at, heartbeat_at, evidence_status, triggered_by) "
            "VALUES (6, 'p', 'ticket', 'B-1', 'agent', 'running', '2026-04-29T00:00:00Z', "
            "        '2026-04-29T00:00:00Z', 'live', 'human')"
        )
        c.commit()
        c.close()
        counts = env["evidence"].rotate_evidence(env["conn_factory"])
        assert counts["summarised"] == 0


# ---------------------------------------------------------------------------
# Failure-mode handling
# ---------------------------------------------------------------------------


class TestSummaryContent:
    def test_failed_run_summary_includes_error(self, env):
        d = _make_evidence_dir(env["evidence_root"], 7, with_files=False)
        _seed_run(
            env,
            7,
            evidence_status="live",
            days_ago=35,
            evidence_dir=d,
            summary="",
            error_class="non_zero_exit",
            error_message="exit 7\nstderr text",
        )
        env["evidence"].rotate_evidence(env["conn_factory"])
        text = (d / "summary.md").read_text()
        assert "non_zero_exit" in text
        assert "exit 7" in text


# ---------------------------------------------------------------------------
# Time-travel control with the `now` seam
# ---------------------------------------------------------------------------


class TestNowSeam:
    def test_explicit_now_overrides_wall_clock(self, env):
        d = _make_evidence_dir(env["evidence_root"], 8)
        # Seed with finished_at = "now" wall-clock; pretend now is +200 days.
        _seed_run(env, 8, evidence_status="live", days_ago=0, evidence_dir=d)
        future = datetime.now(timezone.utc) + timedelta(days=200)
        counts = env["evidence"].rotate_evidence(env["conn_factory"], now=future)
        assert counts["summarised"] == 1
        # Re-rotate — should now go straight to pruned.
        counts2 = env["evidence"].rotate_evidence(env["conn_factory"], now=future)
        assert counts2["pruned"] == 1


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------


class TestDaemonLifecycle:
    def test_start_stop_idempotent(self, env):
        env["evidence"].start_rotation_daemon(env["conn_factory"], interval_s=10000)
        env["evidence"].stop_rotation_daemon()
        env["evidence"].stop_rotation_daemon()  # double stop: no-op
