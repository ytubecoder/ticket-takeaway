"""Dashboard regeneration must target the project that changed (BUG-04).

Regression tests for nested registered projects — a sub-project board whose
path lives inside a parent project's tree. Two defects combined to leave the
nested project's dashboard permanently stale:

1. generate.py's cwd auto-detect took the FIRST registry entry whose path was
   a prefix of the cwd, so when the parent was listed before the nested child,
   every regen from inside the child resolved to the parent.
2. tickets-cli.regenerate_dashboard() relied on that auto-detect (cwd only)
   even though it holds the project id, and swallowed the subprocess result,
   so the misfire was silent.
"""

import os
import sys
import types

from conftest import cli_mod, gen_mod

# ---------------------------------------------------------------------------
# generate.py: detect_project_from_cwd — most-specific match, order-blind
# ---------------------------------------------------------------------------


def _nested_registry(tmp_path):
    parent = tmp_path / "parent"
    child = parent / "nested"
    child.mkdir(parents=True)
    entries = [
        {"id": "parent", "path": str(parent)},
        {"id": "child", "path": str(child)},
    ]
    return entries, parent, child


def test_nested_child_wins_even_when_parent_listed_first(tmp_path):
    entries, _, child = _nested_registry(tmp_path)
    got = gen_mod.detect_project_from_cwd(entries, os.path.realpath(str(child)))
    assert got == "child"


def test_registry_order_is_irrelevant(tmp_path):
    entries, _, child = _nested_registry(tmp_path)
    got = gen_mod.detect_project_from_cwd(
        list(reversed(entries)), os.path.realpath(str(child))
    )
    assert got == "child"


def test_parent_cwd_still_resolves_parent(tmp_path):
    entries, parent, _ = _nested_registry(tmp_path)
    got = gen_mod.detect_project_from_cwd(entries, os.path.realpath(str(parent)))
    assert got == "parent"


def test_subdirectory_of_nested_child_resolves_child(tmp_path):
    entries, _, child = _nested_registry(tmp_path)
    sub = child / "docs"
    sub.mkdir()
    got = gen_mod.detect_project_from_cwd(entries, os.path.realpath(str(sub)))
    assert got == "child"


def test_unrelated_cwd_returns_none(tmp_path):
    entries, _, _ = _nested_registry(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    got = gen_mod.detect_project_from_cwd(entries, os.path.realpath(str(outside)))
    assert got is None


def test_sibling_name_prefix_is_not_a_match(tmp_path):
    # /a/foo must not claim /a/foo-bar
    foo = tmp_path / "foo"
    foobar = tmp_path / "foo-bar"
    foo.mkdir()
    foobar.mkdir()
    entries = [{"id": "foo", "path": str(foo)}]
    got = gen_mod.detect_project_from_cwd(entries, os.path.realpath(str(foobar)))
    assert got is None


def test_empty_path_entry_is_ignored(tmp_path):
    # realpath("") is the process cwd — an empty-path entry must never match.
    entries, _, child = _nested_registry(tmp_path)
    entries.insert(0, {"id": "pathless", "path": ""})
    got = gen_mod.detect_project_from_cwd(entries, os.path.realpath(str(child)))
    assert got == "child"


# ---------------------------------------------------------------------------
# tickets-cli.py: regenerate_dashboard names the project explicitly
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_regen(monkeypatch, tmp_path, result=None):
    """Point DASHBOARD_DIR at a stub generate.py and capture subprocess calls."""
    (tmp_path / "generate.py").write_text("# stub\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return result or _FakeResult()

    monkeypatch.setattr(cli_mod, "DASHBOARD_DIR", tmp_path)
    monkeypatch.setattr(cli_mod, "subprocess", types.SimpleNamespace(run=fake_run))
    return calls


def test_regenerate_dashboard_passes_project_id(tmp_path, monkeypatch):
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    calls = _patch_regen(monkeypatch, tmp_path)

    cli_mod.regenerate_dashboard({"id": "child", "path": str(proj_dir)})

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[0] == sys.executable
    assert cmd[1] == str(tmp_path / "generate.py")
    assert "--project" in cmd
    assert cmd[cmd.index("--project") + 1] == "child"
    assert kwargs.get("cwd") == str(proj_dir)


def test_regenerate_dashboard_without_id_omits_project_flag(tmp_path, monkeypatch):
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    calls = _patch_regen(monkeypatch, tmp_path)

    cli_mod.regenerate_dashboard({"path": str(proj_dir)})

    assert len(calls) == 1
    cmd, _ = calls[0]
    assert "--project" not in cmd


def test_regenerate_dashboard_reports_failure(tmp_path, monkeypatch, capsys):
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    _patch_regen(
        monkeypatch, tmp_path, result=_FakeResult(returncode=1, stderr="boom\n")
    )

    cli_mod.regenerate_dashboard({"id": "child", "path": str(proj_dir)})

    err = capsys.readouterr().err
    assert "child" in err
    assert "boom" in err


def test_regenerate_dashboard_silent_on_success(tmp_path, monkeypatch, capsys):
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    _patch_regen(monkeypatch, tmp_path)

    cli_mod.regenerate_dashboard({"id": "child", "path": str(proj_dir)})

    captured = capsys.readouterr()
    assert captured.err == ""
