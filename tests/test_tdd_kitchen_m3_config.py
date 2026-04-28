"""TDD tests for Kitchen M3 — workflow_config.py reader.

Pure logic. No server, no Playwright. Uses tmp_path for filesystem isolation.
See docs/KITCHEN.md §11.
"""

import copy
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workflow_config import DEFAULTS, load_prompt_template, load_workflow_config


# ---------------------------------------------------------------------------
# load_workflow_config
# ---------------------------------------------------------------------------


def test_missing_workflow_toml_returns_defaults(tmp_path):
    """No WORKFLOW.toml → result equals DEFAULTS (deep equality, not same obj)."""
    result = load_workflow_config(tmp_path)
    assert result == DEFAULTS
    assert result is not DEFAULTS


def test_empty_workflow_toml_returns_defaults(tmp_path):
    """Empty WORKFLOW.toml → result equals DEFAULTS."""
    (tmp_path / "WORKFLOW.toml").write_text("", encoding="utf-8")
    result = load_workflow_config(tmp_path)
    assert result == DEFAULTS
    assert result is not DEFAULTS


def test_partial_workflow_toml_merges_at_leaf(tmp_path):
    """Only [agent] command overridden → other agent.* and all other sections unchanged."""
    (tmp_path / "WORKFLOW.toml").write_text(
        '[agent]\ncommand = "codex"\n', encoding="utf-8"
    )
    result = load_workflow_config(tmp_path)

    # Overridden leaf
    assert result["agent"]["command"] == "codex"
    # Default leaves in same section preserved
    assert result["agent"]["max_turns"] == 20
    assert result["agent"]["sandbox"] == "workspace-write"
    assert result["agent"]["base_ref"] == "origin/main"
    # Other sections entirely unchanged
    assert result["automation"] == DEFAULTS["automation"]
    assert result["workspace"] == DEFAULTS["workspace"]
    assert result["evidence"] == DEFAULTS["evidence"]
    assert result["hooks"] == DEFAULTS["hooks"]


def test_unknown_section_preserved(tmp_path):
    """Unknown top-level section passes through alongside merged known sections."""
    (tmp_path / "WORKFLOW.toml").write_text(
        '[experimental]\nfoo = "bar"\n', encoding="utf-8"
    )
    result = load_workflow_config(tmp_path)

    assert result["experimental"] == {"foo": "bar"}
    # Known sections still present
    assert result["automation"] == DEFAULTS["automation"]
    assert result["agent"] == DEFAULTS["agent"]


def test_unknown_leaf_keys_within_known_section_preserved(tmp_path):
    """Unknown leaf keys inside a known section are kept (forward-compat)."""
    (tmp_path / "WORKFLOW.toml").write_text(
        '[agent]\nfuture_flag = true\n', encoding="utf-8"
    )
    result = load_workflow_config(tmp_path)

    assert result["agent"]["future_flag"] is True
    # Existing defaults still present
    assert result["agent"]["command"] == "claude -p"
    assert result["agent"]["max_turns"] == 20


def test_invalid_toml_raises_clear_error(tmp_path):
    """Malformed TOML raises ValueError mentioning the file path."""
    bad_toml = tmp_path / "WORKFLOW.toml"
    bad_toml.write_text("not valid: toml [[]\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_workflow_config(tmp_path)

    msg = str(exc_info.value)
    assert "WORKFLOW.toml" in msg
    # Should contain the path
    assert str(tmp_path) in msg


def test_load_workflow_config_does_not_mutate_defaults(tmp_path):
    """Calling load_workflow_config twice with overrides never mutates DEFAULTS."""
    original_defaults = copy.deepcopy(DEFAULTS)

    (tmp_path / "WORKFLOW.toml").write_text(
        '[agent]\ncommand = "codex"\nmax_turns = 99\n', encoding="utf-8"
    )

    result1 = load_workflow_config(tmp_path)
    result2 = load_workflow_config(tmp_path)

    # DEFAULTS must be unchanged
    assert DEFAULTS == original_defaults

    # Results are independent copies
    result1["agent"]["command"] = "mutated"
    assert result2["agent"]["command"] == "codex"
    assert DEFAULTS["agent"]["command"] == "claude -p"


# ---------------------------------------------------------------------------
# load_prompt_template
# ---------------------------------------------------------------------------


def test_missing_prompt_md_returns_empty(tmp_path):
    """No PROMPT.md → empty string."""
    assert load_prompt_template(tmp_path) == ""


def test_prompt_md_returns_trimmed_content(tmp_path):
    """File with leading/trailing whitespace → content stripped."""
    (tmp_path / "PROMPT.md").write_text(
        "\n\n  You are an agent.\n\n", encoding="utf-8"
    )
    result = load_prompt_template(tmp_path)
    assert result == "You are an agent."


def test_prompt_md_with_template_placeholders_kept_intact(tmp_path):
    """{{subject.id}} and other placeholders survive verbatim — no rendering."""
    body = "You are working on ticket {{subject.id}}: {{subject.title}}\nDo the thing."
    (tmp_path / "PROMPT.md").write_text(body, encoding="utf-8")
    result = load_prompt_template(tmp_path)
    assert "{{subject.id}}" in result
    assert "{{subject.title}}" in result
    assert result == body


def test_empty_prompt_md_returns_empty(tmp_path):
    """Empty PROMPT.md → empty string."""
    (tmp_path / "PROMPT.md").write_text("   \n\n   ", encoding="utf-8")
    assert load_prompt_template(tmp_path) == ""
