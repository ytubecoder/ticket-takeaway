import importlib.util
from pathlib import Path


INSTALL_PATH = Path(__file__).resolve().parents[1] / "install.py"


def load_install_module():
    spec = importlib.util.spec_from_file_location("ticket_takeaway_install", INSTALL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def target_names(targets):
    return [name for name, _ in targets]


def test_auto_uses_codex_when_running_under_codex(tmp_path):
    install = load_install_module()
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()

    targets = install.resolve_skill_targets(
        "auto",
        env={"CODEX_CI": "1", "CLAUDE_HOME": str(tmp_path / "other-claude")},
        home=tmp_path,
    )

    assert target_names(targets) == ["codex"]
    assert targets[0][1] == tmp_path / ".codex" / "skills"


def test_auto_uses_codex_when_only_codex_home_exists(tmp_path):
    install = load_install_module()
    (tmp_path / ".codex").mkdir()

    targets = install.resolve_skill_targets("auto", env={}, home=tmp_path)

    assert target_names(targets) == ["codex"]
    assert targets[0][1] == tmp_path / ".codex" / "skills"


def test_auto_defaults_to_claude_when_ambiguous(tmp_path):
    install = load_install_module()
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()

    targets = install.resolve_skill_targets("auto", env={}, home=tmp_path)

    assert target_names(targets) == ["claude"]
    assert targets[0][1] == tmp_path / ".claude" / "skills"


def test_codex_target_honors_codex_home(tmp_path):
    install = load_install_module()
    codex_home = tmp_path / "custom-codex"

    targets = install.resolve_skill_targets(
        "codex",
        env={"CODEX_HOME": str(codex_home)},
        home=tmp_path,
    )

    assert target_names(targets) == ["codex"]
    assert targets[0][1] == codex_home / "skills"


def test_both_and_none_targets(tmp_path):
    install = load_install_module()

    both = install.resolve_skill_targets("both", env={}, home=tmp_path)
    assert target_names(both) == ["claude", "codex"]
    assert [path for _, path in both] == [
        tmp_path / ".claude" / "skills",
        tmp_path / ".codex" / "skills",
    ]

    assert install.resolve_skill_targets("none", env={}, home=tmp_path) == []
