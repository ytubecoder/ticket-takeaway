"""TDD tests for the endpoints layer.

Covers:
- endpoints.build_invocation (argv construction, prompt/session_id substitution)
- endpoints.extract_session_id (regex + fallback dir)
- Migration #19 (data backfill, grouping, idempotency, transactionality)
"""
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def endpoint_cli_claude():
    from endpoints import Endpoint
    return Endpoint(
        id="claude-cli",
        name="Claude CLI",
        endpoint_type="cli",
        command="claude",
        args=["-p", "{prompt}", "--output-format", "json"],
        prompt_mode="template",
        capabilities={"sessions": True},
        session_config={
            "resume_args": ["-p", "{prompt}", "--output-format", "json",
                            "--resume", "{session_id}"],
            "session_id_regex": r'"session_id"\s*:\s*"([0-9a-f-]+)"',
        },
    )


@pytest.fixture
def endpoint_cli_codex():
    from endpoints import Endpoint
    return Endpoint(
        id="codex-cli",
        name="Codex CLI",
        endpoint_type="cli",
        command="codex",
        args=["{prompt}"],
        capabilities={"sessions": True},
        session_config={
            "resume_args": ["exec", "resume", "{session_id}"],
            "session_id_regex": r"Session(?:\s+ID)?\s*:\s*([0-9a-f-]+)",
        },
    )


@pytest.fixture
def endpoint_api_anthropic():
    from endpoints import Endpoint
    return Endpoint(
        id="anth-1",
        name="Anthropic API",
        endpoint_type="anthropic_api",
        provider="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
    )


# === build_invocation ===

def test_build_invocation_prompt_placeholder(endpoint_cli_claude):
    from endpoints import build_invocation
    argv = build_invocation(endpoint_cli_claude, "hello world")
    assert argv == ["claude", "-p", "hello world", "--output-format", "json"]


def test_build_invocation_no_placeholder_positional(endpoint_cli_codex):
    """codex stores ['{prompt}'] so substitution covers it; verify the
    no-placeholder fallback for a hypothetical endpoint with args=[]."""
    from endpoints import Endpoint, build_invocation
    ep = Endpoint(id="x", name="x", endpoint_type="cli",
                  command="codex", args=[])
    argv = build_invocation(ep, "do thing")
    assert argv == ["codex", "do thing"]


def test_build_invocation_multiple_prompt_occurrences():
    from endpoints import Endpoint, build_invocation
    ep = Endpoint(id="x", name="x", endpoint_type="cli",
                  command="echo", args=["{prompt}", "and again", "{prompt}"])
    argv = build_invocation(ep, "hi")
    assert argv == ["echo", "hi", "and again", "hi"]


def test_build_invocation_with_session_id_replaces_args(endpoint_cli_codex):
    from endpoints import build_invocation
    argv = build_invocation(endpoint_cli_codex, "anything",
                            session_id="abc-123")
    # resume_args fully replaces args
    assert argv == ["codex", "exec", "resume", "abc-123"]


def test_build_invocation_with_session_id_substitutes_both_placeholders(endpoint_cli_claude):
    from endpoints import build_invocation
    argv = build_invocation(endpoint_cli_claude, "continue",
                            session_id="sess-xyz")
    assert argv == ["claude", "-p", "continue", "--output-format", "json",
                    "--resume", "sess-xyz"]


def test_build_invocation_session_id_without_resume_args_falls_through(caplog):
    from endpoints import Endpoint, build_invocation
    ep = Endpoint(id="x", name="x", endpoint_type="cli", command="claude",
                  args=["{prompt}"],
                  capabilities={"sessions": True},
                  session_config={})  # advertises sessions but no template
    argv = build_invocation(ep, "hi", session_id="s-1")
    assert argv == ["claude", "hi"]
    assert any("advertises sessions but has no resume_args" in r.message
               for r in caplog.records)


def test_build_invocation_rejects_anthropic_api(endpoint_api_anthropic):
    from endpoints import build_invocation, UnsupportedEndpointType
    with pytest.raises(UnsupportedEndpointType) as exc:
        build_invocation(endpoint_api_anthropic, "x")
    assert "anthropic_api" in str(exc.value)
    assert "phase 1" in str(exc.value).lower()


@pytest.mark.parametrize("etype", ["openai_api", "gemini_api", "ssh_cli"])
def test_build_invocation_rejects_other_non_cli_types(etype):
    from endpoints import Endpoint, build_invocation, UnsupportedEndpointType
    ep = Endpoint(id="x", name="x", endpoint_type=etype)
    with pytest.raises(UnsupportedEndpointType):
        build_invocation(ep, "x")


def test_build_invocation_rejects_invalid_args_not_array():
    from endpoints import Endpoint, build_invocation, EndpointMisconfigured
    ep = Endpoint(id="x", name="x", endpoint_type="cli", command="claude")
    ep.args = "not a list"   # bypass dataclass typing for the test
    with pytest.raises(EndpointMisconfigured):
        build_invocation(ep, "x")


def test_build_invocation_rejects_non_string_args_element():
    from endpoints import Endpoint, build_invocation, EndpointMisconfigured
    ep = Endpoint(id="x", name="x", endpoint_type="cli",
                  command="claude", args=["-p", 42, "{prompt}"])
    with pytest.raises(EndpointMisconfigured) as exc:
        build_invocation(ep, "x")
    assert "index 1" in str(exc.value) or "[1]" in str(exc.value)


def test_build_invocation_stdin_mode_not_implemented():
    from endpoints import Endpoint, build_invocation
    ep = Endpoint(id="x", name="x", endpoint_type="cli",
                  command="claude", args=[], prompt_mode="stdin")
    with pytest.raises(NotImplementedError):
        build_invocation(ep, "x")


# === extract_session_id ===

def test_extract_session_id_from_stdout(endpoint_cli_claude):
    from endpoints import extract_session_id
    stdout = '{"session_id": "abc-12345-fed", "result": "ok"}'
    sid = extract_session_id(endpoint_cli_claude, stdout, "", started_before=0)
    assert sid == "abc-12345-fed"


def test_extract_session_id_from_stderr(endpoint_cli_codex):
    from endpoints import extract_session_id
    sid = extract_session_id(endpoint_cli_codex, "",
                             "Session: 019e1234-5678-90ab",
                             started_before=0)
    assert sid == "019e1234-5678-90ab"


def test_extract_session_id_no_match_no_fallback_returns_none():
    from endpoints import Endpoint, extract_session_id
    ep = Endpoint(id="x", name="x", endpoint_type="cli", command="x",
                  capabilities={"sessions": True},
                  session_config={"session_id_regex": r"id:(\w+)"})
    assert extract_session_id(ep, "no id here", "", 0) is None


def test_extract_session_id_fallback_dir_picks_newest_file(tmp_path):
    """If regex misses and a fallback_dir is configured, return the
    newest filename (stem) created since `started_before`."""
    import time
    from endpoints import Endpoint, extract_session_id
    # Setup: create two files, one old (before started_before) and one new
    started = time.time()
    old = tmp_path / "old-session.json"
    old.write_text("{}")
    import os
    old_mtime = started - 10
    os.utime(old, (old_mtime, old_mtime))
    time.sleep(0.05)
    new = tmp_path / "newer-session.json"
    new.write_text("{}")
    ep = Endpoint(id="x", name="x", endpoint_type="cli", command="x",
                  capabilities={"sessions": True},
                  session_config={
                      "session_id_regex": r"NEVER_MATCHES",
                      "session_id_fallback_dir": str(tmp_path),
                  })
    sid = extract_session_id(ep, "no match", "", started_before=started)
    assert sid == "newer-session"


def test_extract_session_id_capabilities_false_returns_none():
    """Documentation: callers should not invoke this for sessions=False
    endpoints, but if they do, return None safely."""
    from endpoints import Endpoint, extract_session_id
    ep = Endpoint(id="x", name="x", endpoint_type="cli", command="x",
                  capabilities={"sessions": False})
    assert extract_session_id(ep, "anything", "", 0) is None
