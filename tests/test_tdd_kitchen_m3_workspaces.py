"""TDD tests for Kitchen M3 — workspace manager (git worktrees + hooks).

Hermetic. Uses tmp_path to host both the simulated 'project' (a real git repo
with one commit + an 'origin/main' ref synthesized via a bare upstream) and
the WORKSPACE_ROOT (monkey-patched to live under tmp_path).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def repo(tmp_path):
    """Create a real git repo with a 'main' branch and an 'origin' remote
    pointing at a bare clone, so `git worktree add origin/main` resolves.
    """
    upstream = tmp_path / "upstream.git"
    work = tmp_path / "project"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(upstream)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(work)],
        check=True,
        capture_output=True,
    )
    # Configure committer so commit doesn't fail in CI-like environments.
    for k, v in [("user.email", "test@example.invalid"), ("user.name", "Test")]:
        subprocess.run(
            ["git", "-C", str(work), "config", k, v], check=True, capture_output=True
        )
    (work / "README.md").write_text("# project\n")
    subprocess.run(
        ["git", "-C", str(work), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(upstream)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )
    return work


@pytest.fixture
def workspaces_mod(tmp_path, monkeypatch):
    """Reload workspaces with WORKSPACE_ROOT pointed at tmp_path."""
    import constants

    monkeypatch.setattr(
        constants, "DASHBOARD_DIR", tmp_path / ".claude" / "ticket-takeaway"
    )
    import importlib

    import workspaces

    importlib.reload(workspaces)
    return workspaces


# ---------------------------------------------------------------------------
# Pure helpers — sanitization, paths, branch names
# ---------------------------------------------------------------------------


class TestPureHelpers:
    def test_sanitize_strips_dangerous_chars(self, workspaces_mod):
        assert workspaces_mod._sanitize_key("a/b") == "a_b"
        assert (
            workspaces_mod._sanitize_key("..") == ".."
        )  # dots are allowed; path-escape blocked elsewhere
        assert workspaces_mod._sanitize_key("B-42") == "B-42"
        assert workspaces_mod._sanitize_key("sub id") == "sub_id"

    def test_workspace_path_is_deterministic(self, workspaces_mod):
        p1 = workspaces_mod.workspace_path_for("proj", "ticket", "B-42")
        p2 = workspaces_mod.workspace_path_for("proj", "ticket", "B-42")
        assert p1 == p2
        assert "proj" in str(p1) and "ticket" in str(p1) and "B-42" in str(p1)

    def test_branch_name_includes_subject_type(self, workspaces_mod):
        assert workspaces_mod._branch_name("ticket", "B-42") == "kitchen/ticket/B-42"
        assert workspaces_mod._branch_name("journey", "J-1") == "kitchen/journey/J-1"

    def test_normalize_base_ref_prefixes_origin(self, workspaces_mod):
        assert workspaces_mod._normalize_base_ref("main") == "origin/main"
        assert workspaces_mod._normalize_base_ref("origin/main") == "origin/main"
        assert (
            workspaces_mod._normalize_base_ref("upstream/dev") == "upstream/dev"
        )  # has '/' — used as-is
        assert workspaces_mod._normalize_base_ref("") == "origin/main"

    def test_path_outside_root_is_rejected(self, workspaces_mod, tmp_path):
        outside = tmp_path / "elsewhere" / "evil"
        with pytest.raises(ValueError, match="not inside workspace_root"):
            workspaces_mod._assert_inside_root(outside)


# ---------------------------------------------------------------------------
# create_or_reuse — real git worktree creation
# ---------------------------------------------------------------------------


class TestCreateOrReuse:
    def test_creates_worktree_for_git_repo(self, workspaces_mod, repo):
        info = workspaces_mod.create_or_reuse(
            repo, "proj", "ticket", "B-1", base_ref="origin/main"
        )
        assert info.is_git_worktree is True
        assert info.created_now is True
        assert info.bootstrapped is False
        assert info.path.exists()
        assert info.branch == "kitchen/ticket/B-1"
        # The README from the repo should be present in the worktree.
        assert (info.path / "README.md").exists()

    def test_reuse_returns_existing_path(self, workspaces_mod, repo):
        info1 = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        info2 = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        assert info1.path == info2.path
        assert info2.created_now is False

    def test_non_git_project_creates_plain_dir(self, workspaces_mod, tmp_path):
        plain = tmp_path / "not_git"
        plain.mkdir()
        info = workspaces_mod.create_or_reuse(plain, "proj", "ticket", "B-1")
        assert info.is_git_worktree is False
        assert info.created_now is True
        assert info.path.exists()
        # No git inside.
        assert not (info.path / ".git").exists()

    def test_bootstrapped_flag_reflects_marker_file(self, workspaces_mod, repo):
        info = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        assert info.bootstrapped is False
        workspaces_mod.mark_bootstrapped(info.path)
        info2 = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        assert info2.bootstrapped is True

    def test_branch_persists_across_runs(self, workspaces_mod, repo, tmp_path):
        """A second create_or_reuse for the same subject reuses the kitchen
        branch even after the worktree dir is removed (git keeps the branch)."""
        info1 = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        # Make a commit on the worktree's branch.
        (info1.path / "wip.txt").write_text("wip")
        subprocess.run(
            ["git", "-C", str(info1.path), "add", "wip.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(info1.path), "config", "user.email", "x@x"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(info1.path), "config", "user.name", "x"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(info1.path), "commit", "-m", "wip"],
            check=True,
            capture_output=True,
        )

        # Remove the worktree dir but leave the branch behind.
        workspaces_mod.remove(repo, "proj", "ticket", "B-1", force=True)
        assert not info1.path.exists()

        # Recreate — should reuse the same branch (so wip.txt comes back).
        info2 = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        assert info2.branch == info1.branch
        assert (info2.path / "wip.txt").exists()


# ---------------------------------------------------------------------------
# wipe_for_retry_fresh
# ---------------------------------------------------------------------------


class TestWipeForRetryFresh:
    def test_resets_to_base_and_clears_marker(self, workspaces_mod, repo):
        info = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        workspaces_mod.mark_bootstrapped(info.path)
        # Add some uncommitted gunk.
        (info.path / "junk.tmp").write_text("garbage")
        ok = workspaces_mod.wipe_for_retry_fresh(repo, "proj", "ticket", "B-1")
        assert ok
        assert not (info.path / "junk.tmp").exists()
        # Marker is gone so after_create runs again.
        assert not (info.path / workspaces_mod.BOOTSTRAP_MARKER).exists()

    def test_returns_false_when_workspace_missing(self, workspaces_mod, repo):
        assert (
            workspaces_mod.wipe_for_retry_fresh(repo, "proj", "ticket", "nope") is False
        )


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


class TestRemove:
    def test_removes_existing_worktree(self, workspaces_mod, repo):
        info = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        ok = workspaces_mod.remove(repo, "proj", "ticket", "B-1")
        assert ok
        assert not info.path.exists()

    def test_remove_missing_is_idempotent(self, workspaces_mod, repo):
        assert workspaces_mod.remove(repo, "proj", "ticket", "never-existed") is True

    def test_force_remove_dirty_worktree(self, workspaces_mod, repo):
        info = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        (info.path / "uncommitted.txt").write_text("x")
        ok = workspaces_mod.remove(repo, "proj", "ticket", "B-1", force=True)
        assert ok
        assert not info.path.exists()


# ---------------------------------------------------------------------------
# Hook execution
# ---------------------------------------------------------------------------


class TestHookExecution:
    def test_empty_script_is_noop_and_succeeds(self, workspaces_mod, repo):
        info = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        result = workspaces_mod.run_hook(info.path, "after_create", "")
        assert result.succeeded
        assert result.exit_code == 0
        assert result.stdout == ""

    def test_success_captures_stdout(self, workspaces_mod, repo):
        info = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        result = workspaces_mod.run_hook(
            info.path, "before_run", "echo hello-from-hook"
        )
        assert result.succeeded
        assert "hello-from-hook" in result.stdout

    def test_nonzero_exit_marks_failed(self, workspaces_mod, repo):
        info = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        result = workspaces_mod.run_hook(info.path, "after_create", "exit 7")
        assert not result.succeeded
        assert result.exit_code == 7
        assert result.timed_out is False

    def test_timeout_sets_timed_out_flag(self, workspaces_mod, repo):
        info = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        result = workspaces_mod.run_hook(
            info.path, "after_create", "sleep 5", timeout_ms=200
        )
        assert result.timed_out is True
        assert not result.succeeded

    def test_cwd_is_the_workspace_path(self, workspaces_mod, repo):
        # Inv1: hook MUST run with cwd=workspace_path.
        info = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        result = workspaces_mod.run_hook(info.path, "before_run", "pwd")
        assert result.succeeded
        # Workspace path may resolve differently on macOS (/var vs /private/var).
        # Compare resolved forms.
        assert Path(result.stdout.strip()).resolve() == info.path.resolve()

    def test_cannot_run_hook_outside_workspace_root(self, workspaces_mod, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with pytest.raises(ValueError):
            workspaces_mod.run_hook(outside, "after_create", "echo hi")

    def test_hook_env_inherits_by_default(self, workspaces_mod, repo, monkeypatch):
        info = workspaces_mod.create_or_reuse(repo, "proj", "ticket", "B-1")
        monkeypatch.setenv("KITCHEN_HOOK_TEST", "yes-i-am-here")
        result = workspaces_mod.run_hook(
            info.path, "before_run", "echo $KITCHEN_HOOK_TEST"
        )
        assert "yes-i-am-here" in result.stdout
