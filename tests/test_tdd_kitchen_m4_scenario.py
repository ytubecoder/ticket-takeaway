"""TDD tests for Kitchen M4 — ScenarioRunner + classify_scenario_failure.

Hermetic. Uses the same env fixture pattern as test_tdd_kitchen_m3_runners.py.
Playwright is never launched — execute_scenario is monkey-patched to return
synthetic RunResult objects, and sync_playwright() is fully patched out.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import types
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Make src/ importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
# Make tests/ importable (scenario_runner lives here).
sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Minimal RunResult stand-in (mirrors the real dataclass from scenario_runner).
# Tests import this rather than scenario_runner so Playwright is not triggered.
# ---------------------------------------------------------------------------


@dataclass
class _RunResult:
    scenario_id: str
    status: str  # "passed" | "failed" | "error"
    duration_ms: int
    failed_step: dict | None = None
    failed_step_index: int | None = None
    screenshots: list[str] = field(default_factory=list)
    error_message: str = ""


# ---------------------------------------------------------------------------
# Fixtures — copied verbatim from test_tdd_kitchen_m3_runners.py
# ---------------------------------------------------------------------------


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
    """Fully-wired temp env: DB, workspace, reloaded modules."""
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

    # Seed a journey (journeys table created by init_db migration 5).
    # Use a lowercase id so compile_to_manifest produces a valid scenario id
    # (validate_manifest requires [a-z0-9][a-z0-9-]* for the id field).
    # actors_json must match the actor column on journey_steps (default "user").
    c = sqlite3.connect(db_file)
    c.execute(
        "INSERT INTO journeys (id, project_id, title, description, actors_json, "
        "seed_json, viewport_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "j-1",
            "p",
            "Test Journey",
            "desc",
            json.dumps({"user": {"label": "User"}}),
            json.dumps({}),
            json.dumps({"width": 1280, "height": 720}),
        ),
    )
    c.execute(
        "INSERT INTO journey_steps (journey_id, project_id, sort_order, action, "
        "target_json, value, actor) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("j-1", "p", 0, "open", json.dumps({}), "/", "user"),
    )
    c.commit()
    c.close()

    ws = workspaces.create_or_reuse(repo, "p", "journey", "j-1")

    def conn_factory():
        c2 = sqlite3.connect(db_file)
        c2.row_factory = sqlite3.Row
        c2.execute("PRAGMA foreign_keys=ON")
        return c2

    return {
        "db_file": db_file,
        "conn_factory": conn_factory,
        "workspaces": workspaces,
        "runners": runners,
        "workspace": ws,
        "repo": repo,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_run(
    env,
    runner_kind="scenario",
    status="queued",
    subject_type="journey",
    subject_id="j-1",
):
    c = env["conn_factory"]()
    cur = c.execute(
        "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, "
        "status, triggered_by) VALUES ('p', ?, ?, ?, ?, 'human')",
        (subject_type, subject_id, runner_kind, status),
    )
    rid = cur.lastrowid
    c.commit()
    c.close()
    return rid


def _run_row(env, run_id):
    c = env["conn_factory"]()
    row = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    c.close()
    return row


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


def _make_manifest(step_count: int = 3, manifest_id: str = "journey-j-1") -> dict:
    """Build a minimal valid-looking manifest dict for classifier tests."""
    steps = [{"action": "open", "path": "/"}]
    steps += [
        {"action": "click", "target": {"testid": f"btn-{i}"}}
        for i in range(step_count - 1)
    ]
    return {
        "id": manifest_id,
        "title": "Test Journey",
        "tags": ["journey"],
        "actors": {"default": {"browser": "chromium"}},
        "seed": {},
        "steps": steps,
    }


def _passed_result() -> _RunResult:
    return _RunResult(
        scenario_id="journey-j-1",
        status="passed",
        duration_ms=100,
        screenshots=[],
    )


def _failed_result(
    action: str = "click",
    target: dict | None = None,
    step_index: int = 1,
    error_message: str = "Element not found",
    screenshots: list[str] | None = None,
) -> _RunResult:
    step: dict[str, Any] = {"action": action}
    if target:
        step["target"] = target
    return _RunResult(
        scenario_id="journey-j-1",
        status="failed",
        duration_ms=200,
        failed_step=step,
        failed_step_index=step_index,
        error_message=error_message,
        screenshots=screenshots or [],
    )


def _error_result(error_message: str = "crash") -> _RunResult:
    return _RunResult(
        scenario_id="journey-j-1",
        status="error",
        duration_ms=50,
        error_message=error_message,
    )


def _build_sys_modules_patch(fake_execute_scenario):
    """Return a sys.modules override dict that fakes playwright + scenario_runner."""
    fake_sr_module = types.ModuleType("scenario_runner")
    fake_sr_module.execute_scenario = fake_execute_scenario
    fake_sr_module.ScenarioContext = MagicMock(side_effect=lambda *a, **kw: MagicMock())
    fake_sr_module.RunResult = _RunResult

    mock_browser = MagicMock()
    mock_pw_cm = MagicMock()
    mock_pw_cm.__enter__ = MagicMock(return_value=mock_pw_cm)
    mock_pw_cm.__exit__ = MagicMock(return_value=False)
    mock_pw_cm.chromium.launch.return_value = mock_browser

    fake_pw_sync_api = types.ModuleType("playwright.sync_api")
    fake_pw_sync_api.sync_playwright = MagicMock(return_value=mock_pw_cm)
    fake_playwright = types.ModuleType("playwright")

    return {
        "scenario_runner": fake_sr_module,
        "playwright": fake_playwright,
        "playwright.sync_api": fake_pw_sync_api,
    }


# ---------------------------------------------------------------------------
# Unit tests for classify_scenario_failure
# ---------------------------------------------------------------------------


class TestClassifyScenarioFailure:
    """Pure-logic tests — no DB, no Playwright."""

    @pytest.fixture(autouse=True)
    def import_classifier(self):
        import runners

        importlib.reload(runners)
        self.classify = runners.classify_scenario_failure

    def test_engine_error_returns_test_harness_gap(self):
        result = _error_result("playwright crashed")
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "test_harness_gap"
        assert gap["error_message"] == "playwright crashed"
        assert gap["step_count"] == 3

    def test_no_failed_step_returns_ambiguous_goal(self):
        result = _RunResult(
            scenario_id="j",
            status="failed",
            duration_ms=10,
            failed_step=None,
            failed_step_index=None,
            error_message="",
        )
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "ambiguous_goal"

    def test_ambiguous_in_error_message_returns_ambiguous_goal(self):
        result = _failed_result(
            action="click",
            target={"testid": "x"},
            error_message="ambiguous selector matched multiple elements",
        )
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "ambiguous_goal"

    def test_open_action_returns_missing_screen(self):
        result = _failed_result(
            action="open", target=None, error_message="404 page not found"
        )
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "missing_screen"

    def test_open_action_without_http_error_still_missing_screen(self):
        result = _failed_result(
            action="open", target=None, error_message="navigation failed"
        )
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "missing_screen"

    def test_assert_visible_returns_missing_feature(self):
        result = _failed_result(
            action="assert_visible",
            target={"testid": "save-btn"},
            error_message="element is not visible",
        )
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "missing_feature"

    def test_wait_for_timeout_with_selector_target_returns_missing_selector(self):
        result = _failed_result(
            action="wait_for",
            target={"testid": "results-panel"},
            error_message="Timeout 10000ms exceeded",
        )
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "missing_selector"

    def test_click_with_css_selector_timeout_returns_missing_selector(self):
        result = _failed_result(
            action="click",
            target={"css": ".my-button"},
            error_message="Timeout 5000ms exceeded waiting for element",
        )
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "missing_selector"

    def test_network_error_with_selector_target_returns_missing_selector(self):
        # net::err is in _TIMEOUT_PHRASES; role is a selector key → missing_selector wins.
        result = _failed_result(
            action="fill",
            target={"role": "textbox", "name": "Email"},
            error_message="net::ERR_CONNECTION_REFUSED",
        )
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "missing_selector"

    def test_timeout_without_selector_target_returns_external_dependency(self):
        # title is NOT a selector key.
        result = _failed_result(
            action="fill",
            target={"title": "My Ticket"},
            error_message="Timeout waiting for server response",
        )
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "external_dependency"

    def test_gap_report_contains_required_fields(self):
        manifest = _make_manifest(step_count=5, manifest_id="journey-abc")
        result = _error_result("boom")
        gap = self.classify(result, manifest)
        assert gap["manifest_id"] == "journey-abc"
        assert gap["step_count"] == 5
        assert gap["failed_step_index"] is None
        assert gap["failed_step_action"] is None
        assert gap["failed_step_target"] is None
        assert gap["screenshot_path"] is None

    def test_screenshot_path_populated_from_failure_screenshot(self):
        result = _failed_result(
            action="click",
            target={"testid": "x"},
            error_message="not found",
            screenshots=["/tmp/FAILURE-step-01.png"],
        )
        gap = self.classify(result, _make_manifest())
        assert gap["screenshot_path"] == "/tmp/FAILURE-step-01.png"

    def test_fallback_returns_missing_feature(self):
        # press action with a non-selector target, no timeout keywords.
        result = _failed_result(
            action="press",
            target={"title": "My Ticket"},
            error_message="some random error",
        )
        gap = self.classify(result, _make_manifest())
        assert gap["gap_kind"] == "missing_feature"


# ---------------------------------------------------------------------------
# Integration tests for ScenarioRunner.execute() — Playwright fully patched.
# ---------------------------------------------------------------------------


class TestScenarioRunnerLifecycle:
    """Test status transitions and event emission with mocked Playwright + execute_scenario."""

    @pytest.fixture(autouse=True)
    def reload_runners(self, env):
        """Ensure a fresh runners module for each test."""
        import runners

        importlib.reload(runners)
        self.env = env
        self.runners = runners

    def _execute_with_patches(self, run_result, subject_id="j-1", cancel_event=None):
        """Patch engine + Playwright, run ScenarioRunner.execute(), return (run_id, outcome).

        Strategy: populate sys.modules["scenario_runner"] with a fake module so
        that the late `from scenario_runner import ...` inside execute() resolves
        to our mock, and replace playwright.sync_api in sys.modules so that
        `from playwright.sync_api import sync_playwright` gives us a mock too.
        """
        import runners

        def fake_execute_scenario(ctx):
            if run_result.status == "passed":
                return run_result
            exc = RuntimeError(run_result.error_message or "step failed")
            exc.__run_result__ = run_result  # type: ignore[attr-defined]
            raise exc

        modules_override = _build_sys_modules_patch(fake_execute_scenario)

        run_id = _new_run(
            self.env,
            runner_kind="scenario",
            subject_type="journey",
            subject_id=subject_id,
        )
        config = {"scenario": {"base_url": "http://localhost:8787/p"}}

        with patch.dict("sys.modules", modules_override):
            outcome = runners.ScenarioRunner().execute(
                run_id,
                "p",
                "journey",
                subject_id,
                self.env["workspace"],
                config,
                self.env["conn_factory"],
                cancel_event=cancel_event,
            )
        return run_id, outcome

    def test_successful_run_transitions_to_succeeded(self):
        run_id, outcome = self._execute_with_patches(_passed_result())
        assert outcome.final_status == "succeeded"
        row = _run_row(self.env, run_id)
        assert row["status"] == "succeeded"
        assert row["finished_at"] is not None

    def test_successful_run_summary_includes_step_count(self):
        run_id, outcome = self._execute_with_patches(_passed_result())
        assert "step" in outcome.summary
        row = _run_row(self.env, run_id)
        assert "step" in (row["summary"] or "")

    def test_successful_run_emits_workspace_created_and_run_succeeded(self):
        run_id, outcome = self._execute_with_patches(_passed_result())
        ws_events = _events(self.env, run_id=run_id, kind="workspace_created")
        succ_events = _events(self.env, run_id=run_id, kind="run_succeeded")
        assert len(ws_events) == 1
        assert len(succ_events) == 1
        assert succ_events[0]["actor_type"] == "agent"

    def test_successful_run_emits_run_succeeded_with_duration(self):
        run_id, outcome = self._execute_with_patches(_passed_result())
        rows = _events(self.env, run_id=run_id, kind="run_succeeded")
        payload = json.loads(rows[0]["payload_json"])
        assert "duration_ms" in payload
        assert payload["run_id"] == run_id

    def test_failed_run_transitions_to_failed(self):
        result = _failed_result(
            action="click", target={"testid": "x"}, error_message="not found"
        )
        run_id, outcome = self._execute_with_patches(result)
        assert outcome.final_status == "failed"
        row = _run_row(self.env, run_id)
        assert row["status"] == "failed"
        assert row["error_class"] == "scenario_step_failed"

    def test_failed_run_stores_gap_report_in_metadata_json(self):
        result = _failed_result(
            action="click",
            target={"testid": "btn"},
            error_message="element not visible",
        )
        run_id, outcome = self._execute_with_patches(result)
        row = _run_row(self.env, run_id)
        meta = json.loads(row["metadata_json"] or "{}")
        assert "gap_report" in meta
        gr = meta["gap_report"]
        assert "gap_kind" in gr
        assert gr["manifest_id"].startswith("journey-")

    def test_failed_run_emits_run_failed_event(self):
        result = _failed_result(action="open", error_message="404")
        run_id, outcome = self._execute_with_patches(result)
        rows = _events(self.env, run_id=run_id, kind="run_failed")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["error_class"] == "scenario_step_failed"

    def test_error_result_stored_as_scenario_error(self):
        result = _error_result("playwright launch error: binary not found")
        run_id, outcome = self._execute_with_patches(result)
        assert outcome.final_status == "failed"
        row = _run_row(self.env, run_id)
        assert row["error_class"] == "scenario_error"
        meta = json.loads(row["metadata_json"] or "{}")
        assert meta["gap_report"]["gap_kind"] == "test_harness_gap"

    def test_cancel_before_playwright_marks_cancelled(self):
        ev = threading.Event()
        ev.set()  # cancel before execute touches browser
        run_id, outcome = self._execute_with_patches(_passed_result(), cancel_event=ev)
        assert outcome.final_status == "cancelled"
        row = _run_row(self.env, run_id)
        assert row["status"] == "cancelled"
        rows = _events(self.env, run_id=run_id, kind="run_cancelled")
        assert len(rows) == 1

    def test_cancel_emits_run_cancelled_with_agent_actor(self):
        ev = threading.Event()
        ev.set()
        run_id, outcome = self._execute_with_patches(_passed_result(), cancel_event=ev)
        rows = _events(self.env, run_id=run_id, kind="run_cancelled")
        assert rows[0]["actor_type"] == "agent"
        assert rows[0]["actor_id"] == str(run_id)

    def test_gap_report_missing_selector_on_wait_for_timeout(self):
        result = _failed_result(
            action="wait_for",
            target={"testid": "panel"},
            error_message="Timeout 10000ms exceeded",
        )
        run_id, outcome = self._execute_with_patches(result)
        row = _run_row(self.env, run_id)
        meta = json.loads(row["metadata_json"])
        assert meta["gap_report"]["gap_kind"] == "missing_selector"

    def test_gap_report_missing_screen_on_open_action(self):
        result = _failed_result(
            action="open", target=None, error_message="404 Not Found"
        )
        run_id, outcome = self._execute_with_patches(result)
        row = _run_row(self.env, run_id)
        meta = json.loads(row["metadata_json"])
        assert meta["gap_report"]["gap_kind"] == "missing_screen"

    def test_successful_run_has_no_gap_report(self):
        """Successful runs should NOT have a gap_report in metadata_json."""
        run_id, outcome = self._execute_with_patches(_passed_result())
        row = _run_row(self.env, run_id)
        meta = json.loads(row["metadata_json"] or "{}")
        assert "gap_report" not in meta

    def test_workspace_created_event_has_path(self):
        run_id, outcome = self._execute_with_patches(_passed_result())
        rows = _events(self.env, run_id=run_id, kind="workspace_created")
        payload = json.loads(rows[0]["payload_json"])
        assert "path" in payload
        assert "reused" in payload
