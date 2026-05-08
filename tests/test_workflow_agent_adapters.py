import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_normalize_agent_args_accepts_json_array_and_shell_string():
    from workflow_agent_adapters import normalize_agent_args

    assert normalize_agent_args('["exec", "-s", "read-only"]') == [
        "exec",
        "-s",
        "read-only",
    ]
    assert normalize_agent_args("exec -s read-only") == ["exec", "-s", "read-only"]
    assert normalize_agent_args(["--model", "sonnet"]) == ["--model", "sonnet"]
    assert normalize_agent_args("") == []


def test_claude_adapter_uses_prompt_arg_and_preserves_flags():
    from workflow_agent_adapters import build_agent_invocation

    invocation = build_agent_invocation(
        {
            "command": "claude",
            "args": '["--model", "sonnet"]',
            "runner_type": "claude",
            "persist_session": 0,
        },
        "review this plan",
    )

    assert invocation.argv == [
        "claude",
        "--model",
        "sonnet",
        "-p",
        "review this plan",
        "--output-format",
        "json",
        "--no-session-persistence",
    ]
    assert invocation.stdin is None


def test_codex_adapter_preserves_shell_style_args_and_passes_prompt_after_exec():
    from workflow_agent_adapters import build_agent_invocation

    invocation = build_agent_invocation(
        {
            "command": "codex",
            "args": "exec -s read-only",
            "runner_type": "codex",
        },
        "check the ticket",
    )

    assert invocation.argv == ["codex", "exec", "-s", "read-only", "check the ticket"]
    assert invocation.stdin is None


def test_hermes_and_openclaw_adapters_do_not_receive_claude_flags():
    from workflow_agent_adapters import build_agent_invocation

    hermes = build_agent_invocation(
        {"command": "hermes", "args": "--provider openai-codex", "runner_type": "hermes"},
        "summarize ticket",
    )
    assert hermes.argv == [
        "hermes",
        "chat",
        "-q",
        "summarize ticket",
        "--provider",
        "openai-codex",
    ]
    assert "--output-format" not in hermes.argv
    assert hermes.stdin is None

    openclaw = build_agent_invocation(
        {"command": "openclaw", "args": "run --fast", "runner_type": "openclaw"},
        "implement this",
    )
    assert openclaw.argv == ["openclaw", "run", "--fast", "implement this"]
    assert "--output-format" not in openclaw.argv
    assert openclaw.stdin is None


def test_custom_adapter_defaults_to_stdin_and_can_pass_prompt_as_arg():
    from workflow_agent_adapters import build_agent_invocation

    stdin_invocation = build_agent_invocation(
        {"command": "python3", "args": "scripts/agent.py", "runner_type": "custom"},
        "hello",
    )
    assert stdin_invocation.argv == ["python3", "scripts/agent.py"]
    assert stdin_invocation.stdin == "hello"

    arg_invocation = build_agent_invocation(
        {
            "command": "python3",
            "args": "scripts/agent.py",
            "runner_type": "custom",
            "prompt_mode": "arg",
        },
        "hello",
    )
    assert arg_invocation.argv == ["python3", "scripts/agent.py", "hello"]
    assert arg_invocation.stdin is None


def test_parse_agent_output_accepts_json_wrappers_and_plain_text():
    from workflow_agent_adapters import parse_agent_output

    assert parse_agent_output('{"result": "done"}') == "done"
    assert parse_agent_output('{"content": "ok"}') == "ok"
    assert parse_agent_output("plain output") == "plain output"
