"""Workflow Bounce agent runner adapters.

Workflow agents look generic in the database (command + args), but different
agent CLIs accept prompts in different shapes.  This module is the single place
that turns a workflow agent row plus prompt into a subprocess command.

Supported built-ins:
- claude: backwards-compatible Claude Code one-shot invocation.
- hermes: Hermes Agent one-shot chat invocation.
- openclaw: template-based so the command can be adjusted as the CLI evolves.
- opencode/codex: conservative template-capable built-ins.
- custom: raw command + args, optionally prompt on stdin.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

PROMPT_PLACEHOLDER = "{{prompt}}"
STDIN_SENTINEL = "{{stdin}}"

SUPPORTED_RUNNER_TYPES = {
    "claude",
    "hermes",
    "openclaw",
    "opencode",
    "codex",
    "custom",
}


class AgentAdapterError(ValueError):
    """Raised when an agent row cannot be converted into a safe command."""


def normalize_runner_type(value: Any) -> str:
    """Return a supported runner type, defaulting to backwards-compatible claude."""
    runner_type = str(value or "claude").strip().lower()
    aliases = {
        "claude-code": "claude",
        "hermes-agent": "hermes",
        "open-claw": "openclaw",
        "open_claw": "openclaw",
        "open-code": "opencode",
        "open_code": "opencode",
    }
    runner_type = aliases.get(runner_type, runner_type)
    if runner_type not in SUPPORTED_RUNNER_TYPES:
        raise AgentAdapterError(
            f"runner_type must be one of: {', '.join(sorted(SUPPORTED_RUNNER_TYPES))}"
        )
    return runner_type


def parse_args(value: Any) -> list[str]:
    """Parse agent args from a JSON array, shell string, list, or empty value."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return shlex.split(stripped)
        if not isinstance(parsed, list):
            raise AgentAdapterError("args must be a JSON array or shell-style argument string")
        return [str(v) for v in parsed]
    raise AgentAdapterError("args must be a JSON array string, shell string, or list")


def _default_command(agent: dict, fallback: str) -> str:
    command = str(agent.get("command") or fallback).strip()
    if not command:
        raise AgentAdapterError("agent command is empty")
    return command


def _replace_prompt_tokens(parts: list[str], prompt: str) -> tuple[list[str], str | None]:
    """Replace {{prompt}} tokens. {{stdin}} means send the prompt on stdin."""
    stdin = None
    out: list[str] = []
    for part in parts:
        if part == STDIN_SENTINEL:
            stdin = prompt
            continue
        out.append(part.replace(PROMPT_PLACEHOLDER, prompt))
    return out, stdin


def _template_parts(template: Any) -> list[str]:
    if not template:
        return []
    if isinstance(template, list):
        return [str(v) for v in template]
    if isinstance(template, str):
        stripped = template.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return shlex.split(stripped)
        if not isinstance(parsed, list):
            raise AgentAdapterError("command_template must be a JSON array or shell-style string")
        return [str(v) for v in parsed]
    raise AgentAdapterError("command_template must be a JSON array, shell string, or list")


def build_agent_command(agent: dict, prompt: str) -> tuple[list[str], str | None]:
    """Build (argv, stdin) for a workflow agent row.

    Existing agents without runner_type keep the historical Claude Code command
    shape so current workflows do not need migration.
    """
    runner_type = normalize_runner_type(agent.get("runner_type"))
    agent_args = parse_args(agent.get("args", "[]"))
    template = _template_parts(agent.get("command_template"))
    if template:
        return _replace_prompt_tokens(template, prompt)

    prompt_mode = str(agent.get("prompt_mode") or "arg").strip().lower()

    if runner_type == "claude":
        command = _default_command(agent, "claude")
        return shlex.split(command) + agent_args + [
            "-p",
            prompt,
            "--output-format",
            "json",
            "--no-session-persistence",
        ], None

    if runner_type == "hermes":
        command = _default_command(agent, "hermes")
        # Hermes global flags must come after `hermes chat` for the chat command.
        return shlex.split(command) + [
            "chat",
            "-q",
            prompt,
            "--quiet",
            "--source",
            "ticket-takeaway-workflow",
        ] + agent_args, None

    if runner_type == "openclaw":
        command = _default_command(agent, "openclaw")
        # OpenClaw CLIs have changed shape across releases.  Keep a sensible
        # default but prefer command_template for exact installs.
        return shlex.split(command) + ["run", "--prompt", prompt] + agent_args, None

    if runner_type == "opencode":
        command = _default_command(agent, "opencode")
        return shlex.split(command) + ["run", prompt] + agent_args, None

    if runner_type == "codex":
        command = _default_command(agent, "codex")
        return shlex.split(command) + ["exec", prompt] + agent_args, None

    # custom
    command = _default_command(agent, "")
    cmd = shlex.split(command) + agent_args
    if prompt_mode == "stdin":
        return cmd, prompt
    if prompt_mode in ("arg", "append_arg"):
        return cmd + [prompt], None
    if prompt_mode in ("none", "manual"):
        return cmd, None
    # Unknown custom modes fall back to stdin: safest and least shell-quoting prone.
    return cmd, prompt


def parse_agent_output(stdout: str) -> str:
    """Extract useful text from common agent JSON wrappers, else raw stdout."""
    text = (stdout or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict):
        for key in ("result", "content", "message", "output"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(parsed, ensure_ascii=False)
    if isinstance(parsed, list):
        return json.dumps(parsed, ensure_ascii=False)
    return str(parsed)
