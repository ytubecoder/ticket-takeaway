"""Workspace manager — per-subject git worktrees + lifecycle hooks.

See docs/KITCHEN.md §10 + §10b. Each Kitchen subject gets a deterministic
filesystem workspace, optionally a git worktree on a kitchen/* branch. Hooks
(after_create, before_run, after_run, before_remove) run with bash -lc and
cwd=workspace_path; failure semantics per Symphony §9.4.

Safety invariants (§10.5) enforced before launching anything:
    Inv1: subprocess cwd MUST equal workspace_path
    Inv2: workspace_path MUST live inside workspace_root
    Inv3: workspace key sanitized to [A-Za-z0-9._-]
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from constants import DASHBOARD_DIR

WORKSPACE_ROOT = DASHBOARD_DIR / "workspaces"
BOOTSTRAP_MARKER = ".kitchen-bootstrap-complete"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _sanitize_key(key: str) -> str:
    """Inv3: only [A-Za-z0-9._-] allowed in path components."""
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in key)


def workspace_path_for(project_id: str, subject_type: str, subject_id: str) -> Path:
    """Return the deterministic workspace path for a subject. Does not create it."""
    return WORKSPACE_ROOT / _sanitize_key(project_id) / _sanitize_key(subject_type) / _sanitize_key(subject_id)


def _branch_name(subject_type: str, subject_id: str) -> str:
    """§10b: kitchen/{subject_type}/{subject_id} — type prefix prevents
    numeric-id collisions between tickets and journeys."""
    return f"kitchen/{_sanitize_key(subject_type)}/{_sanitize_key(subject_id)}"


def _normalize_base_ref(base_ref: str) -> str:
    """If base_ref already starts with 'origin/', use as-is; otherwise prefix.

    Per §10b: WORKFLOW.toml's agent.base_ref defaults to 'origin/main'. The
    fully-qualified form goes into `git worktree add`.
    """
    if not base_ref:
        return "origin/main"
    if base_ref.startswith("origin/") or "/" in base_ref:
        return base_ref
    return f"origin/{base_ref}"


def _assert_inside_root(path: Path) -> None:
    """Inv2: every workspace path MUST live inside WORKSPACE_ROOT.

    Refuse to operate on paths that escape the root via .. or absolute trickery.
    """
    root = WORKSPACE_ROOT.resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        raise ValueError(f"workspace path {path!r} is not inside workspace_root {root!r}")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkspaceInfo:
    """Returned by create_or_reuse — the workspace and how it was provisioned."""
    path: Path
    branch: str               # kitchen/{type}/{id}
    base_ref: str             # fully-qualified, e.g. origin/main
    is_git_worktree: bool     # False if project isn't a git repo (clone fallback skipped in M3)
    created_now: bool         # True iff this call created the dir (gates after_create)
    bootstrapped: bool        # True iff the marker file exists post-call


@dataclass(frozen=True)
class HookResult:
    """Outcome of one hook invocation. Captured streams live in stdout/stderr."""
    hook: str                 # 'after_create' | 'before_run' | 'after_run' | 'before_remove'
    exit_code: int            # 0 = success
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(path), capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _git_fetch(repo_path: Path, remote: str = "origin", timeout: int = 60) -> tuple[bool, str]:
    """Best-effort fetch. Returns (ok, stderr-or-empty)."""
    try:
        r = subprocess.run(
            ["git", "fetch", remote],
            cwd=str(repo_path), capture_output=True, text=True, timeout=timeout,
        )
        return (r.returncode == 0, r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return (False, str(e))


def _branch_exists(repo_path: Path, branch: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(repo_path), capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _git_worktree_add(repo_path: Path, target: Path, branch: str, base_ref: str) -> tuple[bool, str]:
    """Create a worktree at *target* on *branch*, branched from *base_ref*.

    Reuses *branch* if it already exists (no -b); otherwise creates it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo_path, branch):
        cmd = ["git", "worktree", "add", str(target), branch]
    else:
        cmd = ["git", "worktree", "add", "-b", branch, str(target), base_ref]
    try:
        r = subprocess.run(cmd, cwd=str(repo_path), capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return (True, "")
        return (False, (r.stderr or r.stdout or "").strip())
    except (OSError, subprocess.SubprocessError) as e:
        return (False, str(e))


def _git_worktree_remove(repo_path: Path, target: Path, force: bool = False) -> tuple[bool, str]:
    cmd = ["git", "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(target))
    try:
        r = subprocess.run(cmd, cwd=str(repo_path), capture_output=True, text=True, timeout=30)
        return (r.returncode == 0, (r.stderr or r.stdout or "").strip())
    except (OSError, subprocess.SubprocessError) as e:
        return (False, str(e))


def _git_reset_clean(target: Path, base_ref: str) -> tuple[bool, str]:
    """Wipe worktree state to match *base_ref* exactly. Used for retry-fresh."""
    try:
        r1 = subprocess.run(
            ["git", "reset", "--hard", base_ref],
            cwd=str(target), capture_output=True, text=True, timeout=30,
        )
        if r1.returncode != 0:
            return (False, (r1.stderr or r1.stdout or "").strip())
        r2 = subprocess.run(
            ["git", "clean", "-fdx"],
            cwd=str(target), capture_output=True, text=True, timeout=30,
        )
        if r2.returncode != 0:
            return (False, (r2.stderr or r2.stdout or "").strip())
        return (True, "")
    except (OSError, subprocess.SubprocessError) as e:
        return (False, str(e))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_or_reuse(
    repo_path: Path,
    project_id: str,
    subject_type: str,
    subject_id: str,
    base_ref: str = "origin/main",
    fetch: bool = True,
) -> WorkspaceInfo:
    """Create a per-subject workspace (git worktree) or reuse an existing one.

    For git repos: creates ~/.claude/ticket-takeaway/workspaces/{project}/{type}/{id}
    as a `git worktree` on branch kitchen/{type}/{id} forked from base_ref.

    For non-git project paths: creates a plain directory (clone fallback skipped
    in M3 — agents that need history can't run there). is_git_worktree=False.

    Idempotent: calling twice for the same subject returns the same path with
    created_now=False the second time.

    Raises ValueError on path-escape (Inv2) or git failure.
    """
    target = workspace_path_for(project_id, subject_type, subject_id)
    _assert_inside_root(target)
    branch = _branch_name(subject_type, subject_id)
    fq_base = _normalize_base_ref(base_ref)

    already_existed = target.exists()
    bootstrapped = (target / BOOTSTRAP_MARKER).exists()

    if already_existed:
        # Reuse path. Don't touch git — caller is responsible for any sync hooks.
        return WorkspaceInfo(
            path=target, branch=branch, base_ref=fq_base,
            is_git_worktree=_is_git_repo(target),
            created_now=False,
            bootstrapped=bootstrapped,
        )

    is_git = _is_git_repo(repo_path)
    if not is_git:
        target.mkdir(parents=True, exist_ok=True)
        return WorkspaceInfo(
            path=target, branch=branch, base_ref=fq_base,
            is_git_worktree=False, created_now=True,
            bootstrapped=False,
        )

    if fetch:
        # Best-effort. If origin is unreachable we still try — the worktree add
        # will fail clearly if base_ref is unresolvable.
        _git_fetch(repo_path)

    ok, err = _git_worktree_add(repo_path, target, branch, fq_base)
    if not ok:
        # Roll back any partial directory.
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise ValueError(f"git worktree add failed: {err}")

    return WorkspaceInfo(
        path=target, branch=branch, base_ref=fq_base,
        is_git_worktree=True, created_now=True,
        bootstrapped=False,
    )


def mark_bootstrapped(workspace_path: Path) -> None:
    """Drop the marker that prevents after_create from running again."""
    _assert_inside_root(workspace_path)
    (workspace_path / BOOTSTRAP_MARKER).touch()


def remove(repo_path: Path, project_id: str, subject_type: str, subject_id: str,
           force: bool = False) -> bool:
    """Remove the worktree (or plain directory) for a subject. Best-effort.

    Returns True iff the path no longer exists after the call.
    """
    target = workspace_path_for(project_id, subject_type, subject_id)
    _assert_inside_root(target)
    if not target.exists():
        return True
    if _is_git_repo(repo_path):
        ok, _err = _git_worktree_remove(repo_path, target, force=force)
        if not ok and target.exists():
            shutil.rmtree(target, ignore_errors=True)
    else:
        shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


def wipe_for_retry_fresh(repo_path: Path, project_id: str, subject_type: str,
                         subject_id: str, base_ref: str = "origin/main") -> bool:
    """Reset a worktree to base_ref + clean — used for "retry fresh".

    Removes the bootstrap marker so after_create runs again on next attempt.
    Returns True on success.
    """
    target = workspace_path_for(project_id, subject_type, subject_id)
    _assert_inside_root(target)
    if not target.exists():
        return False
    if not _is_git_repo(target):
        # Plain dir — nuke and recreate empty.
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        return True
    fq_base = _normalize_base_ref(base_ref)
    ok, _err = _git_reset_clean(target, fq_base)
    if ok:
        marker = target / BOOTSTRAP_MARKER
        if marker.exists():
            marker.unlink()
    return ok


# ---------------------------------------------------------------------------
# Hook execution
# ---------------------------------------------------------------------------

def run_hook(
    workspace_path: Path,
    hook_name: str,
    script: str,
    timeout_ms: int = 60000,
    env: Optional[dict] = None,
) -> HookResult:
    """Execute a hook script with `bash -lc` from the workspace.

    Inv1 enforced: subprocess cwd is set to workspace_path. Workspace must
    exist and be inside WORKSPACE_ROOT.

    `script` is the literal shell script body (multi-line allowed).
    Empty/whitespace-only script is a no-op (returns succeeded=True).

    Per Symphony §9.4 / docs/KITCHEN.md §10:
      - after_create / before_run failure or timeout → fatal (caller decides)
      - after_run / before_remove failure or timeout → log and ignore
    The semantics of "fatal" live in the caller; this function just returns
    the HookResult.
    """
    import time as _time
    _assert_inside_root(workspace_path)
    if not workspace_path.exists():
        raise ValueError(f"workspace_path does not exist: {workspace_path}")
    body = (script or "").strip()
    if not body:
        return HookResult(hook=hook_name, exit_code=0, stdout="", stderr="",
                          duration_ms=0, timed_out=False)

    timeout_s = max(0.001, timeout_ms / 1000.0)
    started = _time.monotonic()
    try:
        r = subprocess.run(
            ["bash", "-lc", body],
            cwd=str(workspace_path),
            capture_output=True, text=True, timeout=timeout_s,
            env=env,  # None = inherit
        )
        elapsed = int((_time.monotonic() - started) * 1000)
        return HookResult(
            hook=hook_name, exit_code=r.returncode,
            stdout=r.stdout or "", stderr=r.stderr or "",
            duration_ms=elapsed, timed_out=False,
        )
    except subprocess.TimeoutExpired as e:
        elapsed = int((_time.monotonic() - started) * 1000)
        return HookResult(
            hook=hook_name, exit_code=-1,
            stdout=(e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")),
            stderr=(e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")),
            duration_ms=elapsed, timed_out=True,
        )
