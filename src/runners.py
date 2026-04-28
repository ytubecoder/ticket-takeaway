"""Runner abstraction — agent / scenario / gap-analyzer.

See docs/KITCHEN.md §10 + §15 (M3). Each runner takes a `runs` row that the
orchestrator already inserted at status='queued', a WorkspaceInfo, and the
parsed WORKFLOW.toml config dict; executes the work; updates the run row
through its lifecycle (queued → preparing → running → terminal); and emits
activity_events with actor=ActorContext.agent(run_id) along the way.

A Runner is responsible for ALL of its run row's transitions and for cleanup.
The orchestrator reconciles based on heartbeat_at if a runner crashes mid-run.

For M3 the AgentRunner is the only fully-implemented subclass; ScenarioRunner
and GapAnalyzer are stubs that future milestones flesh out.
"""

from __future__ import annotations

import shlex
import sqlite3
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from contextlib import contextmanager

from actions import ActorContext, emit_event, utcnow_iso
from workspaces import HookResult, run_hook, WorkspaceInfo, mark_bootstrapped


# ---------------------------------------------------------------------------
# Connection helper — sqlite3.Connection.__exit__ commits but doesn't close,
# so we wrap it to ensure both happen.
# ---------------------------------------------------------------------------

@contextmanager
def db_session(conn_factory: Callable[[], sqlite3.Connection]):
    """Open a connection via the factory, commit on success, always close."""
    conn = conn_factory()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Run row helpers — shared status-transition + heartbeat plumbing.
# ---------------------------------------------------------------------------

def _set_run_status(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    *,
    error_class: Optional[str] = None,
    error_message: Optional[str] = None,
    summary: Optional[str] = None,
    workspace_path: Optional[str] = None,
    finished: bool = False,
    exit_code: Optional[int] = None,
) -> None:
    """Atomic-ish status flip. Caller commits."""
    fields = ["status = ?", "heartbeat_at = ?"]
    args: list = [status, utcnow_iso()]
    if error_class is not None:
        fields.append("error_class = ?"); args.append(error_class)
    if error_message is not None:
        fields.append("error_message = ?"); args.append(error_message)
    if summary is not None:
        fields.append("summary = ?"); args.append(summary)
    if workspace_path is not None:
        fields.append("workspace_path = ?"); args.append(workspace_path)
    if exit_code is not None:
        fields.append("exit_code = ?"); args.append(exit_code)
    if finished:
        now = utcnow_iso()
        fields.append("finished_at = ?"); args.append(now)
    args.append(run_id)
    conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id = ?", args)


def _heartbeat(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (utcnow_iso(), run_id))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RunOutcome:
    """Returned by Runner.execute() for the orchestrator to log/observe.

    The runner has already written the terminal status to the DB by this point;
    the outcome is informational.
    """
    run_id: int
    final_status: str         # succeeded | failed | stalled | cancelled
    duration_ms: int
    summary: str
    error_class: Optional[str] = None
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Runner ABC
# ---------------------------------------------------------------------------

class Runner(ABC):
    """Common base for every kind of run executor.

    Each concrete runner picks up a row already INSERTed at status='queued'
    by the orchestrator's claim transaction, advances it through preparing →
    running → terminal, and returns a RunOutcome. The runner uses the
    `conn_factory` to open short-lived DB connections (so it doesn't hold a
    connection across the long subprocess wait).
    """

    runner_kind: str = ""  # 'agent' | 'scenario' | 'gap_analyzer' (subclass sets)

    @abstractmethod
    def execute(
        self,
        run_id: int,
        project_id: str,
        subject_type: str,
        subject_id: str,
        workspace: WorkspaceInfo,
        config: dict,
        conn_factory: Callable[[], sqlite3.Connection],
        cancel_event=None,
    ) -> RunOutcome:
        """Run the work to completion. Updates the run row through transitions.

        cancel_event (threading.Event) — if set, the runner should stop ASAP
        and transition the run to 'cancelled'.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# AgentRunner — subprocess to whatever agent.command is configured
# ---------------------------------------------------------------------------

class AgentRunner(Runner):
    """Runs the configured `agent.command` as a subprocess in the workspace.

    The runner is intentionally agnostic about which CLI is used. The command
    is taken verbatim from WORKFLOW.toml's `[agent].command` and split with
    shlex. The prompt is read from PROMPT.md (already loaded into config as
    config['_prompt_template']) with a small token substitution and piped to
    the subprocess on stdin.

    A run goes:
        queued → preparing  (after claim, before hooks)
        preparing → running (after before_run hook + subprocess spawned)
        running → terminal  (subprocess exit)

    Hook failure semantics per Symphony §9.4 / docs/KITCHEN.md §10:
        after_create failure → fatal (run fails with error_class='hook_after_create')
        before_run failure → fatal (error_class='hook_before_run')
        after_run failure → logged, ignored
    """
    runner_kind = "agent"

    DEFAULT_TIMEOUT_S = 1800  # 30 min — orchestrator stall_timeout supersedes for hung subprocesses

    def execute(
        self,
        run_id: int,
        project_id: str,
        subject_type: str,
        subject_id: str,
        workspace: WorkspaceInfo,
        config: dict,
        conn_factory: Callable[[], sqlite3.Connection],
        cancel_event=None,
    ) -> RunOutcome:
        actor = ActorContext.agent(run_id)
        started = time.monotonic()
        agent_cfg = config.get("agent", {})
        hooks_cfg = config.get("hooks", {})
        hook_timeout_ms = int(hooks_cfg.get("timeout_ms", 60000))
        prompt_template = config.get("_prompt_template", "")

        # ── Phase 1: preparing ── workspace exists, run hooks ─────────────
        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "preparing", workspace_path=str(workspace.path))
            emit_event(conn, project_id, subject_type, subject_id, "workspace_created",
                       {"path": str(workspace.path), "reused": not workspace.created_now}, actor)
            conn.commit()

        # after_create — only on first bootstrap, marker-guarded.
        if not workspace.bootstrapped and hooks_cfg.get("after_create"):
            r = self._run_hook(workspace.path, "after_create", hooks_cfg["after_create"],
                               hook_timeout_ms, run_id, project_id, subject_type, subject_id,
                               actor, conn_factory)
            if r is not None and not r.succeeded:
                return self._fail(run_id, project_id, subject_type, subject_id, actor,
                                  conn_factory, started, "hook_after_create",
                                  r.stderr or r.stdout or "after_create failed")
            mark_bootstrapped(workspace.path)

        # before_run — every attempt.
        if hooks_cfg.get("before_run"):
            r = self._run_hook(workspace.path, "before_run", hooks_cfg["before_run"],
                               hook_timeout_ms, run_id, project_id, subject_type, subject_id,
                               actor, conn_factory)
            if r is not None and not r.succeeded:
                return self._fail(run_id, project_id, subject_type, subject_id, actor,
                                  conn_factory, started, "hook_before_run",
                                  r.stderr or r.stdout or "before_run failed")

        # Cancellation check before launching the agent.
        if cancel_event is not None and cancel_event.is_set():
            return self._cancel(run_id, project_id, subject_type, subject_id, actor,
                                conn_factory, started)

        # ── Phase 2: running ── spawn the agent subprocess ────────────────
        cmd_str = (agent_cfg.get("command") or "").strip()
        if not cmd_str:
            return self._fail(run_id, project_id, subject_type, subject_id, actor,
                              conn_factory, started, "missing_command",
                              "WORKFLOW.toml [agent].command is empty")

        prompt = self._render_prompt(prompt_template, subject_id)

        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "running")
            conn.commit()

        try:
            cmd = shlex.split(cmd_str)
        except ValueError as e:
            return self._fail(run_id, project_id, subject_type, subject_id, actor,
                              conn_factory, started, "bad_command", str(e))

        try:
            r = subprocess.run(
                cmd,
                cwd=str(workspace.path),
                input=prompt,
                capture_output=True, text=True,
                timeout=self.DEFAULT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            return self._fail(run_id, project_id, subject_type, subject_id, actor,
                              conn_factory, started, "timeout",
                              f"agent timed out after {self.DEFAULT_TIMEOUT_S}s",
                              stdout_for_summary=(e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")))
        except FileNotFoundError as e:
            return self._fail(run_id, project_id, subject_type, subject_id, actor,
                              conn_factory, started, "agent_not_found", str(e))
        except (OSError, subprocess.SubprocessError) as e:
            return self._fail(run_id, project_id, subject_type, subject_id, actor,
                              conn_factory, started, "subprocess_error", str(e))

        # Cancellation flagged while we were running — honor it.
        if cancel_event is not None and cancel_event.is_set():
            return self._cancel(run_id, project_id, subject_type, subject_id, actor,
                                conn_factory, started)

        # ── Phase 3: terminal ─────────────────────────────────────────────
        elapsed_ms = int((time.monotonic() - started) * 1000)
        stdout_tail = self._tail(r.stdout)
        if r.returncode == 0:
            with db_session(conn_factory) as conn:
                summary = stdout_tail or "agent completed"
                _set_run_status(conn, run_id, "succeeded",
                                summary=summary, exit_code=r.returncode, finished=True)
                emit_event(conn, project_id, subject_type, subject_id, "agent_output",
                           {"run_id": run_id, "summary": summary}, actor)
                emit_event(conn, project_id, subject_type, subject_id, "run_succeeded",
                           {"run_id": run_id, "summary": summary, "duration_ms": elapsed_ms}, actor)
                conn.commit()
            self._maybe_after_run(workspace, hooks_cfg, hook_timeout_ms, run_id,
                                  project_id, subject_type, subject_id, actor, conn_factory)
            return RunOutcome(run_id=run_id, final_status="succeeded",
                              duration_ms=elapsed_ms, summary=summary)

        # Non-zero exit → failed.
        err_msg = self._tail(r.stderr) or stdout_tail or f"exit {r.returncode}"
        return self._fail(run_id, project_id, subject_type, subject_id, actor,
                          conn_factory, started, "non_zero_exit", err_msg,
                          exit_code=r.returncode, stdout_for_summary=stdout_tail)

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _render_prompt(template: str, subject_id: str) -> str:
        """Tiny placeholder substitution. Future M-versions can use Jinja."""
        if not template:
            return ""
        return template.replace("{{subject.id}}", subject_id)

    @staticmethod
    def _tail(text: str, max_chars: int = 1000) -> str:
        if not text:
            return ""
        text = text.strip()
        if len(text) <= max_chars:
            return text
        return "…" + text[-(max_chars - 1):]

    def _run_hook(self, workspace_path, name, script, timeout_ms,
                  run_id, project_id, subject_type, subject_id, actor, conn_factory) -> Optional[HookResult]:
        with db_session(conn_factory) as conn:
            emit_event(conn, project_id, subject_type, subject_id, "hook_started",
                       {"hook": name, "run_id": run_id}, actor)
            conn.commit()
        try:
            result = run_hook(workspace_path, name, script, timeout_ms=timeout_ms)
        except Exception as e:
            with db_session(conn_factory) as conn:
                emit_event(conn, project_id, subject_type, subject_id, "hook_failed",
                           {"hook": name, "run_id": run_id, "error_class": "hook_exception",
                            "error_message": str(e)}, actor)
                conn.commit()
            return HookResult(hook=name, exit_code=-1, stdout="", stderr=str(e),
                              duration_ms=0, timed_out=False)
        with db_session(conn_factory) as conn:
            if result.succeeded:
                emit_event(conn, project_id, subject_type, subject_id, "hook_succeeded",
                           {"hook": name, "run_id": run_id, "duration_ms": result.duration_ms}, actor)
            else:
                emit_event(conn, project_id, subject_type, subject_id, "hook_failed",
                           {"hook": name, "run_id": run_id,
                            "error_class": "timeout" if result.timed_out else "non_zero_exit",
                            "error_message": (result.stderr or result.stdout or "")[:500]}, actor)
            conn.commit()
        return result

    def _maybe_after_run(self, workspace, hooks_cfg, hook_timeout_ms, run_id,
                         project_id, subject_type, subject_id, actor, conn_factory):
        """Run after_run hook on terminal success. Failure is logged, not fatal."""
        if not hooks_cfg.get("after_run"):
            return
        # Best-effort; we don't fail the (already-succeeded) run if this fails.
        self._run_hook(workspace.path, "after_run", hooks_cfg["after_run"],
                       hook_timeout_ms, run_id, project_id, subject_type, subject_id,
                       actor, conn_factory)

    def _fail(self, run_id, project_id, subject_type, subject_id, actor,
              conn_factory, started, error_class, error_message,
              exit_code=None, stdout_for_summary=""):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        summary = stdout_for_summary or error_message[:200] if error_message else "agent failed"
        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "failed",
                            error_class=error_class, error_message=error_message,
                            summary=summary, exit_code=exit_code, finished=True)
            emit_event(conn, project_id, subject_type, subject_id, "run_failed",
                       {"run_id": run_id, "error_class": error_class,
                        "error_message": error_message[:500]}, actor)
            conn.commit()
        return RunOutcome(run_id=run_id, final_status="failed",
                          duration_ms=elapsed_ms, summary=summary,
                          error_class=error_class, error_message=error_message)

    def _cancel(self, run_id, project_id, subject_type, subject_id, actor,
                conn_factory, started):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "cancelled",
                            summary="cancelled by user", finished=True)
            emit_event(conn, project_id, subject_type, subject_id, "run_cancelled",
                       {"run_id": run_id}, actor)
            conn.commit()
        return RunOutcome(run_id=run_id, final_status="cancelled",
                          duration_ms=elapsed_ms, summary="cancelled by user")


# ---------------------------------------------------------------------------
# ScenarioRunner / GapAnalyzer — stubs for M4 to flesh out.
# ---------------------------------------------------------------------------

class ScenarioRunner(Runner):
    runner_kind = "scenario"

    def execute(self, run_id, project_id, subject_type, subject_id,
                workspace, config, conn_factory, cancel_event=None):
        raise NotImplementedError("ScenarioRunner lands in M4 (closed loop)")


class GapAnalyzer(Runner):
    runner_kind = "gap_analyzer"

    def execute(self, run_id, project_id, subject_type, subject_id,
                workspace, config, conn_factory, cancel_event=None):
        raise NotImplementedError("GapAnalyzer lands in M4 (closed loop)")
