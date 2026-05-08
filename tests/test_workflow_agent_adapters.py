"""Tests for Workflow Bounce agent CLI adapter command building."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflow_agent_adapters import build_agent_command, parse_agent_output


def test_claude_adapter_preserves_existing_command_shape():
    agent = {"runner_type": "claude", "command": "claude", "args": '["--model", "sonnet"]'}

    cmd, stdin = build_agent_command(agent, "hello")

    assert cmd == [
        "claude",
        "--model",
        "sonnet",
        "-p",
        "hello",
        "--output-format",
        "json",
        "--no-session-persistence",
    ]
    assert stdin is None


def test_hermes_adapter_uses_chat_query_interface():
    agent = {"runner_type": "hermes", "command": "hermes", "args": '["--provider", "openai-codex"]'}

    cmd, stdin = build_agent_command(agent, "summarize ticket")

    assert cmd == [
        "hermes",
        "chat",
        "-q",
        "summarize ticket",
        "--quiet",
        "--source",
        "ticket-takeaway-workflow",
        "--provider",
        "openai-codex",
    ]
    assert stdin is None


def test_openclaw_adapter_uses_template_so_cli_can_be_swapped():
    agent = {
        "runner_type": "openclaw",
        "command": "openclaw",
        "args": "[]",
        "command_template": '["openclaw", "run", "--prompt", "{{prompt}}", "--json"]',
    }

    cmd, stdin = build_agent_command(agent, "review plan")

    assert cmd == ["openclaw", "run", "--prompt", "review plan", "--json"]
    assert stdin is None


def test_custom_adapter_supports_stdin_prompt_template():
    agent = {
        "runner_type": "custom",
        "command": "python3",
        "args": '["scripts/agent.py"]',
        "prompt_mode": "stdin",
    }

    cmd, stdin = build_agent_command(agent, "via stdin")

    assert cmd == ["python3", "scripts/agent.py"]
    assert stdin == "via stdin"


def test_custom_adapter_arg_mode_appends_prompt():
    agent = {
        "runner_type": "custom",
        "command": "python3",
        "args": '["scripts/agent.py"]',
        "prompt_mode": "arg",
    }

    cmd, stdin = build_agent_command(agent, "via arg")

    assert cmd == ["python3", "scripts/agent.py", "via arg"]
    assert stdin is None


def test_parse_agent_output_accepts_claude_json_result_and_plain_text():
    assert parse_agent_output(json.dumps({"result": "ok"})) == "ok"
    assert parse_agent_output("plain output") == "plain output"
