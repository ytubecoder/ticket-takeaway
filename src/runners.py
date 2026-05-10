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

import json
import logging
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

_log = logging.getLogger(__name__)


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
# Interactive-marker helpers
# ---------------------------------------------------------------------------

def _try_parse_marker(line: str) -> Optional[dict]:
    """Attempt to parse a line as an interactive marker JSON object.

    Returns the parsed dict if the line is a valid JSON object with an 'ask'
    or 'propose' key at top level.  Returns None otherwise.  Never raises.
    """
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict) and ("ask" in obj or "propose" in obj):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _try_parse_handoff(text: str) -> dict:
    """Scan the last 50 lines of text for the most recent valid handoff JSON.

    A handoff object must be a JSON dict with at least one of:
        implemented, undone, commands, issues, procedures_followed

    Returns the parsed dict (possibly with only a subset of keys), or {} if
    no valid handoff object was found.  Never raises.
    """
    _HANDOFF_KEYS = frozenset(["implemented", "undone", "commands", "issues", "procedures_followed"])
    lines = (text or "").splitlines()
    for line in reversed(lines[-50:]):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict) and (_HANDOFF_KEYS & set(obj.keys())):
                # Normalise: missing keys → empty arrays / null
                return {
                    "implemented":          obj.get("implemented") or [],
                    "undone":               obj.get("undone") or [],
                    "commands":             obj.get("commands") or [],
                    "issues":               obj.get("issues") or [],
                    "procedures_followed":  obj.get("procedures_followed") or [],
                }
        except (json.JSONDecodeError, ValueError):
            continue
    return {}


def _read_run_metadata(conn: sqlite3.Connection, run_id: int) -> dict:
    """Read and parse runs.metadata_json for a run.  Returns {} on any error."""
    row = conn.execute("SELECT metadata_json FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["metadata_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _write_run_metadata(conn: sqlite3.Connection, run_id: int, meta: dict) -> None:
    """Serialise and persist runs.metadata_json."""
    conn.execute(
        "UPDATE runs SET metadata_json = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), run_id),
    )


def _append_chat_entry(
    conn: sqlite3.Connection, run_id: int, role: str, content: str
) -> None:
    """Append a chat entry to runs.metadata_json.chat[]."""
    meta = _read_run_metadata(conn, run_id)
    chat = meta.get("chat") or []
    chat.append({"role": role, "content": content})
    meta["chat"] = chat
    _write_run_metadata(conn, run_id, meta)


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

        # For DB-workflow path: render ticket fields into the prompt template.
        workflow_meta = config.get("_workflow_meta")
        if workflow_meta and subject_type == "ticket":
            prompt = self._render_prompt_with_ticket(
                prompt_template, subject_id, project_id, conn_factory
            )
        else:
            prompt = self._render_prompt(prompt_template, subject_id)

        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "running")
            conn.commit()

        try:
            cmd = shlex.split(cmd_str)
        except ValueError as e:
            return self._fail(run_id, project_id, subject_type, subject_id, actor,
                              conn_factory, started, "bad_command", str(e))

        # Determine whether this agent has persist_session enabled (for resume support).
        agent_persist_session = bool(agent_cfg.get("persist_session", 0))

        # Build stdin: prior conversation (for resume) + current prompt.
        stdin_text = self._build_stdin(run_id, prompt, conn_factory)

        try:
            r = subprocess.run(
                cmd,
                cwd=str(workspace.path),
                input=stdin_text,
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

        stdout_text = r.stdout or ""

        # ── Interactive marker detection ──────────────────────────────────
        # Scan stdout lines for the LAST interactive marker.  The marker takes
        # precedence over the exit code — an agent that emits a marker should
        # also exit 0, but we detect the marker regardless of exit code.
        detected_marker: Optional[dict] = None
        non_marker_lines: list[str] = []
        for line in stdout_text.splitlines():
            m = _try_parse_marker(line)
            if m is not None:
                detected_marker = m
                non_marker_lines = []  # reset — everything before is "context"
            else:
                non_marker_lines.append(line)

        if detected_marker is not None:
            # Transition to needs_input and yield.
            marker_kind = "propose" if "propose" in detected_marker else "text"
            context_so_far = "\n".join(non_marker_lines).strip()
            marker_payload = json.dumps(detected_marker, ensure_ascii=False)
            with db_session(conn_factory) as conn:
                # Record agent's last output in the chat log.
                if context_so_far:
                    _append_chat_entry(conn, run_id, "agent", context_so_far)
                # Record the marker itself.
                _append_chat_entry(conn, run_id, "agent_marker", marker_payload)
                # Transition run to needs_input.
                conn.execute(
                    "UPDATE runs SET status = ?, needs_input_kind = ?, "
                    "needs_input_prompt = ?, heartbeat_at = ? WHERE id = ?",
                    ("needs_input", marker_kind, marker_payload, utcnow_iso(), run_id),
                )
                conn.commit()
            # Return a pseudo-outcome that tells the caller this run is paused.
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return RunOutcome(
                run_id=run_id,
                final_status="needs_input",
                duration_ms=elapsed_ms,
                summary=f"waiting for {marker_kind} input",
            )

        # ── Phase 3: terminal ─────────────────────────────────────────────
        elapsed_ms = int((time.monotonic() - started) * 1000)
        stdout_tail = self._tail(stdout_text)
        if r.returncode == 0:
            with db_session(conn_factory) as conn:
                summary = stdout_tail or "agent completed"
                _set_run_status(conn, run_id, "succeeded",
                                summary=summary, exit_code=r.returncode, finished=True)
                # Append final agent output to chat log.
                if stdout_tail:
                    _append_chat_entry(conn, run_id, "agent", stdout_tail)
                # Parse and store handoff JSON if present.
                handoff = _try_parse_handoff(stdout_text)
                if handoff:
                    meta = _read_run_metadata(conn, run_id)
                    meta["handoff"] = handoff
                    _write_run_metadata(conn, run_id, meta)
                    emit_event(conn, project_id, subject_type, subject_id, "handoff_recorded",
                               {"run_id": run_id, "handoff": handoff}, actor)
                emit_event(conn, project_id, subject_type, subject_id, "agent_output",
                           {"run_id": run_id, "summary": summary}, actor)
                emit_event(conn, project_id, subject_type, subject_id, "run_succeeded",
                           {"run_id": run_id, "summary": summary, "duration_ms": elapsed_ms}, actor)
                conn.commit()
            self._maybe_after_run(workspace, hooks_cfg, hook_timeout_ms, run_id,
                                  project_id, subject_type, subject_id, actor, conn_factory)
            # Apply on_success actions for DB-workflow path (Phase 2: single-step only).
            # TODO(phase-3): multi-step chaining — after step_index completes, advance
            # to step_index+1 and dispatch a follow-on run instead of applying on_success.
            if workflow_meta and subject_type == "ticket":
                self._apply_on_success(
                    workflow_meta, project_id, subject_id, actor, conn_factory,
                    stdout_text=stdout_text,
                )
            return RunOutcome(run_id=run_id, final_status="succeeded",
                              duration_ms=elapsed_ms, summary=summary)

        # Non-zero exit → failed.
        err_msg = self._tail(r.stderr) or stdout_tail or f"exit {r.returncode}"
        # Still try handoff parsing on failed runs — agent may have partially succeeded.
        with db_session(conn_factory) as conn:
            handoff = _try_parse_handoff(stdout_text)
            if handoff:
                meta = _read_run_metadata(conn, run_id)
                meta["handoff"] = handoff
                _write_run_metadata(conn, run_id, meta)
                conn.commit()
        return self._fail(run_id, project_id, subject_type, subject_id, actor,
                          conn_factory, started, "non_zero_exit", err_msg,
                          exit_code=r.returncode, stdout_for_summary=stdout_tail)

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_stdin(
        run_id: int,
        prompt: str,
        conn_factory: Callable[[], sqlite3.Connection],
    ) -> str:
        """Build the stdin payload for the agent subprocess.

        For a fresh run: just the prompt.
        For a resumed run (chat history exists): reconstruct conversation from
        metadata_json.chat[] and append the prompt as the latest user turn.

        The format is a plain-text transcript that most Claude-compatible CLIs
        can interpret as conversation context.
        """
        try:
            conn = conn_factory()
            try:
                meta = _read_run_metadata(conn, run_id)
            finally:
                conn.close()
        except Exception:
            meta = {}

        chat = meta.get("chat") or []
        if not chat:
            return prompt

        # Reconstruct conversation as a simple text block so the agent sees
        # prior context when resumed.  CLIs that support --continue / session
        # resumption use a different mechanism; this covers the stdin path.
        lines = []
        for entry in chat:
            role = entry.get("role", "agent")
            content = entry.get("content", "")
            label = "User" if role == "user" else "Assistant"
            lines.append(f"[{label}]\n{content}\n")
        lines.append(f"[User]\n{prompt}\n")
        return "\n".join(lines)

    @classmethod
    def resume_with_response(
        cls,
        run_id: int,
        response_payload: dict,
        project_id: str,
        subject_type: str,
        subject_id: str,
        workspace: WorkspaceInfo,
        config: dict,
        conn_factory: Callable[[], sqlite3.Connection],
        cancel_event=None,
    ) -> RunOutcome:
        """Resume a needs_input run after the user has provided a response.

        response_payload shapes:
          text kind:    {"kind": "text", "response": "<user reply>"}
          propose kind: {"kind": "propose", "accepted": {...fields...}}

        For 'propose' kind: applies accepted parts to the ticket BEFORE re-launching
        the agent, so the agent's next turn sees the ticket in its updated state.

        The existing run row is reused (same run_id).  The conversation history in
        metadata_json.chat[] is preserved and the response is appended, so the
        agent subprocess receives the full conversation on stdin.
        """
        actor = ActorContext.agent(run_id)
        started = time.monotonic()
        kind = response_payload.get("kind", "text")

        # Apply accepted proposal fields before re-launching the agent.
        if kind == "propose":
            accepted = response_payload.get("accepted") or {}
            cls._apply_proposal_to_ticket(
                run_id, accepted, project_id, subject_id, actor, conn_factory
            )

        # Build the resume prompt from the response.
        if kind == "text":
            user_response_text = str(response_payload.get("response") or "")
        elif kind == "propose":
            accepted = response_payload.get("accepted") or {}
            # Summarise what was accepted so the agent knows.
            parts = []
            if accepted.get("description"):
                parts.append("description update accepted")
            if accepted.get("add_criteria"):
                parts.append(f"added criteria: {accepted['add_criteria']}")
            if accepted.get("remove_criteria"):
                parts.append(f"removed criteria: {accepted['remove_criteria']}")
            if accepted.get("add_tags"):
                parts.append(f"added tags: {accepted['add_tags']}")
            if accepted.get("remove_tags"):
                parts.append(f"removed tags: {accepted['remove_tags']}")
            user_response_text = (
                "User accepted: " + ("; ".join(parts) if parts else "nothing")
                if accepted else "User declined the proposal."
            )
        else:
            user_response_text = str(response_payload.get("response") or "")

        # Append the user response to the chat log.
        with db_session(conn_factory) as conn:
            _append_chat_entry(conn, run_id, "user", user_response_text)
            # Reset needs_input state so the run can restart.
            conn.execute(
                "UPDATE runs SET status = ?, needs_input_kind = NULL, "
                "needs_input_prompt = NULL, heartbeat_at = ? WHERE id = ?",
                ("running", utcnow_iso(), run_id),
            )
            conn.commit()

        # Re-launch the agent with the resume prompt (full chat as stdin).
        agent_cfg = config.get("agent", {})
        cmd_str = (agent_cfg.get("command") or "").strip()
        if not cmd_str:
            return cls()._fail(run_id, project_id, subject_type, subject_id, actor,
                               conn_factory, started, "missing_command",
                               "agent command is empty on resume")

        try:
            cmd = shlex.split(cmd_str)
        except ValueError as e:
            return cls()._fail(run_id, project_id, subject_type, subject_id, actor,
                               conn_factory, started, "bad_command", str(e))

        stdin_text = cls._build_stdin(run_id, user_response_text, conn_factory)

        try:
            r = subprocess.run(
                cmd,
                cwd=str(workspace.path),
                input=stdin_text,
                capture_output=True, text=True,
                timeout=cls.DEFAULT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            return cls()._fail(run_id, project_id, subject_type, subject_id, actor,
                               conn_factory, started, "timeout",
                               f"agent timed out after {cls.DEFAULT_TIMEOUT_S}s",
                               stdout_for_summary=(e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")))
        except FileNotFoundError as e:
            return cls()._fail(run_id, project_id, subject_type, subject_id, actor,
                               conn_factory, started, "agent_not_found", str(e))
        except (OSError, subprocess.SubprocessError) as e:
            return cls()._fail(run_id, project_id, subject_type, subject_id, actor,
                               conn_factory, started, "subprocess_error", str(e))

        if cancel_event is not None and cancel_event.is_set():
            return cls()._cancel(run_id, project_id, subject_type, subject_id, actor,
                                 conn_factory, started)

        stdout_text = r.stdout or ""

        # Check for another interactive marker in the resumed output.
        detected_marker: Optional[dict] = None
        non_marker_lines: list[str] = []
        for line in stdout_text.splitlines():
            m = _try_parse_marker(line)
            if m is not None:
                detected_marker = m
                non_marker_lines = []
            else:
                non_marker_lines.append(line)

        if detected_marker is not None:
            marker_kind = "propose" if "propose" in detected_marker else "text"
            context_so_far = "\n".join(non_marker_lines).strip()
            marker_payload = json.dumps(detected_marker, ensure_ascii=False)
            with db_session(conn_factory) as conn:
                if context_so_far:
                    _append_chat_entry(conn, run_id, "agent", context_so_far)
                _append_chat_entry(conn, run_id, "agent_marker", marker_payload)
                conn.execute(
                    "UPDATE runs SET status = ?, needs_input_kind = ?, "
                    "needs_input_prompt = ?, heartbeat_at = ? WHERE id = ?",
                    ("needs_input", marker_kind, marker_payload, utcnow_iso(), run_id),
                )
                conn.commit()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return RunOutcome(
                run_id=run_id,
                final_status="needs_input",
                duration_ms=elapsed_ms,
                summary=f"waiting for {marker_kind} input",
            )

        # Terminal — same as the main execute() path.
        elapsed_ms = int((time.monotonic() - started) * 1000)
        stdout_tail = cls._tail(stdout_text)
        workflow_meta = config.get("_workflow_meta")
        if r.returncode == 0:
            with db_session(conn_factory) as conn:
                summary = stdout_tail or "agent completed"
                _set_run_status(conn, run_id, "succeeded",
                                summary=summary, exit_code=r.returncode, finished=True)
                if stdout_tail:
                    _append_chat_entry(conn, run_id, "agent", stdout_tail)
                handoff = _try_parse_handoff(stdout_text)
                if handoff:
                    meta = _read_run_metadata(conn, run_id)
                    meta["handoff"] = handoff
                    _write_run_metadata(conn, run_id, meta)
                    emit_event(conn, project_id, subject_type, subject_id, "handoff_recorded",
                               {"run_id": run_id, "handoff": handoff}, actor)
                emit_event(conn, project_id, subject_type, subject_id, "agent_output",
                           {"run_id": run_id, "summary": summary}, actor)
                emit_event(conn, project_id, subject_type, subject_id, "run_succeeded",
                           {"run_id": run_id, "summary": summary, "duration_ms": elapsed_ms}, actor)
                conn.commit()
            if workflow_meta and subject_type == "ticket":
                cls._apply_on_success(
                    workflow_meta, project_id, subject_id, actor, conn_factory,
                    stdout_text=stdout_text,
                )
            return RunOutcome(run_id=run_id, final_status="succeeded",
                              duration_ms=elapsed_ms, summary=summary)

        err_msg = cls._tail(r.stderr) or stdout_tail or f"exit {r.returncode}"
        with db_session(conn_factory) as conn:
            handoff = _try_parse_handoff(stdout_text)
            if handoff:
                meta = _read_run_metadata(conn, run_id)
                meta["handoff"] = handoff
                _write_run_metadata(conn, run_id, meta)
                conn.commit()
        return cls()._fail(run_id, project_id, subject_type, subject_id, actor,
                           conn_factory, started, "non_zero_exit", err_msg,
                           exit_code=r.returncode, stdout_for_summary=stdout_tail)

    @staticmethod
    def _apply_proposal_to_ticket(
        run_id: int,
        accepted: dict,
        project_id: str,
        ticket_id: str,
        actor,
        conn_factory: Callable[[], sqlite3.Connection],
    ) -> None:
        """Apply accepted parts of a 'propose' marker to the ticket.

        accepted may contain:
          description:      str — new description
          add_criteria:     list[str]
          remove_criteria:  list[str] — criteria text to remove (exact or partial match)
          add_tags:         list[str]
          remove_tags:      list[str]
        """
        if not accepted:
            return
        try:
            from actions import update_ticket  # type: ignore[import]

            update_kwargs: dict = {}
            if accepted.get("description"):
                update_kwargs["description"] = accepted["description"]
            if accepted.get("add_tags"):
                update_kwargs["add_tags"] = list(accepted["add_tags"])
            if accepted.get("remove_tags"):
                update_kwargs["remove_tags"] = list(accepted["remove_tags"])

            conn = conn_factory()
            try:
                if update_kwargs:
                    update_ticket(conn, project_id, ticket_id, actor=actor, **update_kwargs)

                # Handle criteria add/remove (update_ticket supports add_criteria).
                if accepted.get("add_criteria"):
                    for crit_text in accepted["add_criteria"]:
                        if crit_text and crit_text.strip():
                            conn.execute(
                                "INSERT OR IGNORE INTO acceptance_criteria "
                                "(ticket_id, project_id, text, checked, sort_order) "
                                "SELECT ?, ?, ?, 0, COALESCE(MAX(sort_order)+1, 0) "
                                "FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
                                (ticket_id, project_id, crit_text.strip(), ticket_id, project_id),
                            )
                if accepted.get("remove_criteria"):
                    for crit_text in accepted["remove_criteria"]:
                        if crit_text and crit_text.strip():
                            conn.execute(
                                "DELETE FROM acceptance_criteria "
                                "WHERE ticket_id = ? AND project_id = ? "
                                "AND text = ?",
                                (ticket_id, project_id, crit_text.strip()),
                            )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            _log.exception(
                "Failed to apply proposal to ticket %r (run %d)", ticket_id, run_id
            )

    @staticmethod
    def _render_prompt(template: str, subject_id: str) -> str:
        """Tiny placeholder substitution. Future M-versions can use Jinja."""
        if not template:
            return ""
        return template.replace("{{subject.id}}", subject_id)

    @staticmethod
    def _render_prompt_with_ticket(
        template: str,
        ticket_id: str,
        project_id: str,
        conn_factory: Callable[[], sqlite3.Connection],
    ) -> str:
        """Render workflow prompt_template substituting ticket field placeholders.

        Supported tokens:
          {{ticket.id}}                   — ticket ID
          {{ticket.title}}                — ticket title
          {{ticket.description}}          — ticket description
          {{ticket.acceptance_criteria}}  — criteria joined as bullet list
        """
        if not template:
            return ""
        try:
            conn = conn_factory()
            try:
                trow = conn.execute(
                    "SELECT id, title, description FROM tickets "
                    "WHERE UPPER(id) = UPPER(?) AND project_id = ?",
                    (ticket_id, project_id),
                ).fetchone()
                if not trow:
                    # Fallback: just substitute subject.id and leave other tokens.
                    return template.replace("{{subject.id}}", ticket_id)
                criteria_rows = conn.execute(
                    "SELECT text FROM acceptance_criteria "
                    "WHERE ticket_id = ? AND project_id = ? ORDER BY id",
                    (ticket_id, project_id),
                ).fetchall()
            finally:
                conn.close()

            criteria_text = "\n".join(
                f"- {r['text']}" for r in criteria_rows
            ) if criteria_rows else "(none)"

            result = template
            result = result.replace("{{ticket.id}}", trow["id"] or "")
            result = result.replace("{{ticket.title}}", trow["title"] or "")
            result = result.replace("{{ticket.description}}", trow["description"] or "")
            result = result.replace("{{ticket.acceptance_criteria}}", criteria_text)
            # Also keep legacy {{subject.id}} working.
            result = result.replace("{{subject.id}}", ticket_id)
            return result
        except Exception:
            # Fallback: don't break the run if ticket lookup fails.
            return template.replace("{{subject.id}}", ticket_id)

    @staticmethod
    def _apply_on_success(
        workflow_meta: dict,
        project_id: str,
        ticket_id: str,
        actor,
        conn_factory: Callable[[], sqlite3.Connection],
        stdout_text: str = "",
    ) -> None:
        """Apply on_success_json actions from a DB workflow after a successful run.

        Supported actions:
          move_to:              move ticket to the named section (legacy alias of move_section)
          move_section:         move target ticket to the named section
          set_status:           set target ticket status
          set_priority:         'low' | 'medium' | 'high'
          set_automation_mode:  'auto' | 'manual' | 'paused' (with optional pause_reason)
          set_is_container:     0 | 1 — flip the cosmetic container flag
          add_tags:             list[str] — tags to add (creates if missing)
          remove_tags:          list[str] — tags to remove
          accept_ticket:        truthy → call actions.accept_ticket() for the target
                                (refuses silently if preconditions fail)
          set_readiness_content: {"flag": "<flag>", "from": "stdout"|"<literal>"}
                                Set content for a readiness flag. When from="stdout",
                                the agent's stdout (sans interactive markers) is used.
          clear_readiness_flag: "<flag>" — remove a readiness flag's content row
          set_summary_oneliner: truthy → write the agent's stdout (first non-empty
                                line, marker lines stripped) into tickets.summary_oneliner
                                and refresh tickets.summary_hash atomically. Used by
                                the "Refresh ticket summary" system workflow.
          apply_to:             'self' (default) | 'parent' — when 'parent', all the
                                above effects target the ticket's parent instead.
                                If parent is missing, the effect block is skipped.
        """
        on_success = workflow_meta.get("on_success") or {}
        if not on_success:
            return
        try:
            from actions import (  # type: ignore[import]
                move_ticket,
                update_ticket,
                accept_ticket as _accept_ticket,
                set_automation_mode as _set_automation_mode,
            )
            conn = conn_factory()
            try:
                # Resolve effect target — either the subject (self) or its parent.
                apply_to = (on_success.get("apply_to") or "self").lower()
                target_id = ticket_id
                if apply_to == "parent":
                    row = conn.execute(
                        "SELECT parent FROM tickets WHERE UPPER(id) = UPPER(?) "
                        "AND project_id = ?",
                        (ticket_id, project_id),
                    ).fetchone()
                    parent_id = row["parent"] if row else None
                    if not parent_id:
                        # Nothing to do — silently skip.
                        return
                    target_id = parent_id

                # Move section: accept move_section (canonical) or move_to (alias).
                move_to = on_success.get("move_section") or on_success.get("move_to")
                set_status = on_success.get("set_status")
                set_priority = on_success.get("set_priority")
                set_is_container = on_success.get("set_is_container")
                set_auto_mode = on_success.get("set_automation_mode")
                add_tags = on_success.get("add_tags") or []
                remove_tags = on_success.get("remove_tags") or []
                accept = on_success.get("accept_ticket")
                set_readiness = on_success.get("set_readiness_content")
                clear_readiness = on_success.get("clear_readiness_flag")
                set_summary = on_success.get("set_summary_oneliner")

                if move_to:
                    move_ticket(conn, project_id, target_id, move_to, actor=actor)

                update_kwargs = {}
                if set_status:
                    update_kwargs["status"] = set_status
                if set_priority:
                    update_kwargs["priority"] = set_priority
                if set_is_container is not None:
                    update_kwargs["is_container"] = 1 if set_is_container else 0
                if add_tags:
                    update_kwargs["add_tags"] = list(add_tags) if not isinstance(add_tags, list) else add_tags
                if remove_tags:
                    update_kwargs["remove_tags"] = list(remove_tags) if not isinstance(remove_tags, list) else remove_tags
                if update_kwargs:
                    update_ticket(conn, project_id, target_id, actor=actor, **update_kwargs)

                # set_automation_mode: switch the per-ticket automation toggle.
                if set_auto_mode:
                    mode_str = str(set_auto_mode).lower() if not isinstance(set_auto_mode, dict) else \
                        str(set_auto_mode.get("mode", "")).lower()
                    pause_reason = set_auto_mode.get("pause_reason") if isinstance(set_auto_mode, dict) else None
                    if mode_str in ("auto", "manual", "paused"):
                        try:
                            _set_automation_mode(
                                conn, project_id, "ticket", target_id,
                                mode_str, actor=actor, pause_reason=pause_reason,
                            )
                        except ValueError:
                            pass  # invalid mode — silently skip

                if accept:
                    # Verify preconditions: ticket must currently be in
                    # For Review with status 'done'. This mirrors the
                    # invariants the human Accept button enforces.
                    trow = conn.execute(
                        "SELECT section, status FROM tickets "
                        "WHERE UPPER(id) = UPPER(?) AND project_id = ?",
                        (target_id, project_id),
                    ).fetchone()
                    if trow and trow["section"] == "For Review" and trow["status"] == "done":
                        # Resolve project_path / project_name for spec writing.
                        project_path = ""
                        project_name = project_id
                        try:
                            from db import REGISTRY_PATH  # type: ignore[import]
                            import json as _json
                            registry = _json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
                            for p in registry.get("projects", []):
                                if p.get("id") == project_id:
                                    import os as _os
                                    project_path = _os.path.expanduser(p.get("path", ""))
                                    project_name = p.get("name", project_id)
                                    break
                        except Exception:
                            pass
                        _accept_ticket(
                            conn, project_id, target_id,
                            project_path, project_name, actor=actor,
                        )
                    # else: silently skip — preconditions not met.

                # set_summary_oneliner: write the agent's stdout sentence into
                # tickets.summary_oneliner + refresh tickets.summary_hash so the
                # workflow trigger doesn't fire again on the same content.
                if set_summary:
                    from actions import compute_summary_hash  # type: ignore[import]
                    raw_lines = [
                        line.strip() for line in (stdout_text or "").splitlines()
                        if line.strip() and _try_parse_marker(line) is None
                    ]
                    sentence = next((ln for ln in raw_lines if ln), "")
                    # Cap length defensively — the prompt asks for one sentence
                    # but agents drift. 280 keeps it tweet-sized; trim ellipsis.
                    if len(sentence) > 280:
                        sentence = sentence[:277].rstrip() + "…"
                    if sentence:
                        fresh_hash = compute_summary_hash(conn, project_id, target_id)
                        conn.execute(
                            "UPDATE tickets SET summary_oneliner = ?, summary_hash = ?, "
                            "updated_at = datetime('now') "
                            "WHERE UPPER(id) = UPPER(?) AND project_id = ?",
                            (sentence, fresh_hash, target_id, project_id),
                        )

                # set_readiness_content: write into a readiness flag
                if set_readiness and isinstance(set_readiness, dict):
                    flag_key = (set_readiness.get("flag") or "").lower()
                    # Map UI letter flags to DB flag names (mirrors conditions.py _eval_flag_set).
                    _flag_map = {"d": "description", "c": "criteria", "l": "reviewed"}
                    db_flag = _flag_map.get(flag_key, flag_key)
                    from_spec = set_readiness.get("from", "")
                    if from_spec == "stdout":
                        # Strip marker lines from stdout before using as content.
                        content_lines = [
                            line for line in (stdout_text or "").splitlines()
                            if _try_parse_marker(line) is None
                        ]
                        content = "\n".join(content_lines).strip()
                    else:
                        content = str(from_spec)
                    if db_flag and content:
                        conn.execute(
                            "INSERT INTO readiness_flags (ticket_id, project_id, flag, content, set_by) "
                            "VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT (ticket_id, project_id, flag) DO UPDATE SET content = excluded.content",
                            (target_id, project_id, db_flag, content, "workflow"),
                        )

                # clear_readiness_flag: remove a flag's row entirely (the
                # symmetric counterpart of set_readiness_content).
                if clear_readiness:
                    flag_key = str(clear_readiness).lower()
                    _flag_map = {"d": "description", "c": "criteria", "l": "reviewed"}
                    db_flag = _flag_map.get(flag_key, flag_key)
                    if db_flag:
                        conn.execute(
                            "DELETE FROM readiness_flags "
                            "WHERE ticket_id = ? AND project_id = ? AND flag = ?",
                            (target_id, project_id, db_flag),
                        )

                conn.commit()
            finally:
                conn.close()
        except Exception:
            _log.exception(
                "on_success actions failed for workflow %r ticket %r",
                workflow_meta.get("workflow_id"), ticket_id,
            )

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
# classify_scenario_failure — pure rule-based gap classification (M4).
#
# Heuristic rules (no LLM — LLM-backed classifier is M4+):
#   1. engine-level error (RunResult.status == "error") → test_harness_gap
#   2. no failed step at all → ambiguous_goal
#   3. "open" action with 4xx/5xx indicator → missing_screen
#   4. "assert_visible" on existing-but-not-visible element → missing_feature
#   5. wait_for / locator timeout / 0-element → missing_selector
#      (target has testid/css/role/text key)
#   6. timeout / network error in error_message → external_dependency
#   7. error_message contains "ambiguous" → ambiguous_goal
#   8. fallback → missing_feature
# ---------------------------------------------------------------------------

_SELECTOR_TARGET_KEYS = {"testid", "css", "role", "text"}
_TIMEOUT_PHRASES = ("timeout", "net::err", "connection refused", "econnrefused", "socket")
_SCREEN_PHRASES = ("404", "403", "500", "502", "503", "not found", "page not found")


def classify_scenario_failure(result, manifest: dict) -> dict:
    """Return a gap_report dict for a non-passing RunResult.

    ``result`` must be a RunResult-like object with attributes:
        .status        str — "failed" | "error"
        .failed_step   dict | None
        .failed_step_index  int | None
        .error_message str
        .screenshots   list[str]

    ``manifest`` must be a compiled scenario manifest dict with a "steps" list.
    """
    steps = manifest.get("steps", [])
    step_count = len(steps)
    manifest_id = manifest.get("id", "")
    error_message = result.error_message or ""
    failed_step = result.failed_step
    failed_step_index = result.failed_step_index

    # Screenshot: last entry in screenshots list that contains "FAILURE", else last overall.
    screenshot_path: Optional[str] = None
    if result.screenshots:
        failure_shots = [s for s in result.screenshots if "FAILURE" in s]
        screenshot_path = failure_shots[-1] if failure_shots else result.screenshots[-1]

    failed_step_action: Optional[str] = None
    failed_step_target: Optional[dict] = None
    if failed_step:
        failed_step_action = failed_step.get("action")
        failed_step_target = failed_step.get("target")

    def _build(gap_kind: str) -> dict:
        return {
            "gap_kind": gap_kind,
            "failed_step_index": failed_step_index,
            "failed_step_action": failed_step_action,
            "failed_step_target": failed_step_target,
            "screenshot_path": screenshot_path,
            "error_message": error_message,
            "manifest_id": manifest_id,
            "step_count": step_count,
        }

    # Rule 1: engine-level error
    if result.status == "error":
        return _build("test_harness_gap")

    # Rule 2: no failed step at all (passed but somehow we're here — edge case)
    if failed_step is None:
        # Rule 7 before rule 2 fallback
        if "ambiguous" in error_message.lower():
            return _build("ambiguous_goal")
        return _build("ambiguous_goal")

    # Rule 7: error_message contains "ambiguous"
    if "ambiguous" in error_message.lower():
        return _build("ambiguous_goal")

    # Rule 3: open action with nav error hinting at 4xx/5xx
    if failed_step_action == "open":
        em_lower = error_message.lower()
        if any(phrase in em_lower for phrase in _SCREEN_PHRASES):
            return _build("missing_screen")
        # open action without a clear HTTP error also maps to missing_screen
        return _build("missing_screen")

    # Rule 6: timeout / network — checked before selector so timeout on any step maps here
    em_lower = error_message.lower()
    if any(phrase in em_lower for phrase in _TIMEOUT_PHRASES):
        # But if it's a selector-style target that timed out, prefer missing_selector
        if failed_step_target and _SELECTOR_TARGET_KEYS & set(failed_step_target.keys()):
            return _build("missing_selector")
        return _build("external_dependency")

    # Rule 5: wait_for / click / fill etc. with a selector target → missing_selector
    if failed_step_action in ("wait_for", "click", "fill", "select", "press",
                               "double_click", "dblclick"):
        if failed_step_target and _SELECTOR_TARGET_KEYS & set(failed_step_target.keys()):
            return _build("missing_selector")

    # Rule 4: assert_visible → missing_feature
    if failed_step_action == "assert_visible":
        return _build("missing_feature")

    # Rule 8: fallback
    return _build("missing_feature")


# ---------------------------------------------------------------------------
# ScenarioRunner — M4 implementation.
#
# Compiles a journey into a scenario manifest and executes it via the
# tests/scenario_runner.py Playwright engine.  On non-pass results, builds a
# structured gap_report and stores it in runs.metadata_json.
#
# NOTE: tests/scenario_runner.py is appended to sys.path at call time (inside
# execute()) rather than at module import time so that:
#   a) importing runners.py never requires Playwright to be installed, and
#   b) the path surgery is isolated to the one runner that needs it.
# Longer-term, the engine should be copied to src/ so the path hack goes away.
# ---------------------------------------------------------------------------

class ScenarioRunner(Runner):
    runner_kind = "scenario"

    DEFAULT_TIMEOUT_S = 300  # 5 min hard cap for a scenario run

    def execute(
        self,
        run_id: int,
        project_id: str,
        subject_type: str,
        subject_id: str,
        workspace,
        config: dict,
        conn_factory,
        cancel_event=None,
    ) -> RunOutcome:
        # Late imports — keep top-of-module clean; playwright + journeys + scenarios
        # are only needed for this runner.
        import json as _json
        import sys as _sys
        import os as _os

        # Make the scenario_runner engine importable from tests/.
        _tests_dir = _os.path.join(_os.path.dirname(__file__), "..", "tests")
        _tests_dir = _os.path.abspath(_tests_dir)
        if _tests_dir not in _sys.path:
            _sys.path.insert(0, _tests_dir)

        # Make src/ importable (journeys, scenarios live there).
        _src_dir = _os.path.dirname(_os.path.abspath(__file__))
        if _src_dir not in _sys.path:
            _sys.path.insert(0, _src_dir)

        from scenario_runner import execute_scenario, ScenarioContext, RunResult
        from journeys import compile_to_manifest
        from scenarios import validate_manifest

        actor = ActorContext.agent(run_id)
        started = time.monotonic()

        # ── Phase 1: preparing ─────────────────────────────────────────────
        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "preparing",
                            workspace_path=str(workspace.path))
            emit_event(conn, project_id, subject_type, subject_id, "workspace_created",
                       {"path": str(workspace.path), "reused": not workspace.created_now}, actor)
            conn.commit()

        # Honour cancel before touching Playwright.
        if cancel_event is not None and cancel_event.is_set():
            return self._cancel(run_id, project_id, subject_type, subject_id,
                                actor, conn_factory, started)

        # ── Compile + validate manifest ────────────────────────────────────
        try:
            with db_session(conn_factory) as conn:
                manifest = compile_to_manifest(conn, project_id, subject_id)
            validate_manifest(manifest)
        except Exception as exc:
            return self._fail_with_gap(
                run_id, project_id, subject_type, subject_id, actor,
                conn_factory, started, "manifest_error", str(exc),
                manifest=None, result=None, conn_factory_for_meta=conn_factory,
            )

        # ── Phase 2: running ───────────────────────────────────────────────
        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "running")
            conn.commit()

        # Resolve output directory.
        output_dir = _os.path.expanduser(
            f"~/.claude/ticket-takeaway/evidence/{run_id}/scenario_runner"
        )
        _os.makedirs(output_dir, exist_ok=True)

        # Resolve base_url.
        scenario_cfg = config.get("scenario", {})
        default_base = f"http://localhost:8787/{project_id}"
        base_url = scenario_cfg.get("base_url", default_base)
        # Explicit substitution so the template is clear in config.
        base_url = base_url.replace("{project_id}", project_id)

        # ── Launch Playwright and run the scenario ─────────────────────────
        result: Optional[RunResult] = None
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    ctx_obj = ScenarioContext(base_url, browser, output_dir, manifest)
                    try:
                        result = execute_scenario(ctx_obj)
                    except Exception as exc:
                        # execute_scenario raises on failure but attaches RunResult.
                        result = getattr(exc, "__run_result__", None)
                        if result is None:
                            # Engine-level error (not a scenario step failure).
                            elapsed_ms = int((time.monotonic() - started) * 1000)
                            result = RunResult(
                                scenario_id=manifest.get("id", ""),
                                status="error",
                                duration_ms=elapsed_ms,
                                error_message=str(exc),
                            )
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as exc:
            # Playwright itself failed to launch (no binary, etc.)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            result = RunResult(
                scenario_id=manifest.get("id", ""),
                status="error",
                duration_ms=elapsed_ms,
                error_message=f"playwright launch error: {exc}",
            )

        # ── Phase 3: terminal ─────────────────────────────────────────────
        elapsed_ms = int((time.monotonic() - started) * 1000)
        step_count = len(manifest.get("steps", []))

        if result is not None and result.status == "passed":
            summary = f"scenario passed ({step_count} steps)"
            with db_session(conn_factory) as conn:
                _set_run_status(conn, run_id, "succeeded",
                                summary=summary, finished=True)
                emit_event(conn, project_id, subject_type, subject_id, "run_succeeded",
                           {"run_id": run_id, "summary": summary,
                            "duration_ms": elapsed_ms}, actor)
                conn.commit()
            return RunOutcome(run_id=run_id, final_status="succeeded",
                              duration_ms=elapsed_ms, summary=summary)

        # Non-passing — build gap report and store in metadata_json.
        if result is None:
            elapsed_ms_r = elapsed_ms
            result = RunResult(
                scenario_id=manifest.get("id", ""),
                status="error",
                duration_ms=elapsed_ms_r,
                error_message="unknown scenario error",
            )

        gap_report = classify_scenario_failure(result, manifest)
        error_class = (
            "scenario_step_failed" if result.status == "failed" else "scenario_error"
        )
        error_msg = result.error_message or "scenario did not pass"
        summary = (error_msg[:200] if error_msg else "scenario failed")

        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "failed",
                            error_class=error_class,
                            error_message=error_msg,
                            summary=summary,
                            finished=True)
            conn.execute(
                "UPDATE runs SET metadata_json = ? WHERE id = ?",
                (_json.dumps({"gap_report": gap_report}), run_id),
            )
            emit_event(conn, project_id, subject_type, subject_id, "run_failed",
                       {"run_id": run_id, "error_class": error_class,
                        "error_message": error_msg[:500]}, actor)
            conn.commit()

        return RunOutcome(
            run_id=run_id, final_status="failed",
            duration_ms=elapsed_ms, summary=summary,
            error_class=error_class, error_message=error_msg,
        )

    def _fail_with_gap(
        self, run_id, project_id, subject_type, subject_id, actor,
        conn_factory, started, error_class, error_message,
        manifest, result, conn_factory_for_meta,
    ):
        import json as _json
        elapsed_ms = int((time.monotonic() - started) * 1000)
        summary = error_message[:200] if error_message else "scenario failed"

        # Build a minimal gap report even when we don't have a full result.
        if result is not None and manifest is not None:
            gap_report = classify_scenario_failure(result, manifest)
        elif manifest is not None:
            gap_report = {
                "gap_kind": "test_harness_gap",
                "failed_step_index": None,
                "failed_step_action": None,
                "failed_step_target": None,
                "screenshot_path": None,
                "error_message": error_message,
                "manifest_id": manifest.get("id", ""),
                "step_count": len(manifest.get("steps", [])),
            }
        else:
            gap_report = {
                "gap_kind": "test_harness_gap",
                "failed_step_index": None,
                "failed_step_action": None,
                "failed_step_target": None,
                "screenshot_path": None,
                "error_message": error_message,
                "manifest_id": "",
                "step_count": 0,
            }

        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "failed",
                            error_class=error_class,
                            error_message=error_message,
                            summary=summary, finished=True)
            conn.execute(
                "UPDATE runs SET metadata_json = ? WHERE id = ?",
                (_json.dumps({"gap_report": gap_report}), run_id),
            )
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


class GapAnalyzer(Runner):
    runner_kind = "gap_analyzer"

    def execute(self, run_id, project_id, subject_type, subject_id,
                workspace, config, conn_factory, cancel_event=None):
        raise NotImplementedError("GapAnalyzer lands in M4 (closed loop)")


# ---------------------------------------------------------------------------
# NoopRunner — used by system workflows that are pure mutation rules
# (parent-promote, auto-accept, etc.). No agent subprocess, no workspace
# required. Just applies the workflow's on_success effects and marks the
# run succeeded. The workspace argument is ignored (the dispatcher passes a
# stub; see kitchen._try_claim_and_dispatch zero-step path).
# ---------------------------------------------------------------------------

class NoopRunner(Runner):
    """Runner for zero-step workflows: applies on_success effects only."""
    runner_kind = "noop"

    def execute(
        self,
        run_id: int,
        project_id: str,
        subject_type: str,
        subject_id: str,
        workspace,
        config: dict,
        conn_factory: Callable[[], sqlite3.Connection],
        cancel_event=None,
    ) -> RunOutcome:
        actor = ActorContext.system()
        started = time.monotonic()

        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "running")
            conn.commit()

        # Apply on_success effects via the same helper AgentRunner uses.
        workflow_meta = config.get("_workflow_meta")
        if workflow_meta and subject_type == "ticket":
            AgentRunner._apply_on_success(
                workflow_meta, project_id, subject_id, actor, conn_factory
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        wf_name = (workflow_meta or {}).get("workflow_name", "system workflow")
        summary = f"{wf_name} applied"
        with db_session(conn_factory) as conn:
            _set_run_status(conn, run_id, "succeeded",
                            summary=summary, exit_code=0, finished=True)
            emit_event(conn, project_id, subject_type, subject_id, "run_succeeded",
                       {"run_id": run_id, "summary": summary,
                        "duration_ms": elapsed_ms}, actor)
            conn.commit()
        return RunOutcome(run_id=run_id, final_status="succeeded",
                          duration_ms=elapsed_ms, summary=summary)
