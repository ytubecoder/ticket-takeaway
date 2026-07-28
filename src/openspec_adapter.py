"""openspec_adapter.py — the ONLY place Ticket Takeaway shells out to `openspec`.

Everything that knows about the OpenSpec CLI lives here: the pinned version, the
telemetry opt-out, the argv shapes, and the JSON key names. Callers get plain
Python dicts and never see a subprocess.

Why the containment matters
---------------------------
`@fission-ai/openspec` ships roughly two releases a month, is maintained by one
person, and its own `docs/agent-contract.md` self-reports inconsistent JSON key
casing between commands. Scattering shell-outs through the codebase would mean a
version bump breaks N call sites. Here it breaks one, and the fixtures in
``fixtures/openspec/`` fail loudly when a shape moves.

Version pin
-----------
1.6.0 is the first release where ``archive`` and ``validate`` return reliable
exit codes — before it, ``archive`` exited 0 even when validation failed and
nothing was archived (upstream PR #1311). Gating on anything older is unsafe.
``check_cli()`` refuses a mismatched version rather than guessing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Pin + invocation environment
# ---------------------------------------------------------------------------

REQUIRED_VERSION = "1.6.0"
NPM_PACKAGE = "@fission-ai/openspec"

#: The bare `openspec` npm name is a dead 2019 squat (0.0.0, no bin). Anything
#: that installs or invokes it is a supply-chain mistake, so the fallback below
#: pins the scoped package explicitly.
_NPX_FALLBACK = ["npx", "--yes", f"{NPM_PACKAGE}@{REQUIRED_VERSION}"]

DEFAULT_TIMEOUT_MS = 120_000


class OpenSpecError(RuntimeError):
    """Raised when the OpenSpec CLI is missing, mispinned, or unparseable."""


@dataclass
class Result:
    """Outcome of one OpenSpec invocation.

    ``ok`` mirrors the process exit code, which is the thing the gates rest on.
    ``data`` is the parsed --json payload when there was one, else ``None``.
    """

    ok: bool
    exit_code: int
    data: Any = None
    stdout: str = ""
    stderr: str = ""
    argv: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        """Best available human-readable explanation, preferring structured status."""
        msgs = _status_messages(self.data)
        if msgs:
            return "; ".join(msgs)
        text = (self.stderr or "").strip() or (self.stdout or "").strip()
        return text.splitlines()[-1] if text else f"openspec exited {self.exit_code}"


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------


def _binary() -> list[str]:
    """Return argv[0:] for invoking OpenSpec, preferring an installed binary."""
    found = shutil.which("openspec")
    if found:
        return [found]
    return list(_NPX_FALLBACK)


def _env() -> dict[str, str]:
    """Process env with telemetry disabled.

    The CLI checks ``process.env.OPENSPEC_TELEMETRY === '0'`` — an exact string
    compare — so this must be the string "0". DO_NOT_TRACK is set as a belt-and-
    braces second opt-out that the same module honours.
    """
    env = dict(os.environ)
    env["OPENSPEC_TELEMETRY"] = "0"
    env["DO_NOT_TRACK"] = "1"
    return env


def _run(
    project_path: str | Path, args: list[str], timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> Result:
    """Run `openspec <args>` inside *project_path* and parse JSON when present."""
    argv = _binary() + list(args)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(project_path),
            env=_env(),
            capture_output=True,
            text=True,
            timeout=max(1, timeout_ms) / 1000.0,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment-specific
        raise OpenSpecError(
            f"OpenSpec CLI not found. Install with: npm install -g {NPM_PACKAGE}@{REQUIRED_VERSION}"
        ) from exc
    except subprocess.TimeoutExpired:
        return Result(
            ok=False,
            exit_code=124,
            data=None,
            stdout="",
            stderr=f"openspec timed out after {timeout_ms}ms",
            argv=argv,
        )

    data = _parse_json(proc.stdout)
    return Result(
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        data=data,
        stdout=proc.stdout,
        stderr=proc.stderr,
        argv=argv,
    )


def _parse_json(text: str) -> Any:
    """Parse a --json payload, tolerating leading non-JSON progress lines."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Some commands print a spinner line before the payload. Retry from the
    # first brace/bracket rather than losing the whole result to one stray line.
    for opener in ("{", "["):
        idx = text.find(opener)
        if idx > 0:
            try:
                return json.loads(text[idx:])
            except json.JSONDecodeError:
                continue
    return None


def _status_messages(data: Any) -> list[str]:
    """Pull human messages out of the `status` array some commands return.

    Key casing is inconsistent across OpenSpec commands (self-reported upstream),
    so this reads defensively rather than assuming one shape.
    """
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for entry in data.get("status") or []:
        if isinstance(entry, dict) and entry.get("message"):
            msg = str(entry["message"])
            if entry.get("fix"):
                msg = f"{msg} {entry['fix']}"
            out.append(msg)
    return out


# ---------------------------------------------------------------------------
# Naming — deterministic, so ticket <-> change maps both ways with no join table
# ---------------------------------------------------------------------------


def change_name(ticket_id: str, title: str) -> str:
    """Build the canonical change directory name for a ticket.

    ``B-44`` + ``Knowledge ingestion pipeline`` -> ``b-44-knowledge-ingestion-pipeline``

    Deterministic in both directions: :func:`ticket_id_from_change_name` recovers
    the ticket id, so no lookup table is needed and a rename can't orphan a link.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    tid = (ticket_id or "").lower().strip()
    if not slug:
        return tid
    return f"{tid}-{slug}"[:80].rstrip("-")


def ticket_id_from_change_name(name: str) -> str:
    """Recover the ticket id from a change name produced by :func:`change_name`."""
    m = re.match(r"^([a-z]+-\d+)", (name or "").lower())
    return m.group(1).upper() if m else ""


# ---------------------------------------------------------------------------
# Project-level probes (cheap, no subprocess)
# ---------------------------------------------------------------------------


def is_initialised(project_path: str | Path) -> bool:
    """True when *project_path* has an `openspec/` root with a config."""
    return (Path(project_path) / "openspec" / "config.yaml").is_file()


def change_dir(project_path: str | Path, name: str) -> Path:
    return Path(project_path) / "openspec" / "changes" / name


def change_exists(project_path: str | Path, name: str) -> bool:
    return change_dir(project_path, name).is_dir()


def archived_change_dirs(project_path: str | Path, name: str) -> list[Path]:
    """Archived copies of *name* — archive prefixes the directory with a date."""
    root = Path(project_path) / "openspec" / "changes" / "archive"
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir() if p.is_dir() and p.name.endswith(f"-{name}")
    )


def has_spec_delta(project_path: str | Path, name: str) -> bool:
    """True when the change carries at least one non-empty spec delta file."""
    specs = change_dir(project_path, name) / "specs"
    if not specs.is_dir():
        return False
    return any(
        p.is_file() and p.read_text(encoding="utf-8").strip()
        for p in specs.rglob("*.md")
    )


# ---------------------------------------------------------------------------
# CLI operations
# ---------------------------------------------------------------------------


def version(project_path: str | Path = ".") -> str:
    """Return the installed OpenSpec version string (may be empty)."""
    res = _run(project_path, ["--version"], timeout_ms=30_000)
    return (res.stdout or "").strip()


def check_cli(project_path: str | Path = ".") -> tuple[bool, str]:
    """Verify OpenSpec is present and pinned to :data:`REQUIRED_VERSION`.

    Returns ``(ok, reason)``. A mismatched version is a refusal, not a warning:
    the accept gate's guarantees are version-specific.
    """
    try:
        found = version(project_path)
    except OpenSpecError as exc:
        return (False, str(exc))
    if not found:
        return (False, f"openspec not found — install {NPM_PACKAGE}@{REQUIRED_VERSION}")
    if found != REQUIRED_VERSION:
        return (
            False,
            (
                f"openspec {found} installed, gates require {REQUIRED_VERSION} "
                f"(npm install -g {NPM_PACKAGE}@{REQUIRED_VERSION})"
            ),
        )
    return (True, f"openspec {found}")


def init(project_path: str | Path, tools: str = "claude") -> Result:
    """Run `openspec init --tools <tools>`.

    Never passes --force: that deletes legacy command directories unprompted.
    """
    return _run(project_path, ["init", "--tools", tools], timeout_ms=180_000)


def new_change(project_path: str | Path, name: str) -> Result:
    """Create `openspec/changes/<name>/` via the CLI (not by hand)."""
    if change_exists(project_path, name):
        return Result(
            ok=True,
            exit_code=0,
            data=None,
            stdout=f"change {name} already exists",
            argv=[],
        )
    return _run(project_path, ["new", "change", name])


def status(project_path: str | Path, name: str) -> Result:
    """Artifact completion status for a change (`openspec status --json`).

    Payload is camelCase here: changeName / artifactPaths / isComplete /
    applyRequires, plus an `artifacts` array of {id, outputPath, status}.
    """
    return _run(project_path, ["status", "--change", name, "--json"])


def artifact_states(result: Result) -> dict[str, str]:
    """Map artifact id -> status ('ready' | 'blocked' | 'complete' | ...)."""
    data = result.data
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for art in data.get("artifacts") or []:
        if isinstance(art, dict) and art.get("id"):
            out[str(art["id"])] = str(art.get("status", ""))
    return out


def instructions(project_path: str | Path, artifact: str, name: str) -> Result:
    """Enriched template + config.yaml context for one artifact.

    This is what the Orchestrator interview drives, rather than the agent
    inventing a document shape.
    """
    return _run(project_path, ["instructions", artifact, "--change", name, "--json"])


def validate(
    project_path: str | Path, name: str | None = None, strict: bool = True
) -> Result:
    """Validate one change, or everything when *name* is None.

    Exit code is the contract: 0 = valid, non-zero = at least one ERROR. Verified
    against 1.6.0 rather than trusted from the docs.
    """
    args = ["validate"]
    args += [name] if name else ["--all"]
    if strict:
        args.append("--strict")
    args += ["--json", "--no-interactive"]
    return _run(project_path, args)


def validation_errors(result: Result) -> list[str]:
    """Flatten a validate payload into 'path: message' strings for ERROR issues."""
    data = result.data
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for item in data.get("items") or []:
        if not isinstance(item, dict) or item.get("valid"):
            continue
        for issue in item.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            if str(issue.get("level", "")).upper() != "ERROR":
                continue
            where = issue.get("path") or item.get("id") or "?"
            out.append(f"{where}: {issue.get('message', '')}".strip())
    return out


def archive(project_path: str | Path, name: str, skip_specs: bool = False) -> Result:
    """Merge a change's spec delta into `openspec/specs/` and file the change.

    Always validates first (never `--no-validate`) — the merge into canon is the
    one irreversible step, so it must not happen on a broken delta.
    """
    args = ["archive", name, "-y", "--json"]
    if skip_specs:
        args.append("--skip-specs")
    return _run(project_path, args, timeout_ms=180_000)


def archive_summary(result: Result) -> dict[str, Any]:
    """Extract the `archive` object from an archive payload ({} when refused)."""
    data = result.data
    if isinstance(data, dict) and isinstance(data.get("archive"), dict):
        return data["archive"]
    return {}


def list_specs(project_path: str | Path) -> Result:
    """List canonical specs (`openspec list --specs`)."""
    return _run(project_path, ["list", "--specs"])
