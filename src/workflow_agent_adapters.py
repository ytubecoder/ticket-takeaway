"""Workflow agent CLI adapter helpers.

The Kitchen runner has two sources of agent configuration:

* project WORKFLOW.toml ``[agent].command`` values, historically executed as a
  shell-style command with the rendered prompt on stdin; and
* database-backed ``workflow_agents`` rows, which split ``command`` and ``args``
  and may target CLIs with different prompt conventions.

This module keeps those conventions explicit and testable without adding new
schema.  ``args`` accepts both the historical JSON-array representation and the
newer shell-style strings used by the default seeded agents.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentInvocation:
    argv: list[str]
    stdin: str | None = None


def normalize_agent_args(value: Any) -> list[str]:
    """Return ``value`` as argv fragments.

    Accepts JSON-array strings (legacy API/CLI), shell-style strings (current
    seed data), Python lists/tuples, and empty/null values.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None]
    if not isinstance(value, str):
        return [str(value)]

    text = value.strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v) for v in parsed if v is not None]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    return shlex.split(text)


def parse_agent_output(output: str) -> str:
    """Normalize common agent output wrappers to plain text."""
    text = (output or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text

    if isinstance(parsed, dict):
        for key in ("result", "content", "message", "summary"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value.strip()
        choices = parsed.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"].strip()
    if isinstance(parsed, str):
        return parsed.strip()
    return text


def infer_runner_type(agent_cfg: dict[str, Any]) -> str:
    explicit = (agent_cfg.get("runner_type") or agent_cfg.get("adapter") or "").strip().lower()
    if explicit:
        return explicit
    command = (agent_cfg.get("command") or "").strip()
    try:
        argv0 = shlex.split(command)[0]
    except (ValueError, IndexError):
        argv0 = command.split()[0] if command.split() else ""
    name = argv0.rsplit("/", 1)[-1].lower()
    if name in {"claude", "hermes", "openclaw", "opencode", "codex"}:
        return name
    return "custom"


def _command_parts(command: str) -> list[str]:
    return shlex.split((command or "").strip())


def _with_system_prompt(prompt: str, system_prompt: str | None) -> str:
    system = (system_prompt or "").strip()
    if not system:
        return prompt
    return f"{system}\n\n{prompt}"


def _has_prompt_flag(args: list[str]) -> bool:
    return "-p" in args or "--prompt" in args


def build_agent_invocation(agent_cfg: dict[str, Any], prompt: str) -> AgentInvocation:
    """Build argv/stdin for a workflow agent.

    ``runner_type`` controls how the prompt is delivered.  If omitted, the type
    is inferred from the command basename.  For callers that need the historical
    WORKFLOW.toml behavior, do not call this helper; run the command directly
    with prompt on stdin.
    """
    command = (agent_cfg.get("command") or "").strip()
    if not command:
        raise ValueError("agent command is empty")
    cmd = _command_parts(command)
    args = normalize_agent_args(agent_cfg.get("args"))
    runner_type = infer_runner_type(agent_cfg)
    prompt_text = _with_system_prompt(prompt, agent_cfg.get("system_prompt"))

    if runner_type == "claude":
        argv = cmd + args
        if _has_prompt_flag(args):
            # Seeded Planner stores args='-p'; attach the prompt to that flag
            # rather than appending a duplicate -p.
            argv.append(prompt_text)
        else:
            argv.extend(["-p", prompt_text])
        if "--output-format" not in args:
            argv.extend(["--output-format", "json"])
        if not agent_cfg.get("persist_session") and "--no-session-persistence" not in args:
            argv.append("--no-session-persistence")
        return AgentInvocation(argv=argv, stdin=None)

    if runner_type == "hermes":
        argv = cmd[:]
        # Default Hermes non-interactive chat shape. If the command already
        # includes a subcommand, respect it and only append the query flag.
        if len(argv) == 1 and (not args or args[0] != "chat"):
            argv.append("chat")
        argv.extend(["-q", prompt_text])
        argv.extend(args)
        return AgentInvocation(argv=argv, stdin=None)

    if runner_type in {"codex", "openclaw", "opencode"}:
        return AgentInvocation(argv=cmd + args + [prompt_text], stdin=None)

    prompt_mode = (agent_cfg.get("prompt_mode") or "stdin").strip().lower()
    argv = cmd + args
    if prompt_mode in {"arg", "append_arg"}:
        return AgentInvocation(argv=argv + [prompt_text], stdin=None)
    if prompt_mode in {"none", "manual"}:
        return AgentInvocation(argv=argv, stdin=None)
    return AgentInvocation(argv=argv, stdin=prompt_text)
