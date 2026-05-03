#!/usr/bin/env python3
"""Ticket Takeaway installer/upgrader.

Usage:
    python3 install.py                    # Install/upgrade system files + skills
    python3 install.py --register         # Also register current project
    python3 install.py --register --id myproject --name "My Project"
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent
VALID_TARGETS = ("auto", "claude", "codex", "both", "none")
CODEX_CURRENT_ENV = (
    "CODEX_CI",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "CODEX_THREAD_ID",
)
CLAUDE_CURRENT_ENV_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE")
SKILL_SOURCES = [
    ("ticket-takeaway", "src/skills/ticket-takeaway/SKILL.md"),
    ("review", "src/skills/review/SKILL.md"),
    ("spec", "src/skills/spec/SKILL.md"),
    ("accept", "src/skills/accept/SKILL.md"),
    ("feedbacks", "src/skills/feedbacks/SKILL.md"),
]


def _home(home: Path | str | None = None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def _env(env=None):
    return os.environ if env is None else env


def get_claude_home(home: Path | str | None = None) -> Path:
    return _home(home) / ".claude"


def get_codex_home(env=None, home: Path | str | None = None) -> Path:
    env = _env(env)
    configured = env.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else _home(home) / ".codex"


def get_skill_dir(agent: str, env=None, home: Path | str | None = None) -> Path:
    if agent == "claude":
        return get_claude_home(home) / "skills"
    if agent == "codex":
        return get_codex_home(env, home) / "skills"
    raise ValueError(f"Unsupported skill target: {agent}")


def detect_auto_target(env=None, home: Path | str | None = None) -> str:
    """Resolve --target auto to one agent, avoiding cross-agent skill writes."""
    env = _env(env)
    explicit = env.get("TICKET_TAKEAWAY_AGENT", "").strip().lower()
    if explicit in {"claude", "codex"}:
        return explicit

    codex_current = any(key in env for key in CODEX_CURRENT_ENV)
    claude_current = any(key.startswith(CLAUDE_CURRENT_ENV_PREFIXES) for key in env)
    if codex_current and not claude_current:
        return "codex"
    if claude_current and not codex_current:
        return "claude"
    if env.get("CODEX_HOME") and not claude_current:
        return "codex"

    codex_present = get_codex_home(env, home).exists()
    claude_present = get_claude_home(home).exists()
    if codex_present and not claude_present:
        return "codex"

    # Historical default: when ambiguous or fresh, install Claude skills.
    return "claude"


def resolve_skill_targets(target: str = "auto", env=None, home: Path | str | None = None):
    """Return (agent, skills_dir) pairs for the requested skill install target."""
    if target not in VALID_TARGETS:
        raise ValueError(f"Unsupported skill target: {target}")
    if target == "none":
        return []
    if target == "both":
        agents = ["claude", "codex"]
    elif target == "auto":
        agents = [detect_auto_target(env, home)]
    else:
        agents = [target]
    return [(agent, get_skill_dir(agent, env, home)) for agent in agents]


INSTALL_DIR = Path.home() / ".claude" / "ticket-takeaway"
DASHBOARD_DIR = Path.home() / ".claude" / "dashboard"
SKILLS_DIR = Path.home() / ".claude" / "skills"
DB_PATH = INSTALL_DIR / "tickets.db"
REGISTRY_PATH = INSTALL_DIR / "registry.json"


def copy_file(src: Path, dst: Path, label: str = ""):
    """Copy a file, creating parent dirs as needed."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if label:
        print(f"  {label}: {dst}")


def install_system_files(target: str = "auto") -> list[str]:
    """Copy CLI, generator, and skills to runtime locations."""
    print("Installing system files...")
    skill_targets = resolve_skill_targets(target)

    # Core files
    copy_file(REPO_DIR / "src" / "tickets-cli.py", INSTALL_DIR / "tickets-cli.py", "CLI")
    copy_file(REPO_DIR / "src" / "generate.py", INSTALL_DIR / "generate.py", "Generator")
    copy_file(REPO_DIR / "src" / "serve.py", INSTALL_DIR / "serve.py", "Server")
    copy_file(REPO_DIR / "src" / "actions.py", INSTALL_DIR / "actions.py", "Actions")
    copy_file(REPO_DIR / "src" / "constants.py", INSTALL_DIR / "constants.py", "Constants")
    copy_file(REPO_DIR / "src" / "db.py", INSTALL_DIR / "db.py", "Database")
    copy_file(REPO_DIR / "src" / "conditions.py", INSTALL_DIR / "conditions.py", "Conditions")
    copy_file(REPO_DIR / "src" / "workflows_seed.py", INSTALL_DIR / "workflows_seed.py", "Workflows seeder")

    # Dashboard copy (needs DASHBOARD_DIR path fix)
    dashboard_gen = DASHBOARD_DIR / "generate.py"
    copy_file(REPO_DIR / "src" / "generate.py", dashboard_gen, "Generator (dashboard)")
    # Patch DASHBOARD_DIR in the dashboard copy
    text = dashboard_gen.read_text(encoding="utf-8")
    text = text.replace(
        'DASHBOARD_DIR = Path.home() / ".claude" / "ticket-takeaway"',
        'DASHBOARD_DIR = Path.home() / ".claude" / "dashboard"',
    )
    dashboard_gen.write_text(text, encoding="utf-8")

    # Skills
    if not skill_targets:
        print("  Skills: skipped (--target none)")
    for agent, skills_dir in skill_targets:
        for skill_name, src_path in SKILL_SOURCES:
            src = REPO_DIR / src_path
            if src.exists():
                dst = skills_dir / skill_name / "SKILL.md"
                copy_file(src, dst, f"Skill ({agent}): /{skill_name}")

    # Registry template (only if registry doesn't exist yet)
    example = REPO_DIR / "src" / "registry.example.json"
    if not REGISTRY_PATH.exists() and example.exists():
        copy_file(example, REGISTRY_PATH, "Registry (new)")
    elif REGISTRY_PATH.exists():
        print(f"  Registry: kept existing {REGISTRY_PATH}")

    print("System files installed.")
    return [agent for agent, _ in skill_targets]


def register_project(project_id: str = None, project_name: str = None, project_path: str = None):
    """Register the current (or specified) project in the registry."""
    if project_path is None:
        project_path = os.getcwd()

    # Auto-detect id and name from the directory
    if project_id is None:
        project_id = os.path.basename(project_path).lower().replace(" ", "-")
    if project_name is None:
        project_name = os.path.basename(project_path)

    # Load or create registry
    if REGISTRY_PATH.exists():
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            registry = json.load(f)
    else:
        registry = {"projects": []}

    # Check if already registered
    for p in registry["projects"]:
        if p["id"] == project_id:
            # Update path if changed
            p["path"] = project_path
            p["name"] = project_name
            print(f"Updated existing registration: {project_id} -> {project_path}")
            with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                json.dump(registry, f, indent=2)
            return

    # Add new entry
    registry["projects"].append({
        "id": project_id,
        "name": project_name,
        "path": project_path,
        "description": "",
        "active": True,
    })

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)

    print(f"Registered: {project_id} ({project_name}) -> {project_path}")


def seed_project(project_id: str = None):
    """Seed the DB from existing PRODUCT_BACKLOG.md."""
    cli = str(INSTALL_DIR / "tickets-cli.py")
    cmd = [sys.executable, cli, "seed"]
    if project_id:
        cmd.extend(["--project", project_id])
    subprocess.run(cmd, check=False)


def main():
    parser = argparse.ArgumentParser(
        description="Install or upgrade Ticket Takeaway"
    )
    parser.add_argument("--register", action="store_true",
                        help="Register the current project in the registry")
    parser.add_argument("--id", help="Project ID (default: directory name)")
    parser.add_argument("--name", help="Project display name (default: directory name)")
    parser.add_argument("--path", help="Project path (default: current directory)")
    parser.add_argument("--no-seed", action="store_true",
                        help="Skip seeding the DB from markdown")
    parser.add_argument("--target", choices=VALID_TARGETS, default="auto",
                        help=("Skill install target: auto detects the current agent, "
                              "claude installs to ~/.claude/skills, codex installs to "
                              "$CODEX_HOME/skills or ~/.codex/skills, both installs both, "
                              "none skips skills"))

    args = parser.parse_args()

    # Always install/upgrade system files
    installed_targets = install_system_files(args.target)
    print()

    # Register if requested
    if args.register:
        register_project(args.id, args.name, args.path)

        # Seed the DB from markdown
        if not args.no_seed:
            print()
            project_id = args.id or os.path.basename(args.path or os.getcwd()).lower().replace(" ", "-")
            seed_project(project_id)

        # Seed default system workflows (idempotent)
        try:
            sys.path.insert(0, str(INSTALL_DIR))
            from db import get_db, init_db  # type: ignore[import]
            from workflows_seed import seed_default_workflows  # type: ignore[import]
            _conn = get_db()
            init_db(_conn)
            _pid = args.id or os.path.basename(args.path or os.getcwd()).lower().replace(" ", "-")
            _res = seed_default_workflows(_conn, _pid)
            _conn.close()
            if _res["inserted"]:
                print(f"  Seeded {_res['inserted']} default workflow(s) for project {_pid!r}")
        except Exception as _e:
            print(f"  Warning: could not seed default workflows: {_e}")

    print()
    print("Done. Run /dashboard to generate the board.")
    if "claude" in installed_targets:
        print("Restart Claude Code to pick up updated skills.")
    if "codex" in installed_targets:
        print("Restart Codex to pick up updated skills.")


if __name__ == "__main__":
    main()
