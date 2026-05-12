# Model Endpoint Abstraction Implementation Plan

**Status:** shipped 2026-05-12, prod-live (merge commit `70aee2b`, PR #11). See § Shipped (as built) at the bottom for deltas.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `workflow_agents` into Agent (persona + system_prompt) + Endpoint (runtime: command/args/capabilities) layers, ship CLI endpoint execution covering claude/codex/hermes, leave the schema seam for API/SSH endpoint types.

**Architecture:** New `endpoints` table referenced by `workflow_agents.endpoint_id`. New `src/endpoints.py` module owns invocation building + session-id extraction (replacing the hardcoded `_build_agent_cmd` / `_apply_resume_args` / `_extract_session_id` dispatch in `serve.py`). Migration #19 splits existing data and pins known system runtimes to canonical seeded endpoint ids. Compat fallback on `workflow_agents.command`/`args` fires only when `endpoint_id IS NULL`.

**Tech Stack:** Python 3.10+ stdlib only (sqlite3, http.server, json, re, subprocess, pathlib), pytest + Playwright for tests.

**Spec:** `docs/superpowers/specs/2026-05-10-model-endpoint-abstraction-design.md` (revised 2026-05-11 after 2-round Codex review).

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/endpoints.py` | Create | Endpoint dataclass, CRUD, build_invocation, extract_session_id |
| `src/db.py` | Modify | Migration #19 (endpoints table + workflow_agents.endpoint_id + data backfill) |
| `src/workflows_seed.py` | Modify | `DEFAULT_ENDPOINTS` list; `DEFAULT_AGENTS` rewired to reference endpoint ids |
| `src/runners.py` | Modify | Route through `endpoints.build_invocation()`; drop command-name dispatch |
| `src/serve.py` | Modify | Remove `_build_agent_cmd` / `_apply_resume_args` / `_extract_session_id`; add `/api/endpoints` CRUD; update `/workflows` HTML |
| `src/tickets-cli.py` | Modify | Add `endpoint` subcommand group; add `--endpoint-id` to agent commands |
| `src/compare_seed_to_db.py` | Modify | Add `_audit_endpoints(conn)` |
| `tests/test_tdd_endpoints.py` | Create | TDD coverage for endpoints module + migration #20 |
| `tests/test_smoke_endpoints.py` | Create | API smoke tests for `/api/endpoints` |
| `tests/test_tdd_consultant_seed.py` | Modify | Update assertions for post-migration schema (endpoint_id present) |
| `tests/test_tdd_lane_a_primitives.py` | Modify | Update assertions for post-migration schema |
| `tests/test_tdd_workflows_seed.py` | Modify | Update assertions for post-migration schema |
| `CLAUDE.md` | Modify | Rewrite Workflow Bounce section for agent/endpoint split |

Files NOT modified: `src/actions.py`, `src/constants.py`, `src/journeys.py`, `src/kitchen.py`, `src/generate.py`, `src/scenarios.py`.

---

## Critical Invariants

These MUST hold throughout implementation:

1. **Single-transaction migration**: Migration #19 wraps CREATE TABLE + ALTER + data backfill + _migrations INSERT in one `conn.commit()`. Partial state must be impossible.
2. **FK pragma active**: Verify `PRAGMA foreign_keys=ON` still set after migration runs (it's set at connection time per `src/db.py:25`). Migration must not toggle it off.
3. **Migration grouping key**: `(command, effective_argv, system)` — never just `(command, args)`. User agents must not be locked behind system endpoints.
4. **Canonical id mapping**: Known system runtimes (claude/[], codex/[exec,-s,read-only]) map directly to canonical seeded ids (`claude-cli`, `codex-exec-readonly`) — no duplicate endpoints created and then collided with by the seed.
5. **Compat fallback ONLY for `endpoint_id IS NULL`**: A non-NULL `endpoint_id` pointing at a missing or non-cli endpoint is a hard error with no fallback.
6. **No live `_build_agent_cmd` call from the migration**: The migration carries a pinned helper that reproduces today's `_build_agent_cmd` logic. The live function is removed in Phase E, but the migration must replay correctly forever.

---

## Phase A — Seed Scaffolding

The migration's "canonical id mapping" step (Task 8) needs `DEFAULT_ENDPOINTS` to exist. Add the seed data first.

### Task 1: Add `Endpoint` dataclass and `DEFAULT_ENDPOINTS` to `workflows_seed.py`

**Files:**
- Modify: `src/workflows_seed.py` (add Endpoint dataclass + DEFAULT_ENDPOINTS list)

- [ ] **Step 1: Read the existing module to find where DEFAULT_AGENTS is defined**

Run: `grep -n "^DEFAULT_AGENTS\|^@dataclass\|^class " src/workflows_seed.py`

Expected: a `DEFAULT_AGENTS` list around line 31 and (probably) an `@dataclass` for `Agent` nearby.

- [ ] **Step 2: Add the Endpoint dataclass above DEFAULT_AGENTS**

In `src/workflows_seed.py`, insert near the top of the module (after the `@dataclass` for `Agent`, before `DEFAULT_AGENTS`):

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Endpoint:
    """Seed-time representation of an endpoints table row.

    Mirrors the SQL columns in migration 20. JSON-shaped fields (args,
    capabilities, session_config) are held as Python types here and
    json.dumps()'d at upsert time.
    """
    id: str
    name: str
    endpoint_type: str = "cli"
    command: Optional[str] = None
    args: list = field(default_factory=list)
    prompt_mode: str = "template"
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    timeout_s: int = 120
    capabilities: dict = field(default_factory=dict)
    session_config: dict = field(default_factory=dict)
    system: int = 0
```

- [ ] **Step 3: Add the DEFAULT_ENDPOINTS list immediately above DEFAULT_AGENTS**

```python
DEFAULT_ENDPOINTS: list[Endpoint] = [
    Endpoint(
        id="claude-cli",
        name="Claude CLI",
        endpoint_type="cli",
        system=1,
        command="claude",
        args=["-p", "{prompt}", "--output-format", "json"],
        prompt_mode="template",
        capabilities={"sessions": True},
        session_config={
            "resume_args": ["-p", "{prompt}", "--output-format", "json",
                            "--resume", "{session_id}"],
            "session_id_regex": r'"session_id"\s*:\s*"([0-9a-f-]+)"',
        },
    ),
    Endpoint(
        id="codex-cli",
        name="Codex CLI",
        endpoint_type="cli",
        system=1,
        command="codex",
        args=["{prompt}"],
        prompt_mode="template",
        capabilities={"sessions": True},
        session_config={
            "resume_args": ["exec", "resume", "{session_id}"],
            "session_id_regex": r"Session(?:\s+ID)?\s*:\s*([0-9a-f-]+)",
            "session_id_fallback_dir": "~/.codex/sessions/",
        },
    ),
    Endpoint(
        id="codex-exec-readonly",
        name="Codex exec (read-only)",
        endpoint_type="cli",
        system=1,
        command="codex",
        args=["exec", "-s", "read-only", "{prompt}"],
        prompt_mode="template",
        capabilities={"sessions": False},
    ),
    Endpoint(
        id="hermes-cli",
        name="Hermes CLI",
        endpoint_type="cli",
        system=1,
        command="hermes",
        args=["chat", "-q", "{prompt}"],
        prompt_mode="template",
        capabilities={"sessions": False},
    ),
]
```

- [ ] **Step 4: Add a `KNOWN_CLI_MAPPINGS` lookup the migration will use**

Add immediately after `DEFAULT_ENDPOINTS`:

```python
# Maps legacy (command, raw_args_tuple) -> canonical endpoint id.
# Used by migration #20 to pin known system runtimes to seeded ids
# instead of synthesising duplicate endpoints. raw_args_tuple is the
# value stored in workflow_agents.args BEFORE _build_agent_cmd's
# runner-side flag injection.
KNOWN_CLI_MAPPINGS: dict[tuple, str] = {
    ("claude", ()): "claude-cli",
    ("codex", ()): "codex-cli",
    ("codex", ("exec", "-s", "read-only")): "codex-exec-readonly",
    ("hermes", ()): "hermes-cli",
    ("hermes", ("chat",)): "hermes-cli",
}
```

- [ ] **Step 5: Commit**

```bash
git add src/workflows_seed.py
git commit -m "feat(seed): add Endpoint dataclass + DEFAULT_ENDPOINTS for migration 20"
git push
```

---

### Task 2: Rewire `DEFAULT_AGENTS` to reference endpoint ids

**Files:**
- Modify: `src/workflows_seed.py:31-172` (the DEFAULT_AGENTS list)

- [ ] **Step 1: Inspect the Agent dataclass**

Run: `grep -n "class Agent\|@dataclass" src/workflows_seed.py | head -10`

Find the `Agent` dataclass definition. It currently has `command: str`, `args: list`, `system_prompt: str` etc.

- [ ] **Step 2: Add `endpoint_id` field to the Agent dataclass**

In the `Agent` dataclass (whatever the line is), add:

```python
    endpoint_id: Optional[str] = None
```

Place it after `name` and before `command` so the field order is `id, name, endpoint_id, command, ...`. Leave `command` and `args` in place — they remain populated for the compat fallback and for `compare_seed_to_db.py` to read.

- [ ] **Step 3: Add `endpoint_id` to each DEFAULT_AGENTS entry**

For each Agent literal in `DEFAULT_AGENTS`, add the `endpoint_id` kwarg per this mapping:

| Agent id | endpoint_id |
|---|---|
| `agent_planner` | `"claude-cli"` |
| `agent_consultant` | `"codex-exec-readonly"` |
| `agent_orchestrator` | `"claude-cli"` |
| `agent_worker` | `"claude-cli"` |
| `agent_summarizer` | `"claude-cli"` |
| `agent_validator` | `"claude-cli"` |

Example for one entry:

```python
Agent(
    id="agent_planner",
    name="Planner",
    endpoint_id="claude-cli",   # ← NEW
    command="claude",            # ← retained for compat
    args=[],                     # ← retained for compat
    system_prompt="You are a practical implementation planner...",
    persist_session=1,
),
```

- [ ] **Step 4: Update `seed_default_agents` (or equivalent upsert) to seed endpoints first**

Find the function that seeds DEFAULT_AGENTS (usually `seed_default_agents(db)` or similar). Add a sibling function above it:

```python
def seed_default_endpoints(db) -> dict:
    """Upsert DEFAULT_ENDPOINTS into the endpoints table.

    Returns {"upserted": n, "skipped_collision": m}.
    System rows always overwrite. If a system=0 row already exists with
    the same id as a DEFAULT_ENDPOINTS entry, log and skip.
    """
    import json
    upserted = 0
    skipped = 0
    for ep in DEFAULT_ENDPOINTS:
        existing = db.execute(
            "SELECT system FROM endpoints WHERE id = ?", (ep.id,)
        ).fetchone()
        if existing is not None and existing[0] == 0:
            print(f"WARN seed: skipping system endpoint {ep.id} — "
                  f"user row with same id exists, please rename")
            skipped += 1
            continue
        db.execute("""
            INSERT INTO endpoints (id, name, endpoint_type, provider, model,
                base_url, api_key_env, command, args, prompt_mode,
                timeout_s, capabilities, session_config, system)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                endpoint_type=excluded.endpoint_type,
                provider=excluded.provider,
                model=excluded.model,
                base_url=excluded.base_url,
                api_key_env=excluded.api_key_env,
                command=excluded.command,
                args=excluded.args,
                prompt_mode=excluded.prompt_mode,
                timeout_s=excluded.timeout_s,
                capabilities=excluded.capabilities,
                session_config=excluded.session_config,
                system=excluded.system
            WHERE endpoints.system = 1
        """, (
            ep.id, ep.name, ep.endpoint_type, ep.provider, ep.model,
            ep.base_url, ep.api_key_env, ep.command,
            json.dumps(ep.args), ep.prompt_mode, ep.timeout_s,
            json.dumps(ep.capabilities), json.dumps(ep.session_config),
            ep.system,
        ))
        upserted += 1
    db.commit()
    print(f"INFO seed: endpoints_upserted={upserted} endpoints_skipped_collision={skipped}")
    return {"upserted": upserted, "skipped_collision": skipped}
```

- [ ] **Step 5: Call `seed_default_endpoints(db)` from the existing seed entry point BEFORE `seed_default_agents(db)`**

Find where `seed_default_agents(db)` is called (probably in `src/db.py` `init_db()` or in `src/serve.py` startup). Add `seed_default_endpoints(db)` immediately before that call. The endpoints table must exist (created in Task 6) before this runs, so the call will fail until the migration lands — that's expected and fine.

- [ ] **Step 6: Update existing `seed_default_agents` upsert to include `endpoint_id`**

Find the INSERT or UPDATE statement that upserts an Agent row. Add `endpoint_id` to the column list and bind the Agent's `endpoint_id` value. The column doesn't exist in the schema yet (added in Task 7), so the seeding will fail until the migration runs — accepted; the migration is what makes this work.

- [ ] **Step 7: Commit**

```bash
git add src/workflows_seed.py
git commit -m "feat(seed): rewire DEFAULT_AGENTS to endpoint_ids + seed_default_endpoints"
git push
```

---

## Phase B — TDD Tests (red)

Write tests before implementation. They should all fail initially.

### Task 3: Create the endpoints TDD test scaffold and write `build_invocation` tests

**Files:**
- Create: `tests/test_tdd_endpoints.py`

- [ ] **Step 1: Create the test file with the conftest-style sys.path shim and shared fixtures**

```python
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
```

- [ ] **Step 2: Add `build_invocation` happy-path tests**

Append to the file:

```python
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
```

- [ ] **Step 3: Add `build_invocation` error-path tests**

```python
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
```

- [ ] **Step 4: Run the test file — every test must fail with ImportError or AttributeError**

Run: `python3 -m pytest tests/test_tdd_endpoints.py -v`

Expected: every test fails because `src/endpoints.py` does not exist yet. The output should show `ModuleNotFoundError: No module named 'endpoints'` or similar.

- [ ] **Step 5: Commit**

```bash
git add tests/test_tdd_endpoints.py
git commit -m "test: build_invocation TDD red — endpoints module not yet created"
git push
```

---

### Task 4: Add `extract_session_id` TDD tests

**Files:**
- Modify: `tests/test_tdd_endpoints.py` (append more tests)

- [ ] **Step 1: Append extract_session_id tests**

```python
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
```

- [ ] **Step 2: Run and verify all new tests fail**

Run: `python3 -m pytest tests/test_tdd_endpoints.py -v -k extract_session_id`

Expected: all 5 tests fail with ModuleNotFoundError.

- [ ] **Step 3: Commit**

```bash
git add tests/test_tdd_endpoints.py
git commit -m "test: extract_session_id TDD red"
git push
```

---

### Task 5: Add migration #20 TDD tests

**Files:**
- Modify: `tests/test_tdd_endpoints.py` (append migration tests)

- [ ] **Step 1: Add shared migration fixtures**

```python
# === Migration #19 ===

@pytest.fixture
def db_pre_migration_20(tmp_path):
    """Build a sqlite DB with the schema as of migration 19 (so #20 hasn't
    run yet) and a small set of legacy agent rows for testing the data
    backfill."""
    import db as ttdb
    db_path = tmp_path / "test.db"

    # Use the real init logic but stop before migration 20
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    # Replay migrations 1..19 by importing init_db and patching out 20.
    # Easiest path: call init_db() then sanity-check no endpoints table.
    with patch.object(ttdb, "_apply_migration_20", lambda c: None,
                      create=True):
        ttdb.init_db(conn)
    # Insert legacy agent rows
    conn.executemany("""
        INSERT INTO workflow_agents
            (id, name, command, args, system_prompt, persist_session, system)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [
        # System agents (match canonical mappings)
        ("agent_planner", "Planner", "claude", "[]", "...", 1, 1),
        ("agent_worker", "Worker", "claude", "[]", "...", 0, 1),
        ("agent_consultant", "Consultant", "codex",
         '["exec", "-s", "read-only"]', "...", 1, 1),
        # User agent sharing command with system planner
        ("usr_my_claude", "My Claude", "claude", "[]", "user prompt", 0, 0),
        # User agent with unknown command
        ("usr_my_thing", "My Thing", "mytool", '["--flag"]', "...", 0, 0),
        # Malformed args
        ("agent_bad", "Bad", "claude", "not json", "...", 0, 0),
    ])
    conn.commit()
    return conn, str(db_path)


def test_migration_20_canonical_id_for_system_claude(db_pre_migration_20):
    """A system agent with command='claude', args=[] should be rewired
    to the canonical 'claude-cli' endpoint, not a synthesised id."""
    from db import _apply_migration_20
    conn, _ = db_pre_migration_20
    _apply_migration_20(conn)
    row = conn.execute(
        "SELECT endpoint_id FROM workflow_agents WHERE id='agent_planner'"
    ).fetchone()
    assert row[0] == "claude-cli"


def test_migration_20_user_agent_does_not_share_system_endpoint(db_pre_migration_20):
    """User agent with same (command, args) as system planner must get
    its own user-owned endpoint — grouping key includes 'system'."""
    from db import _apply_migration_20
    conn, _ = db_pre_migration_20
    _apply_migration_20(conn)
    user_eid = conn.execute(
        "SELECT endpoint_id FROM workflow_agents WHERE id='usr_my_claude'"
    ).fetchone()[0]
    system_eid = conn.execute(
        "SELECT endpoint_id FROM workflow_agents WHERE id='agent_planner'"
    ).fetchone()[0]
    assert user_eid != system_eid
    assert system_eid == "claude-cli"
    # The user agent's endpoint should be system=0
    user_ep_system = conn.execute(
        "SELECT system FROM endpoints WHERE id=?", (user_eid,)
    ).fetchone()[0]
    assert user_ep_system == 0


def test_migration_20_unknown_command_creates_synthesised_endpoint(db_pre_migration_20):
    from db import _apply_migration_20
    conn, _ = db_pre_migration_20
    _apply_migration_20(conn)
    eid = conn.execute(
        "SELECT endpoint_id FROM workflow_agents WHERE id='usr_my_thing'"
    ).fetchone()[0]
    assert eid is not None
    row = conn.execute(
        "SELECT command, args, system FROM endpoints WHERE id=?", (eid,)
    ).fetchone()
    assert row[0] == "mytool"
    assert json.loads(row[1]) == ["--flag", "{prompt}"]   # positional append
    assert row[2] == 0   # user agent → user endpoint


def test_migration_20_malformed_args_defaults_to_empty(db_pre_migration_20, caplog):
    from db import _apply_migration_20
    conn, _ = db_pre_migration_20
    _apply_migration_20(conn)
    # The bad agent should still get an endpoint (default args=[] applied)
    eid = conn.execute(
        "SELECT endpoint_id FROM workflow_agents WHERE id='agent_bad'"
    ).fetchone()[0]
    assert eid is not None
    # Should have logged a warning naming the agent id
    assert any("agent_bad" in r.message for r in caplog.records)


def test_migration_20_every_agent_gets_endpoint_id(db_pre_migration_20):
    from db import _apply_migration_20
    conn, _ = db_pre_migration_20
    _apply_migration_20(conn)
    nulls = conn.execute(
        "SELECT COUNT(*) FROM workflow_agents WHERE endpoint_id IS NULL"
    ).fetchone()[0]
    assert nulls == 0


def test_migration_20_idempotent(db_pre_migration_20):
    from db import _apply_migration_20
    conn, _ = db_pre_migration_20
    _apply_migration_20(conn)
    snapshot_a = conn.execute("SELECT id, endpoint_id FROM workflow_agents ORDER BY id").fetchall()
    eps_a = conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
    _apply_migration_20(conn)  # rerun
    snapshot_b = conn.execute("SELECT id, endpoint_id FROM workflow_agents ORDER BY id").fetchall()
    eps_b = conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
    assert snapshot_a == snapshot_b
    assert eps_a == eps_b


def test_migration_20_compat_columns_preserved(db_pre_migration_20):
    """The compat fallback requires workflow_agents.command/.args remain
    populated after migration."""
    from db import _apply_migration_20
    conn, _ = db_pre_migration_20
    _apply_migration_20(conn)
    row = conn.execute(
        "SELECT command, args FROM workflow_agents WHERE id='agent_planner'"
    ).fetchone()
    assert row[0] == "claude"
    assert row[1] == "[]"


def test_migration_20_pragma_foreign_keys_still_on(db_pre_migration_20):
    from db import _apply_migration_20
    conn, _ = db_pre_migration_20
    _apply_migration_20(conn)
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
```

- [ ] **Step 2: Run and verify all migration tests fail**

Run: `python3 -m pytest tests/test_tdd_endpoints.py -v -k migration_20`

Expected: all fail with `AttributeError: module 'db' has no attribute '_apply_migration_20'` (or similar).

- [ ] **Step 3: Commit**

```bash
git add tests/test_tdd_endpoints.py
git commit -m "test: migration #20 TDD red — covers grouping, canonical-id, malformed args, idempotency"
git push
```

---

## Phase C — Schema + Migration

### Task 6: Add `endpoints` table to `src/db.py` (migration #20, schema only)

**Files:**
- Modify: `src/db.py` (add migration function)

- [ ] **Step 1: Read the migration runner pattern**

Run: `grep -n "_migrations\|def _apply_migration\|version = " src/db.py | head -40`

Identify the pattern: each migration is gated by `SELECT 1 FROM _migrations WHERE version = N`, runs its DDL/DML, INSERTs `(N)` into `_migrations`, then `conn.commit()`.

- [ ] **Step 2: Add a `_apply_migration_20` function near the other `_apply_migration_*` functions**

```python
def _apply_migration_20(conn) -> None:
    """Migration 19: add endpoints table + workflow_agents.endpoint_id,
    backfill data from existing workflow_agents.

    All work happens in a single transaction. The _migrations row is
    inserted last, before the implicit commit, so partial state is
    impossible.
    """
    if conn.execute(
        "SELECT 1 FROM _migrations WHERE version = 20"
    ).fetchone():
        return

    # 1. Create endpoints table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS endpoints (
            id             TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            endpoint_type  TEXT NOT NULL CHECK (endpoint_type IN
                             ('cli','anthropic_api','openai_api',
                              'gemini_api','ssh_cli')),
            provider       TEXT,
            model          TEXT,
            base_url       TEXT,
            api_key_env    TEXT,
            command        TEXT,
            args           TEXT NOT NULL DEFAULT '[]',
            prompt_mode    TEXT NOT NULL DEFAULT 'template'
                             CHECK (prompt_mode IN ('template','stdin')),
            timeout_s      INTEGER NOT NULL DEFAULT 120,
            capabilities   TEXT NOT NULL DEFAULT '{}',
            session_config TEXT NOT NULL DEFAULT '{}',
            system         INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # 2. Add endpoint_id column to workflow_agents (idempotent guard)
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(workflow_agents)").fetchall()}
    if "endpoint_id" not in cols:
        conn.execute(
            "ALTER TABLE workflow_agents ADD COLUMN endpoint_id TEXT "
            "REFERENCES endpoints(id) ON DELETE SET NULL"
        )

    # 3. Data backfill (Task 8 fills this in — for now just record version)
    _backfill_endpoints_from_agents(conn)

    # 4. Record version (last step in transaction)
    conn.execute("INSERT INTO _migrations (version) VALUES (20)")
    conn.commit()


def _backfill_endpoints_from_agents(conn) -> None:
    """Backfill endpoints from workflow_agents rows. See Task 8 for
    full implementation."""
    pass   # Stub; implemented in Task 8
```

- [ ] **Step 3: Wire migration #20 into the migration runner**

Find the function that runs migrations sequentially (likely `init_db` or `_run_migrations`). Add `_apply_migration_20(conn)` to the sequence after migration 18.

- [ ] **Step 4: Run only the schema tests — confirm schema-related ones pass, data tests still fail**

Run: `python3 -m pytest tests/test_tdd_endpoints.py -v -k "pragma_foreign_keys or compat_columns"`

Expected: `pragma_foreign_keys` test should now pass (schema work doesn't disable the pragma). `compat_columns_preserved` still fails because the data backfill is a stub.

- [ ] **Step 5: Commit**

```bash
git add src/db.py
git commit -m "feat(db): migration 20 — endpoints table + workflow_agents.endpoint_id (schema)"
git push
```

---

### Task 7: Implement the migration's data backfill

**Files:**
- Modify: `src/db.py` (fill in `_backfill_endpoints_from_agents`)

- [ ] **Step 1: Implement the pinned `_build_agent_cmd` helper inside the migration**

Replace the stub `_backfill_endpoints_from_agents` in `src/db.py` with:

```python
def _backfill_endpoints_from_agents(conn) -> None:
    """Backfill endpoints from workflow_agents rows. Pin known system
    runtimes to canonical seeded ids; create synthesised endpoints for
    everything else. Idempotent — skips rows whose endpoint_id is set.

    See spec section 'Data migration' for the full contract.
    """
    import json as _json
    import logging as _logging
    log = _logging.getLogger("migration20")

    # Import canonical mappings lazily to avoid circular imports
    try:
        from workflows_seed import KNOWN_CLI_MAPPINGS
    except Exception:
        KNOWN_CLI_MAPPINGS = {}

    def _pinned_build_argv(command, args_list):
        """Migration-local copy of today's _build_agent_cmd transformation
        for known commands, with '{prompt}' as the literal prompt token.

        Returns the args-only portion (command stripped), suitable for
        storing in endpoints.args.
        """
        cmd = (command or "").lower()
        base = list(args_list or [])
        if cmd == "claude":
            return base + ["-p", "{prompt}", "--output-format", "json"]
        if cmd == "codex":
            return base + ["{prompt}"]
        return base + ["{prompt}"]

    counters = {"created": 0, "reused": 0, "remapped": 0,
                "malformed_args": 0, "collisions": 0}

    agents = conn.execute("""
        SELECT id, command, args, system, persist_session
        FROM workflow_agents
        WHERE endpoint_id IS NULL
    """).fetchall()

    # First pass: compute (command, raw_args_tuple, effective_argv, system,
    # persist_session) per agent
    plan = []   # list of (agent_id, command, raw_args, eff_argv, system, persist)
    for agent_id, command, args_text, system_flag, persist in agents:
        try:
            raw_args = _json.loads(args_text or "[]")
            if not isinstance(raw_args, list) or not all(
                    isinstance(x, str) for x in raw_args):
                raise ValueError("args is not a list of strings")
        except Exception as e:
            log.warning(
                f"migration20: agent_id={agent_id} has malformed "
                f"args={args_text!r}, defaulting to [] ({e})"
            )
            raw_args = []
            counters["malformed_args"] += 1
        eff_argv = _pinned_build_argv(command, raw_args)
        plan.append((agent_id, command, tuple(raw_args), tuple(eff_argv),
                     system_flag or 0, persist or 0))

    # Group by (command, effective_argv, system)
    groups = {}
    for entry in plan:
        agent_id, command, raw_args, eff_argv, sysflag, persist = entry
        key = (command, eff_argv, sysflag)
        groups.setdefault(key, []).append(entry)

    # Resolve each group to an endpoint id (canonical if known, else create)
    for (command, eff_argv, sysflag), members in groups.items():
        # Try canonical mapping (system rows only)
        canonical_id = None
        if sysflag == 1:
            canonical_id = KNOWN_CLI_MAPPINGS.get(
                (command, members[0][2]))  # use raw_args of first member
            # KNOWN_CLI_MAPPINGS uses RAW args (pre _build_agent_cmd),
            # so the same canonical id is returned regardless of how
            # many agents in this group.

        if canonical_id:
            endpoint_id = canonical_id
            # Ensure the row exists as a placeholder; seed pass will
            # upsert the canonical fields on next boot. For now insert
            # a minimal row so the FK is valid.
            existing = conn.execute(
                "SELECT 1 FROM endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO endpoints
                      (id, name, endpoint_type, command, args, system)
                    VALUES (?, ?, 'cli', ?, ?, 1)
                """, (endpoint_id, endpoint_id, command,
                      _json.dumps(list(eff_argv))))
                counters["created"] += 1
            else:
                counters["reused"] += 1
        else:
            # Synthesise a new endpoint
            endpoint_id = _synthesise_endpoint_id(
                conn, command, list(eff_argv), sysflag)
            sessions = 1 if any(p for *_, p in members) else 0
            session_config = {}
            if command == "claude":
                session_config = {
                    "resume_args": list(eff_argv) +
                                   ["--resume", "{session_id}"],
                    "session_id_regex":
                        r'"session_id"\s*:\s*"([0-9a-f-]+)"',
                }
            elif command == "codex":
                session_config = {
                    "resume_args": ["exec", "resume", "{session_id}"],
                    "session_id_regex":
                        r"Session(?:\s+ID)?\s*:\s*([0-9a-f-]+)",
                    "session_id_fallback_dir": "~/.codex/sessions/",
                }
            conn.execute("""
                INSERT INTO endpoints
                  (id, name, endpoint_type, command, args,
                   capabilities, session_config, system)
                VALUES (?, ?, 'cli', ?, ?, ?, ?, ?)
            """, (endpoint_id,
                  _synth_name(command, list(eff_argv)),
                  command,
                  _json.dumps(list(eff_argv)),
                  _json.dumps({"sessions": bool(sessions)}),
                  _json.dumps(session_config),
                  sysflag))
            counters["created"] += 1

        # Remap every member agent to this endpoint
        for agent_id, *_ in members:
            conn.execute(
                "UPDATE workflow_agents SET endpoint_id = ? WHERE id = ?",
                (endpoint_id, agent_id),
            )
            counters["remapped"] += 1

    log.info(
        f"migration20: created={counters['created']} "
        f"reused={counters['reused']} "
        f"agents_remapped={counters['remapped']} "
        f"malformed_args_defaulted={counters['malformed_args']} "
        f"id_collisions_resolved={counters['collisions']}"
    )


def _synthesise_endpoint_id(conn, command, eff_argv, sysflag) -> str:
    """Generate a unique slugified id from command + args. On collision,
    append -2, -3, ..."""
    import re as _re
    base = command or "endpoint"
    if eff_argv and len(eff_argv) > 0:
        # Use first non-placeholder arg if available
        non_placeholder = next(
            (a for a in eff_argv
             if a and not (a.startswith("{") and a.endswith("}"))),
            None,
        )
        if non_placeholder:
            base = f"{base}-{non_placeholder}"
    slug = _re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-").lower()
    if sysflag == 0:
        slug = f"usr-{slug}"
    candidate = slug
    n = 2
    while conn.execute(
        "SELECT 1 FROM endpoints WHERE id = ?", (candidate,)
    ).fetchone():
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


def _synth_name(command, eff_argv) -> str:
    summary = " ".join(
        a for a in eff_argv
        if not (a and a.startswith("{") and a.endswith("}"))
    )[:50]
    return f"{command} {summary}".strip()
```

- [ ] **Step 2: Run all migration #20 tests**

Run: `python3 -m pytest tests/test_tdd_endpoints.py -v -k migration_20`

Expected: all 8 migration tests pass.

- [ ] **Step 3: Verify the migration runs cleanly on the real dev DB**

Run:

```bash
python3 -c "
import sys; sys.path.insert(0, 'src')
import sqlite3, db
conn = sqlite3.connect('/tmp/test-migration-19.db')
db.init_db(conn)
print('Endpoints:', conn.execute('SELECT id, system FROM endpoints').fetchall())
print('Agents:', conn.execute('SELECT id, endpoint_id FROM workflow_agents').fetchall())
"
```

Expected: prints a list of endpoints (claude-cli, codex-exec-readonly, etc.) and agents with non-NULL endpoint_id values.

- [ ] **Step 4: Commit**

```bash
git add src/db.py
git commit -m "feat(db): migration 20 data backfill — canonical id mapping + grouped synthesis"
git push
```

---

## Phase D — endpoints.py Module

### Task 8: Create `src/endpoints.py` with the Endpoint dataclass, exceptions, and `load/save` helpers

**Files:**
- Create: `src/endpoints.py`

- [ ] **Step 1: Create the module with the Endpoint dataclass and exceptions**

```python
"""Model endpoint abstraction.

Owns the runtime configuration that the agents layer used to bundle
inline: command, args, prompt-template, session-resume config, and
capability advertisement.

Phase 1: only endpoint_type='cli' executes. Other types validate and
persist but raise UnsupportedEndpointType at invocation time.
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

VALID_TYPES = {"cli", "anthropic_api", "openai_api", "gemini_api", "ssh_cli"}
VALID_PROMPT_MODES = {"template", "stdin"}


class UnsupportedEndpointType(Exception):
    """Raised when build_invocation is called on a non-CLI endpoint."""


class EndpointMisconfigured(Exception):
    """Raised when an endpoint's args or other config is invalid for execution."""


@dataclass
class Endpoint:
    id: str
    name: str
    endpoint_type: str = "cli"
    command: Optional[str] = None
    args: list = field(default_factory=list)
    prompt_mode: str = "template"
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    timeout_s: int = 120
    capabilities: dict = field(default_factory=dict)
    session_config: dict = field(default_factory=dict)
    system: int = 0
    created_at: Optional[str] = None
```

- [ ] **Step 2: Add CRUD helpers**

```python
def from_row(row: sqlite3.Row) -> Endpoint:
    """Inflate an Endpoint from a sqlite Row. JSON columns are parsed."""
    d = dict(row)
    d["args"] = json.loads(d.get("args") or "[]")
    d["capabilities"] = json.loads(d.get("capabilities") or "{}")
    d["session_config"] = json.loads(d.get("session_config") or "{}")
    return Endpoint(**d)


def list_endpoints(conn) -> list[Endpoint]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM endpoints ORDER BY system DESC, id ASC"
    ).fetchall()
    return [from_row(r) for r in rows]


def get_endpoint(conn, endpoint_id: str) -> Optional[Endpoint]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)
    ).fetchone()
    return from_row(row) if row else None


def create_endpoint(conn, ep: Endpoint) -> Endpoint:
    _validate_for_persist(ep)
    conn.execute("""
        INSERT INTO endpoints
          (id, name, endpoint_type, provider, model, base_url, api_key_env,
           command, args, prompt_mode, timeout_s, capabilities,
           session_config, system)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ep.id, ep.name, ep.endpoint_type, ep.provider, ep.model,
        ep.base_url, ep.api_key_env, ep.command,
        json.dumps(ep.args), ep.prompt_mode, ep.timeout_s,
        json.dumps(ep.capabilities), json.dumps(ep.session_config),
        ep.system,
    ))
    conn.commit()
    return get_endpoint(conn, ep.id)


def update_endpoint(conn, endpoint_id: str, **fields) -> Endpoint:
    existing = get_endpoint(conn, endpoint_id)
    if existing is None:
        raise KeyError(endpoint_id)
    if existing.system == 1:
        raise PermissionError("system_endpoint")
    # Validate the merged result
    merged = Endpoint(**{**asdict(existing), **fields})
    _validate_for_persist(merged)
    # Build dynamic UPDATE
    sets, vals = [], []
    for k, v in fields.items():
        if k in ("args", "capabilities", "session_config"):
            v = json.dumps(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(endpoint_id)
    conn.execute(
        f"UPDATE endpoints SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return get_endpoint(conn, endpoint_id)


def delete_endpoint(conn, endpoint_id: str) -> int:
    """Delete a user endpoint. Returns the count of agents that were
    unlinked by the FK SET NULL cascade. Raises PermissionError on
    system rows."""
    existing = get_endpoint(conn, endpoint_id)
    if existing is None:
        raise KeyError(endpoint_id)
    if existing.system == 1:
        raise PermissionError("system_endpoint")
    affected = conn.execute(
        "SELECT COUNT(*) FROM workflow_agents WHERE endpoint_id = ?",
        (endpoint_id,)
    ).fetchone()[0]
    conn.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
    conn.commit()
    return affected


def _validate_for_persist(ep: Endpoint) -> None:
    if not re.match(r"^[a-zA-Z0-9_-]+$", ep.id or ""):
        raise EndpointMisconfigured(
            f"endpoint id must match [a-zA-Z0-9_-]+, got {ep.id!r}")
    if ep.endpoint_type not in VALID_TYPES:
        raise EndpointMisconfigured(
            f"endpoint_type must be in {VALID_TYPES}, got {ep.endpoint_type!r}")
    if ep.prompt_mode not in VALID_PROMPT_MODES:
        raise EndpointMisconfigured(
            f"prompt_mode must be in {VALID_PROMPT_MODES}, got {ep.prompt_mode!r}")
    if ep.endpoint_type == "cli" and not ep.command:
        raise EndpointMisconfigured("cli endpoints require 'command'")
    if ep.endpoint_type.endswith("_api"):
        if not ep.provider:
            raise EndpointMisconfigured(
                f"{ep.endpoint_type} endpoints require 'provider'")
        if not ep.api_key_env:
            raise EndpointMisconfigured(
                f"{ep.endpoint_type} endpoints require 'api_key_env'")
    if not isinstance(ep.args, list):
        raise EndpointMisconfigured("args must be a list")
    for i, a in enumerate(ep.args):
        if not isinstance(a, str):
            raise EndpointMisconfigured(
                f"args[{i}] must be a string, got {type(a).__name__}")
```

- [ ] **Step 3: Commit**

```bash
git add src/endpoints.py
git commit -m "feat(endpoints): module scaffold with Endpoint dataclass, exceptions, CRUD"
git push
```

---

### Task 9: Implement `build_invocation`

**Files:**
- Modify: `src/endpoints.py` (add function)

- [ ] **Step 1: Append the function**

```python
def build_invocation(
    endpoint: Endpoint, prompt: str, *, session_id: Optional[str] = None
) -> list[str]:
    """Build the argv for executing `prompt` against `endpoint`.

    Phase 1: only endpoint_type='cli' is supported.
    See spec section 'endpoints.build_invocation contract' for details.
    """
    if endpoint.endpoint_type != "cli":
        raise UnsupportedEndpointType(
            f"endpoint {endpoint.id!r} has type {endpoint.endpoint_type!r}; "
            f"API endpoint execution is not implemented (phase 1 = CLI only)"
        )
    if endpoint.prompt_mode == "stdin":
        raise NotImplementedError(
            "prompt_mode='stdin' is reserved for a future phase"
        )
    if not isinstance(endpoint.args, list):
        raise EndpointMisconfigured(
            f"endpoint {endpoint.id!r} args must be a list, got "
            f"{type(endpoint.args).__name__}"
        )
    for i, a in enumerate(endpoint.args):
        if not isinstance(a, str):
            raise EndpointMisconfigured(
                f"endpoint {endpoint.id!r} args[{i}] must be a string"
            )

    # Session-resume path: resume_args fully replaces args
    if session_id is not None:
        resume_template = (endpoint.session_config or {}).get("resume_args")
        if resume_template:
            substituted = _substitute(
                resume_template, prompt, session_id=session_id)
            return [endpoint.command] + substituted
        log.warning(
            "endpoint=%s advertises sessions but has no resume_args "
            "template — session resume skipped", endpoint.id,
        )

    # Normal path
    substituted = _substitute(endpoint.args, prompt)
    if "{prompt}" not in " ".join(endpoint.args):
        substituted.append(prompt)
    return [endpoint.command] + substituted


def _substitute(template: list[str], prompt: str,
                *, session_id: Optional[str] = None) -> list[str]:
    out = []
    for tok in template:
        new = tok.replace("{prompt}", prompt)
        if session_id is not None:
            new = new.replace("{session_id}", session_id)
        out.append(new)
    return out
```

- [ ] **Step 2: Run the build_invocation tests**

Run: `python3 -m pytest tests/test_tdd_endpoints.py -v -k build_invocation`

Expected: all 9 build_invocation tests pass.

- [ ] **Step 3: Commit**

```bash
git add src/endpoints.py
git commit -m "feat(endpoints): build_invocation with template substitution + session resume"
git push
```

---

### Task 10: Implement `extract_session_id`

**Files:**
- Modify: `src/endpoints.py` (add function)

- [ ] **Step 1: Append the function**

```python
def extract_session_id(
    endpoint: Endpoint, stdout: str, stderr: str, started_before: float
) -> Optional[str]:
    """Mine a session id from a completed invocation's output.

    Returns None if endpoint doesn't advertise session support, regex
    doesn't match, and no fallback dir finds a fresh file.

    See spec section 'endpoints.extract_session_id contract' for details.
    """
    caps = endpoint.capabilities or {}
    if not caps.get("sessions"):
        return None
    cfg = endpoint.session_config or {}

    # 1. Try regex against combined stdout + stderr
    regex = cfg.get("session_id_regex")
    if regex:
        combined = f"{stdout}\n{stderr}"
        m = re.search(regex, combined)
        if m and m.groups():
            return m.group(1)

    # 2. Fallback: newest file in session_id_fallback_dir created
    #    after started_before
    fallback_dir = cfg.get("session_id_fallback_dir")
    if fallback_dir:
        try:
            d = Path(fallback_dir).expanduser()
            if d.is_dir():
                fresh = [
                    p for p in d.iterdir()
                    if p.stat().st_mtime > started_before
                ]
                if fresh:
                    newest = max(fresh, key=lambda p: p.stat().st_mtime)
                    return newest.stem
        except OSError as e:
            log.warning("session_id_fallback_dir %s: %s", fallback_dir, e)
    return None
```

- [ ] **Step 2: Run the extract_session_id tests**

Run: `python3 -m pytest tests/test_tdd_endpoints.py -v -k extract_session_id`

Expected: all 5 tests pass.

- [ ] **Step 3: Run the entire endpoints TDD suite**

Run: `python3 -m pytest tests/test_tdd_endpoints.py -v`

Expected: every test in the file passes.

- [ ] **Step 4: Commit**

```bash
git add src/endpoints.py
git commit -m "feat(endpoints): extract_session_id (regex + fallback dir)"
git push
```

---

## Phase E — Wire Runner

### Task 11: Route runner dispatch through `endpoints.build_invocation`

**Files:**
- Modify: `src/runners.py` (define the helper)
- Modify: `src/serve.py` (the workflow thread `_run_workflow_thread` at ~lines 2039-2280 is the actual call site — update it to use the new helper)

- [ ] **Step 0: Inspect how agents are loaded by the runtime — dict, dataclass, or sqlite Row?**

Run: `grep -n "_get_workflow_agent\|workflow_agents" src/serve.py | head -20`

Read the implementation of `_get_workflow_agent` (or whatever fetches an agent row at runtime). Note: is the returned object a dict, a tuple, a sqlite.Row, or a dataclass? The new wiring needs to access `endpoint_id` on this object — the SELECT must include `endpoint_id`, and the helper below must adapt to the actual shape (attribute access for Row/dataclass, key access for dict).

- [ ] **Step 1: Find every call site of `_build_agent_cmd`, `_apply_resume_args`, `_extract_session_id` in both files**

Run: `grep -n "_build_agent_cmd\|_apply_resume_args\|_extract_session_id" src/runners.py src/serve.py`

You'll likely find the main calls in `src/serve.py` inside `_run_workflow_thread`. Note their line numbers — they're what gets rewired in Step 3.

- [ ] **Step 2: Add `_resolve_argv_for_agent` helper to `src/runners.py`**

The helper accepts agent as either a dict, a sqlite Row, or a dataclass. The two access shapes are abstracted via `_agent_field()`.

```python
from endpoints import (
    build_invocation, extract_session_id, get_endpoint, Endpoint,
    UnsupportedEndpointType, EndpointMisconfigured,
)
import json as _json
import logging as _logging

_log = _logging.getLogger("runner")
_seen_compat_agents = set()


def _agent_field(agent, name, default=None):
    """Read a field from an agent object that may be a dict, sqlite Row,
    or dataclass."""
    if isinstance(agent, dict):
        return agent.get(name, default)
    try:
        return getattr(agent, name)
    except AttributeError:
        try:
            return agent[name]
        except (KeyError, IndexError, TypeError):
            return default


def _resolve_argv_for_agent(conn, agent, prompt, session_id=None):
    """Resolve an agent + prompt into an argv via its endpoint.

    Falls back to agent's compat command/args ONLY when endpoint_id IS
    NULL. A non-NULL endpoint_id pointing at a missing endpoint, or any
    non-cli endpoint, is a hard error.
    """
    agent_id = _agent_field(agent, "id")
    endpoint_id = _agent_field(agent, "endpoint_id")

    if not endpoint_id:
        command = _agent_field(agent, "command")
        args_raw = _agent_field(agent, "args") or "[]"
        args = _json.loads(args_raw) if isinstance(args_raw, str) else list(args_raw)
        _log_compat_path_once(agent_id, command, args)
        ep = Endpoint(
            id=f"_compat:{agent_id}",
            name=f"compat for {agent_id}",
            endpoint_type="cli",
            command=command,
            args=args,
        )
        return ep, build_invocation(ep, prompt, session_id=session_id)

    ep = get_endpoint(conn, endpoint_id)
    if ep is None:
        raise EndpointMisconfigured(
            f"agent {agent_id!r} references endpoint "
            f"{endpoint_id!r} which does not exist"
        )
    return ep, build_invocation(ep, prompt, session_id=session_id)


def _log_compat_path_once(agent_id, command, args):
    if agent_id in _seen_compat_agents:
        return
    _seen_compat_agents.add(agent_id)
    _log.warning(
        f"runner: agent={agent_id} using compat command={command} "
        f"args={args} — endpoint_id is NULL (legacy or unmigrated)"
    )
```

Note the return shape: `(endpoint, argv)`. The endpoint is returned so the caller can pass it to `extract_session_id` after the subprocess completes.

- [ ] **Step 3: Update the workflow thread in `src/serve.py`**

In `_run_workflow_thread` (around lines 2039-2280), find the `_build_agent_cmd` call. Replace:

```python
# OLD
cmd = _build_agent_cmd(agent.command, agent.args, prompt_with_resume)
proc = subprocess.Popen(cmd, ...)
```

with:

```python
# NEW
from runners import _resolve_argv_for_agent
ep, cmd = _resolve_argv_for_agent(
    db, agent, prompt,
    session_id=stored_session_id if persist else None,
)
proc = subprocess.Popen(cmd, ...)
```

Also update the SELECT that loads the agent to include `endpoint_id` if it doesn't already (post-migration the column exists, but the query may not select it):

```sql
SELECT id, name, command, args, system_prompt, persist_session, system, endpoint_id
FROM workflow_agents WHERE id = ?
```

And replace the session-id extraction call:

```python
# OLD
sid = _extract_session_id(agent.command, stdout, stderr, started_before)

# NEW
from endpoints import extract_session_id
sid = extract_session_id(ep, stdout, stderr, started_before=run_start_ts)
```

- [ ] **Step 4: Verify nothing imports the old helpers any more**

```bash
grep -rn "_build_agent_cmd\|_apply_resume_args\|_extract_session_id" src/ tests/
```

Expected: matches only inside the function definitions in `serve.py` (which Task 12 will delete) and the test files that may reference them (will need fixture updates if any). No live callers outside the old definitions.

- [ ] **Step 5: Run the existing runner-touching test suite**

Run: `python3 -m pytest tests/test_tdd_kitchen_m4_gap_ticket.py tests/test_tdd_engine_workflows.py -v`

Expected: tests pass (or fail for unrelated reasons — record any new failures and fix in this task before continuing).

- [ ] **Step 6: Commit**

```bash
git add src/runners.py src/serve.py
git commit -m "feat(runner): route agent dispatch through endpoints.build_invocation"
git push
```

---

### Task 12: Remove `_build_agent_cmd`, `_apply_resume_args`, `_extract_session_id` from `serve.py`

**Files:**
- Modify: `src/serve.py` (delete three functions + their call sites)

- [ ] **Step 1: Locate the three functions**

Run: `grep -n "def _build_agent_cmd\|def _apply_resume_args\|def _extract_session_id" src/serve.py`

Note their line ranges.

- [ ] **Step 2: Find all callers**

Run: `grep -n "_build_agent_cmd\|_apply_resume_args\|_extract_session_id" src/serve.py`

For each caller (outside the function definitions themselves):
- If the caller is the workflow runner thread, it should now go through `runners._resolve_argv_for_agent` / `endpoints.extract_session_id` (already wired in Task 11). Update the call to use the new path.
- If the caller is anywhere else (unexpected), evaluate and either redirect to the new module or document why it stays.

- [ ] **Step 3: Delete the three functions from serve.py**

Remove the function bodies. Run a syntax check:

```bash
python3 -c "import ast; ast.parse(open('src/serve.py').read())"
```

Expected: no output (parse successful).

- [ ] **Step 4: Restart serve.py against a test DB and tail the log for any AttributeError on a missing function**

```bash
# Use a throwaway DB to avoid touching prod
PORT=8799 TT_DB=/tmp/test-serve.db python3 src/serve.py &
sleep 2
curl -s http://localhost:8799/api/workflow/agents | head -c 200
kill %1
```

Expected: agents API returns JSON. No `AttributeError: _build_agent_cmd` in the output.

- [ ] **Step 5: Commit**

```bash
git add src/serve.py
git commit -m "refactor(serve): remove _build_agent_cmd/_apply_resume_args/_extract_session_id (moved to endpoints.py)"
git push
```

---

### Task 13: Compatibility checkpoint — manually run all 6 system workflows

This is a **gate**: if any system agent triggers a compat-path warning, the migration is incomplete. Block here until green.

- [ ] **Step 1: Spin up serve.py against the migrated dev DB**

```bash
cd ~/projects/ticket-takeaway
PORT=8799 python3 src/serve.py 2>/tmp/serve-checkpoint.log &
sleep 2
```

- [ ] **Step 2: Trigger each of the 6 system agents via a test workflow run**

For each agent (planner, consultant, orchestrator, worker, summarizer, validator), trigger via the API:

```bash
# Replace <project> and <agent_id>
curl -X POST http://localhost:8799/<project>/api/tickets/<test-ticket-id>/workflow/run \
  -H "Content-Type: application/json" \
  -d '{"workflow_id": "<workflow-that-uses-the-agent>"}'
```

If a real CLI execution would cost API credits / take too long, mock subprocess.Popen for the checkpoint:

```bash
PYTHONPATH=src python3 -c "
from unittest.mock import patch, MagicMock
import sys, runners
mp = MagicMock()
mp.return_value.communicate.return_value = ('{\"session_id\":\"test\"}', '')
mp.return_value.returncode = 0
with patch('subprocess.Popen', mp):
    # invoke runner for each agent
    pass
"
```

- [ ] **Step 3: Grep the server log for compat-path warnings tied to system agents**

```bash
grep "WARN runner.*using compat command" /tmp/serve-checkpoint.log | grep "agent_"
```

Expected: **NO output**. If any line matches a system agent id (`agent_planner`, `agent_consultant`, etc.), the migration didn't wire that agent — investigate and fix the migration before continuing.

- [ ] **Step 4: Kill serve.py**

```bash
kill %1
```

- [ ] **Step 5: Document the checkpoint outcome**

Add to the branch's commit log or PR description: "Compatibility checkpoint green — 0 compat-path warnings for system agents on dev DB at <timestamp>".

No commit needed for this step (informational).

---

## Phase F — HTTP API

### Task 14: Add `GET /api/endpoints` and create the smoke test scaffold

**Files:**
- Modify: `src/serve.py` (add route)
- Create: `tests/test_smoke_endpoints.py`

- [ ] **Step 1: Create the smoke test scaffold**

```python
"""API smoke tests for /api/endpoints.

Uses the same dashboard_server fixture as other smoke tests.
"""
import json
import pytest
import urllib.request
import urllib.error


@pytest.fixture
def api_url(dashboard_server):
    """dashboard_server is project-scoped; endpoints are global."""
    # Extract base URL (strip project path)
    base = dashboard_server.rsplit("/", 1)[0]
    return f"{base}/api/endpoints"


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, json.loads(r.read().decode())


def _post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _put(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, json.loads(body) if body else {}


def _delete(url):
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, json.loads(body) if body else {}


def test_get_endpoints_returns_seed(api_url):
    status, body = _get(api_url)
    assert status == 200
    assert "endpoints" in body
    ids = {e["id"] for e in body["endpoints"]}
    assert "claude-cli" in ids
    assert "codex-cli" in ids
    assert "codex-exec-readonly" in ids
    assert "hermes-cli" in ids
```

- [ ] **Step 2: Run the test — it must fail until the route exists**

Run: `python3 -m pytest tests/test_smoke_endpoints.py::test_get_endpoints_returns_seed -v`

Expected: HTTP 404 or similar.

- [ ] **Step 3: Add the GET route to serve.py**

Find the global-routes section in serve.py (before the `_LEGACY_PROJECT_ID` redirect — see CLAUDE.md note on "Global route ordering"). Add:

```python
elif self.path == "/api/endpoints" and self.command == "GET":
    from endpoints import list_endpoints
    eps = [
        {k: v for k, v in vars(ep).items()}
        for ep in list_endpoints(get_db())
    ]
    self._json(200, {"endpoints": eps})
    return
```

- [ ] **Step 4: Run the test — expect PASS**

Run: `python3 -m pytest tests/test_smoke_endpoints.py::test_get_endpoints_returns_seed -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve.py tests/test_smoke_endpoints.py
git commit -m "feat(api): GET /api/endpoints — list endpoints"
git push
```

---

### Task 15: Add `POST /api/endpoints` (create) with validation

**Files:**
- Modify: `src/serve.py` (add route)
- Modify: `tests/test_smoke_endpoints.py` (add tests)

- [ ] **Step 1: Add tests**

Append to `tests/test_smoke_endpoints.py`:

```python
def test_post_creates_user_endpoint(api_url):
    status, body = _post(api_url, {
        "id": "test-user-ep",
        "name": "Test User Endpoint",
        "endpoint_type": "cli",
        "command": "echo",
        "args": ["{prompt}"],
    })
    assert status == 201
    assert body["id"] == "test-user-ep"
    assert body["system"] == 0
    # cleanup
    _delete(f"{api_url}/test-user-ep")


def test_post_rejects_invalid_id(api_url):
    status, body = _post(api_url, {
        "id": "bad id with spaces",
        "name": "x",
        "endpoint_type": "cli",
        "command": "echo",
    })
    assert status == 400
    assert "error" in body


def test_post_rejects_duplicate_id(api_url):
    status, _ = _post(api_url, {
        "id": "dup-test",
        "name": "x",
        "endpoint_type": "cli",
        "command": "echo",
    })
    assert status == 201
    status, _ = _post(api_url, {
        "id": "dup-test",
        "name": "x",
        "endpoint_type": "cli",
        "command": "echo",
    })
    assert status == 409
    _delete(f"{api_url}/dup-test")


def test_post_rejects_api_type_without_api_key_env(api_url):
    status, body = _post(api_url, {
        "id": "api-no-key",
        "name": "x",
        "endpoint_type": "openai_api",
        "provider": "openai",
    })
    assert status == 400
    assert "api_key_env" in str(body)


def test_post_rejects_args_not_array_of_strings(api_url):
    status, body = _post(api_url, {
        "id": "bad-args",
        "name": "x",
        "endpoint_type": "cli",
        "command": "echo",
        "args": ["ok", 42, "also-ok"],
    })
    assert status == 400
    assert "[1]" in str(body) or "index 1" in str(body)
```

- [ ] **Step 2: Add the POST route to serve.py**

```python
elif self.path == "/api/endpoints" and self.command == "POST":
    from endpoints import (
        Endpoint, create_endpoint, EndpointMisconfigured,
    )
    try:
        body = self._read_json_body()
    except json.JSONDecodeError as e:
        self._json(400, {"error": f"invalid JSON body: {e}"})
        return
    try:
        ep = Endpoint(
            id=body.get("id"),
            name=body.get("name") or body.get("id"),
            endpoint_type=body.get("endpoint_type", "cli"),
            command=body.get("command"),
            args=body.get("args", []),
            prompt_mode=body.get("prompt_mode", "template"),
            provider=body.get("provider"),
            model=body.get("model"),
            base_url=body.get("base_url"),
            api_key_env=body.get("api_key_env"),
            timeout_s=int(body.get("timeout_s", 120)),
            capabilities=body.get("capabilities", {}),
            session_config=body.get("session_config", {}),
            system=0,   # API can never create system rows
        )
    except (TypeError, ValueError) as e:
        self._json(400, {"error": str(e)})
        return
    try:
        created = create_endpoint(get_db(), ep)
    except EndpointMisconfigured as e:
        self._json(400, {"error": str(e)})
        return
    except sqlite3.IntegrityError:
        self._json(409, {"error": f"endpoint {ep.id!r} already exists"})
        return
    self._json(201, vars(created))
    return
```

- [ ] **Step 3: Run all POST tests**

Run: `python3 -m pytest tests/test_smoke_endpoints.py -v -k "post or duplicate"`

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/serve.py tests/test_smoke_endpoints.py
git commit -m "feat(api): POST /api/endpoints — create with validation"
git push
```

---

### Task 16: Add `PUT /api/endpoints/{id}` and `DELETE /api/endpoints/{id}`

**Files:**
- Modify: `src/serve.py` (add routes)
- Modify: `tests/test_smoke_endpoints.py` (add tests)

- [ ] **Step 1: Add tests**

```python
def test_put_updates_user_endpoint(api_url):
    _post(api_url, {"id": "put-test", "name": "x",
                    "endpoint_type": "cli", "command": "echo"})
    status, body = _put(f"{api_url}/put-test", {"name": "renamed"})
    assert status == 200
    assert body["name"] == "renamed"
    _delete(f"{api_url}/put-test")


def test_put_system_endpoint_returns_403(api_url):
    status, body = _put(f"{api_url}/claude-cli", {"name": "evil"})
    assert status == 403
    assert body.get("error") == "system_endpoint"


def test_delete_system_endpoint_returns_403(api_url):
    status, body = _delete(f"{api_url}/claude-cli")
    assert status == 403


def test_delete_user_endpoint_returns_unlinked_count(api_url):
    _post(api_url, {"id": "del-test", "name": "x",
                    "endpoint_type": "cli", "command": "echo"})
    status, body = _delete(f"{api_url}/del-test")
    assert status == 204 or status == 200
    # 204 has empty body; 200 has agents_unlinked
    if status == 200:
        assert "agents_unlinked" in body
```

- [ ] **Step 2: Add the PUT route**

```python
elif self.path.startswith("/api/endpoints/") and self.command == "PUT":
    endpoint_id = self.path.rsplit("/", 1)[-1]
    if not re.match(r"^[a-zA-Z0-9_-]+$", endpoint_id):
        self._json(400, {"error": "invalid endpoint id"}); return
    from endpoints import update_endpoint, EndpointMisconfigured
    try:
        body = self._read_json_body()
    except json.JSONDecodeError as e:
        self._json(400, {"error": str(e)}); return
    try:
        updated = update_endpoint(get_db(), endpoint_id, **body)
    except KeyError:
        self._json(404, {"error": "endpoint not found"}); return
    except PermissionError:
        self._json(403, {"error": "system_endpoint"}); return
    except EndpointMisconfigured as e:
        self._json(400, {"error": str(e)}); return
    self._json(200, vars(updated))
    return
```

- [ ] **Step 3: Add the DELETE route**

```python
elif self.path.startswith("/api/endpoints/") and self.command == "DELETE":
    endpoint_id = self.path.rsplit("/", 1)[-1]
    if not re.match(r"^[a-zA-Z0-9_-]+$", endpoint_id):
        self._json(400, {"error": "invalid endpoint id"}); return
    from endpoints import delete_endpoint
    try:
        unlinked = delete_endpoint(get_db(), endpoint_id)
    except KeyError:
        self._json(404, {"error": "endpoint not found"}); return
    except PermissionError:
        self._json(403, {"error": "system_endpoint"}); return
    if unlinked > 0:
        self._json(200, {"agents_unlinked": unlinked})
    else:
        self.send_response(204)
        self.end_headers()
    return
```

- [ ] **Step 4: Run all smoke tests**

Run: `python3 -m pytest tests/test_smoke_endpoints.py -v`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/serve.py tests/test_smoke_endpoints.py
git commit -m "feat(api): PUT/DELETE /api/endpoints/{id} with 403 lock on system rows"
git push
```

---

## Phase G — CLI Surface

### Task 17: Add `endpoint` subcommand group to `tickets-cli.py`

**Files:**
- Modify: `src/tickets-cli.py`

- [ ] **Step 1: Locate the existing `agent` subcommand for pattern reference**

Run: `grep -n "def cmd_agent\|'agent'" src/tickets-cli.py | head -10`

- [ ] **Step 2: Add the `endpoint` subcommand handler**

In `src/tickets-cli.py`, add (alongside the existing `agent` handlers):

```python
def cmd_endpoint_list(args, conn):
    from endpoints import list_endpoints
    eps = list_endpoints(conn)
    if args.format == "json":
        import json
        print(json.dumps([vars(e) for e in eps], indent=2, default=str))
        return
    print(f"{'ID':<25} {'TYPE':<10} {'CMD':<15} {'SYS'}")
    for e in eps:
        print(f"{e.id:<25} {e.endpoint_type:<10} "
              f"{(e.command or ''):<15} {e.system}")


def cmd_endpoint_add(args, conn):
    if args.type != "cli":
        print(f"endpoint add: --type {args.type} requires API endpoint "
              f"execution support (not in phase 1). Create via the HTTP API "
              f"instead.", file=sys.stderr)
        sys.exit(2)
    import json as _json
    from endpoints import Endpoint, create_endpoint, EndpointMisconfigured
    try:
        parsed_args = _json.loads(args.args) if args.args else []
    except _json.JSONDecodeError as e:
        print(f"--args is not valid JSON: {e}", file=sys.stderr)
        sys.exit(2)
    ep = Endpoint(
        id=args.id,
        name=args.name or args.id,
        endpoint_type="cli",
        command=args.cmd,
        args=parsed_args,
        timeout_s=args.timeout_s,
    )
    try:
        created = create_endpoint(conn, ep)
    except EndpointMisconfigured as e:
        print(f"endpoint add: {e}", file=sys.stderr); sys.exit(2)
    print(f"created endpoint {created.id}")


def cmd_endpoint_update(args, conn):
    import json as _json
    from endpoints import update_endpoint, EndpointMisconfigured
    fields = {}
    if args.name is not None:
        fields["name"] = args.name
    if args.cmd is not None:
        fields["command"] = args.cmd
    if args.args is not None:
        try:
            fields["args"] = _json.loads(args.args)
        except _json.JSONDecodeError as e:
            print(f"--args invalid JSON: {e}", file=sys.stderr); sys.exit(2)
    if args.timeout_s is not None:
        fields["timeout_s"] = args.timeout_s
    try:
        updated = update_endpoint(conn, args.id, **fields)
    except KeyError:
        print(f"endpoint not found: {args.id}", file=sys.stderr); sys.exit(1)
    except PermissionError:
        print(f"endpoint {args.id} is a system row — edit "
              f"src/workflows_seed.py and restart", file=sys.stderr); sys.exit(2)
    except EndpointMisconfigured as e:
        print(f"endpoint update: {e}", file=sys.stderr); sys.exit(2)
    print(f"updated endpoint {updated.id}")


def cmd_endpoint_remove(args, conn):
    from endpoints import delete_endpoint
    try:
        n = delete_endpoint(conn, args.id)
    except KeyError:
        print(f"endpoint not found: {args.id}", file=sys.stderr); sys.exit(1)
    except PermissionError:
        print(f"endpoint {args.id} is a system row — cannot remove",
              file=sys.stderr); sys.exit(2)
    print(f"removed endpoint {args.id} (unlinked {n} agents)")
```

- [ ] **Step 3: Wire the subparser**

Find where `agent` subparsers are wired and add:

```python
ep = sub.add_parser("endpoint", help="Manage runtime endpoints")
ep_sub = ep.add_subparsers(dest="subcommand", required=True)

ep_list = ep_sub.add_parser("list")
ep_list.add_argument("--format", choices=["table", "json"], default="table")
ep_list.set_defaults(func=cmd_endpoint_list)

ep_add = ep_sub.add_parser("add")
ep_add.add_argument("id")
ep_add.add_argument("--type", default="cli")   # phase-1: only 'cli' accepted
ep_add.add_argument("--name", default=None)
ep_add.add_argument("--cmd", required=True)
ep_add.add_argument("--args", default="[]")
ep_add.add_argument("--timeout-s", type=int, default=120, dest="timeout_s")
ep_add.set_defaults(func=cmd_endpoint_add)

ep_upd = ep_sub.add_parser("update")
ep_upd.add_argument("id")
ep_upd.add_argument("--name", default=None)
ep_upd.add_argument("--cmd", default=None)
ep_upd.add_argument("--args", default=None)
ep_upd.add_argument("--timeout-s", type=int, default=None, dest="timeout_s")
ep_upd.set_defaults(func=cmd_endpoint_update)

ep_rm = ep_sub.add_parser("remove")
ep_rm.add_argument("id")
ep_rm.set_defaults(func=cmd_endpoint_remove)
```

- [ ] **Step 4: Smoke-test the CLI**

```bash
python3 src/tickets-cli.py endpoint list
python3 src/tickets-cli.py endpoint add test-from-cli --cmd echo --args '["{prompt}"]'
python3 src/tickets-cli.py endpoint list | grep test-from-cli
python3 src/tickets-cli.py endpoint update test-from-cli --name "Renamed"
python3 src/tickets-cli.py endpoint remove test-from-cli
python3 src/tickets-cli.py endpoint add bad --type anthropic_api --cmd x 2>&1 | grep "phase 1"
```

Expected: list shows seeded endpoints, add/update/remove cycle works, the `--type anthropic_api` attempt is rejected with the phase-1 message.

- [ ] **Step 5: Commit**

```bash
git add src/tickets-cli.py
git commit -m "feat(cli): endpoint list/add/update/remove subcommands (--type cli only)"
git push
```

---

### Task 18: Add `--endpoint-id` to agent commands and deprecate `--cmd`/`--args`

**Files:**
- Modify: `src/tickets-cli.py`

- [ ] **Step 1: Add `--endpoint-id` to the agent add/update parsers**

Find the existing `agent add` and `agent update` argparser definitions. Add:

```python
agent_add.add_argument("--endpoint-id", default=None, dest="endpoint_id",
                       help="ID of the endpoint this agent should use")
agent_upd.add_argument("--endpoint-id", default=None, dest="endpoint_id")
```

- [ ] **Step 2: Wire `endpoint_id` into the agent insert/update SQL**

In `cmd_agent_add` and `cmd_agent_update`, include `endpoint_id` in the column list and bind. Validate at write time:

```python
if args.endpoint_id is not None:
    from endpoints import get_endpoint
    if get_endpoint(conn, args.endpoint_id) is None:
        print(f"endpoint not found: {args.endpoint_id}",
              file=sys.stderr); sys.exit(2)
```

- [ ] **Step 3: Add deprecation warning when `--cmd` or `--args` is used**

In `cmd_agent_add` and `cmd_agent_update`, after parsing:

```python
if args.cmd is not None or args.args is not None:
    print("WARN: --cmd/--args on agent commands are deprecated. "
          "Create an endpoint via 'endpoint add' and reference it with "
          "--endpoint-id instead. Compat columns will be removed in a "
          "future release.", file=sys.stderr)
```

- [ ] **Step 4: Smoke-test**

```bash
python3 src/tickets-cli.py agent add test-agent-ep --endpoint-id claude-cli --system-prompt "you are a test"
python3 src/tickets-cli.py agent add test-agent-old --cmd claude 2>&1 | grep WARN
python3 src/tickets-cli.py agent remove test-agent-ep
python3 src/tickets-cli.py agent remove test-agent-old
```

Expected: first add succeeds silently, second add emits the deprecation warning.

- [ ] **Step 5: Commit**

```bash
git add src/tickets-cli.py
git commit -m "feat(cli): --endpoint-id on agent commands; deprecate --cmd/--args"
git push
```

---

## Phase H — UI

### Task 19: Add Endpoints tab to `/workflows` page

**Files:**
- Modify: `src/serve.py` (around line 6434-6495, the agents tab rendering)

- [ ] **Step 1: Find the existing Agents tab rendering**

Run: `grep -n "Agents tab\|workflow_agents\|ag_ro\|Workflows & Agents" src/serve.py | head -20`

Locate the function that renders the /workflows page HTML (probably named like `_render_workflows_page` or inline in the route handler).

- [ ] **Step 2: Add a new Endpoints tab to the tab strip**

In the tab strip HTML (currently has Agents + Workflows), add:

```html
<button class="tab" data-tab="endpoints">Endpoints</button>
```

- [ ] **Step 3: Render the endpoints list**

Below the agents-tab `<div>`, add an endpoints-tab `<div>`:

```python
endpoints_html = []
for ep in list_endpoints(get_db()):
    ep_ro = ' readonly' if ep.system else ''
    is_locked = ep.system == 1
    sys_banner = ('<div class="banner">System endpoint — edit '
                  '<code>src/workflows_seed.py</code> and restart to '
                  'modify.</div>') if is_locked else ''
    endpoints_html.append(f"""
    <div class="row endpoint-row" data-id="{html.escape(ep.id)}">
      {sys_banner}
      <div class="field"><label>ID</label>
        <code>{html.escape(ep.id)}</code></div>
      <div class="field"><label>Name</label>
        <input type="text" value="{html.escape(ep.name)}"{ep_ro}
               data-key="name"></div>
      <div class="field"><label>Type</label>
        <select{ep_ro} data-key="endpoint_type">
          {''.join(f'<option value="{t}"'
                   f'{" selected" if t == ep.endpoint_type else ""}>{t}'
                   f'{(" ⚠ not executable in phase 1") if t != "cli" else ""}'
                   '</option>' for t in
                   ('cli','anthropic_api','openai_api','gemini_api','ssh_cli'))}
        </select>
      </div>
      <div class="field"><label>Command</label>
        <input type="text" value="{html.escape(ep.command or '')}"{ep_ro}
               data-key="command"></div>
      <div class="field"><label>Args (JSON array of strings)</label>
        <textarea{ep_ro} data-key="args">{html.escape(json.dumps(ep.args))}</textarea></div>
      <div class="field"><label>Prompt mode</label>
        <select{ep_ro} data-key="prompt_mode">
          <option value="template"{" selected" if ep.prompt_mode == "template" else ""}>template</option>
          <option value="stdin"{" selected" if ep.prompt_mode == "stdin" else ""}>stdin (reserved)</option>
        </select>
      </div>
      <div class="field"><label>Timeout (s)</label>
        <input type="number" value="{ep.timeout_s}"{ep_ro}
               data-key="timeout_s"></div>
      <div class="actions">
        <button class="save"{' disabled' if is_locked else ''}>Save</button>
        <button class="delete danger"{' disabled' if is_locked else ''}>Delete</button>
      </div>
    </div>
    """)

# Add a "+ New endpoint" button at the bottom
endpoints_html.append("""
<div class="actions"><button id="new-endpoint">+ New endpoint</button></div>
""")
```

- [ ] **Step 4: Add JS handlers for Save / Delete / New**

In the existing JS block of the /workflows page, add handlers:

```javascript
document.querySelectorAll('.endpoint-row .save').forEach(btn => {
  btn.addEventListener('click', async () => {
    const row = btn.closest('.endpoint-row');
    const id = row.dataset.id;
    const body = {};
    row.querySelectorAll('[data-key]').forEach(el => {
      let v = el.value;
      if (el.dataset.key === 'args') v = JSON.parse(v);
      if (el.dataset.key === 'timeout_s') v = parseInt(v, 10);
      body[el.dataset.key] = v;
    });
    const r = await fetch(`/api/endpoints/${id}`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (r.ok) showAppToast(`Saved ${id}`, 'success');
    else {
      const e = await r.json();
      showAppToast(`Save failed: ${e.error || r.status}`, 'error');
    }
  });
});

document.querySelectorAll('.endpoint-row .delete').forEach(btn => {
  btn.addEventListener('click', async () => {
    const row = btn.closest('.endpoint-row');
    const id = row.dataset.id;
    if (!confirm(`Delete endpoint ${id}? Agents pointing at it will be unlinked.`)) return;
    const r = await fetch(`/api/endpoints/${id}`, { method: 'DELETE' });
    if (r.ok) {
      row.remove();
      showAppToast(`Deleted ${id}`, 'success');
    }
  });
});

// "+ New endpoint" — opens a modal (or inline form). Minimal version:
document.getElementById('new-endpoint')?.addEventListener('click', async () => {
  const id = prompt('Endpoint id (alphanumeric, dashes, underscores)');
  if (!id) return;
  const cmd = prompt('Command (e.g. claude, codex, hermes)');
  const r = await fetch('/api/endpoints', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id, name: id, endpoint_type: 'cli',
                          command: cmd, args: ['{prompt}'] }),
  });
  if (r.ok) location.reload();
  else {
    const e = await r.json();
    showAppToast(`Create failed: ${e.error}`, 'error');
  }
});
```

- [ ] **Step 5: Manual UI check**

Start serve.py, open `/workflows`, click the Endpoints tab. Confirm the seeded endpoints render, system rows have the banner and disabled inputs, "+ New endpoint" creates a user endpoint, and edit/delete work.

- [ ] **Step 6: Commit**

```bash
git add src/serve.py
git commit -m "feat(ui): Endpoints tab on /workflows page with CRUD"
git push
```

---

### Task 20: Add endpoint dropdown to agent rows with CLI-only filter

**Files:**
- Modify: `src/serve.py` (agents tab rendering)

- [ ] **Step 1: Find the existing agent row rendering**

Already located in Task 19's step 1. Find the per-agent `<div class="row">` template.

- [ ] **Step 2: Add the Endpoint dropdown field**

Inside each agent row, before the existing command/args fields, add:

```python
endpoint_options = []
for ep in list_endpoints(get_db()):
    if ep.endpoint_type != 'cli':
        continue   # filtered by default
    sel = ' selected' if ep.id == (agent.endpoint_id or '') else ''
    endpoint_options.append(
        f'<option value="{html.escape(ep.id)}"{sel}>{html.escape(ep.name)}</option>'
    )

agent_html += f"""
<div class="field"><label>Endpoint</label>
  <select{ag_ro} data-key="endpoint_id">
    <option value=""></option>
    {''.join(endpoint_options)}
  </select>
  <label class="show-all-toggle">
    <input type="checkbox" class="show-all-endpoints">
    Show non-executable types
  </label>
</div>
"""
```

- [ ] **Step 3: Add the JS for the "show all" toggle**

```javascript
document.querySelectorAll('.show-all-endpoints').forEach(cb => {
  cb.addEventListener('change', async () => {
    const select = cb.closest('.field').querySelector('select');
    const r = await fetch('/api/endpoints');
    const { endpoints } = await r.json();
    const current = select.value;
    select.innerHTML = '<option value=""></option>';
    endpoints.forEach(ep => {
      if (!cb.checked && ep.endpoint_type !== 'cli') return;
      const label = ep.endpoint_type === 'cli'
        ? ep.name
        : `${ep.name} ⚠ execution not implemented`;
      const opt = document.createElement('option');
      opt.value = ep.id;
      opt.textContent = label;
      if (ep.id === current) opt.selected = true;
      select.appendChild(opt);
    });
  });
});

// Confirm dialog when saving an agent pointed at a non-CLI endpoint
document.querySelectorAll('.agent-row .save').forEach(btn => {
  btn.addEventListener('click', async (e) => {
    const row = btn.closest('.agent-row');
    const sel = row.querySelector('select[data-key="endpoint_id"]');
    const selectedOpt = sel.options[sel.selectedIndex];
    if (selectedOpt && selectedOpt.textContent.includes('not implemented')) {
      if (!confirm('This endpoint type cannot execute in phase 1. ' +
                   'The agent will fail on next run. Continue?')) {
        e.preventDefault();
        return;
      }
    }
    // continue with existing save logic...
  }, true);   // capture phase so we run before the existing handler
});
```

- [ ] **Step 4: Manual UI check**

Start serve.py, open /workflows, click Agents tab. Verify:
- Each agent row has an Endpoint dropdown
- Default dropdown shows only CLI endpoints
- Toggling "Show non-executable types" reveals API/SSH endpoints with the ⚠ suffix
- Selecting a non-CLI endpoint and clicking Save triggers the confirm dialog

- [ ] **Step 5: Commit**

```bash
git add src/serve.py
git commit -m "feat(ui): endpoint dropdown on agent rows with CLI-only filter + show-all toggle"
git push
```

---

## Phase I — Adjacent Changes

### Task 21: Extend `compare_seed_to_db.py` with `_audit_endpoints`

**Files:**
- Modify: `src/compare_seed_to_db.py`

- [ ] **Step 1: Read the existing audit pattern**

Run: `grep -n "_audit_agents\|_audit_workflows\|def _audit" src/compare_seed_to_db.py`

Read 10 lines around `_audit_agents` to understand the format.

- [ ] **Step 2: Add `_audit_endpoints`**

```python
def _audit_endpoints(conn) -> int:
    """Compare DEFAULT_ENDPOINTS to live endpoints table. Returns issue count."""
    from workflows_seed import DEFAULT_ENDPOINTS
    issues = 0
    print("\n=== Endpoints ===")

    # Seed -> DB drift: every system endpoint should match its source
    db_eps = {
        row["id"]: row
        for row in conn.execute("""
            SELECT id, name, endpoint_type, command, args, system,
                   prompt_mode, capabilities, session_config
            FROM endpoints
        """).fetchall()
    }
    seed_ids = {ep.id for ep in DEFAULT_ENDPOINTS}

    for ep in DEFAULT_ENDPOINTS:
        row = db_eps.get(ep.id)
        if row is None:
            _drift(f"system endpoint {ep.id} missing from DB")
            issues += 1
            continue
        if row["system"] != 1:
            _drift(f"endpoint {ep.id}: expected system=1, got system={row['system']}")
            issues += 1
        if row["command"] != ep.command:
            _drift(f"endpoint {ep.id}: command drift "
                   f"(seed={ep.command!r}, db={row['command']!r})")
            issues += 1
        import json as _json
        if _json.loads(row["args"]) != ep.args:
            _drift(f"endpoint {ep.id}: args drift "
                   f"(seed={ep.args}, db={_json.loads(row['args'])})")
            issues += 1
        if not issues:
            _ok(f"endpoint {ep.id} matches seed")

    # DB -> Seed cruft: system=1 rows not in DEFAULT_ENDPOINTS
    for db_id, row in db_eps.items():
        if row["system"] == 1 and db_id not in seed_ids:
            _cruft(f"system endpoint {db_id} present in DB but not in DEFAULT_ENDPOINTS")
            issues += 1

    return issues
```

- [ ] **Step 3: Wire `_audit_endpoints` into the main dispatcher**

Find where `_audit_agents` and `_audit_workflows` are called. Add a call to `_audit_endpoints(conn)` and sum its return value into the total.

- [ ] **Step 4: Run the audit script**

```bash
python3 src/compare_seed_to_db.py
```

Expected: prints sections for Agents, Workflows, and now Endpoints. The Endpoints section should show "matches seed" for all 4 seeded endpoints if migration + seed ran cleanly.

- [ ] **Step 5: Commit**

```bash
git add src/compare_seed_to_db.py
git commit -m "feat(audit): _audit_endpoints in compare_seed_to_db.py"
git push
```

---

### Task 22: Update `tests/test_tdd_consultant_seed.py` for the new schema

**Files:**
- Modify: `tests/test_tdd_consultant_seed.py`

- [ ] **Step 1: Run the test as-is and capture failures**

Run: `python3 -m pytest tests/test_tdd_consultant_seed.py -v 2>&1 | tail -40`

Note which assertions fail because of the new `endpoint_id` column or because the agent now references an endpoint.

- [ ] **Step 2: Update assertions**

For each failing test, update its assertions to expect post-migration shape. Common patterns:

- A test asserting `workflow_agents` columns includes `endpoint_id` now.
- A test calling `seed_default_agents()` should also call `seed_default_endpoints()` first (or assert that the test fixture sets up endpoints).
- A test asserting `agent_consultant.command == 'codex'` is still valid (compat columns retained) but consider adding `assert agent.endpoint_id == 'codex-exec-readonly'`.

- [ ] **Step 3: Verify all tests pass**

Run: `python3 -m pytest tests/test_tdd_consultant_seed.py -v`

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_tdd_consultant_seed.py
git commit -m "test: update consultant_seed for post-migration endpoint_id"
git push
```

---

### Task 23: Update `tests/test_tdd_lane_a_primitives.py` for the new schema

**Files:**
- Modify: `tests/test_tdd_lane_a_primitives.py`

- [ ] **Step 1: Same triage as Task 22**

```bash
python3 -m pytest tests/test_tdd_lane_a_primitives.py -v 2>&1 | tail -40
```

- [ ] **Step 2: Update assertions to expect the new schema**

Same patterns as Task 22. Specifically, tests asserting on `workflow_agents` schema should expect the additional `endpoint_id` column; tests seeding agents should also seed endpoints.

- [ ] **Step 3: Verify all tests pass**

Run: `python3 -m pytest tests/test_tdd_lane_a_primitives.py -v`

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_tdd_lane_a_primitives.py
git commit -m "test: update lane_a_primitives for post-migration endpoint_id"
git push
```

---

### Task 24: Update `tests/test_tdd_workflows_seed.py` for the new schema

**Files:**
- Modify: `tests/test_tdd_workflows_seed.py`

- [ ] **Step 1: Same triage as Task 22**

```bash
python3 -m pytest tests/test_tdd_workflows_seed.py -v 2>&1 | tail -40
```

- [ ] **Step 2: Update assertions**

The most likely impacted assertions: counts of system agents (still 6), seed idempotency tests (now also covers DEFAULT_ENDPOINTS), and any test that introspects the seeded data.

Add at least one new test asserting `seed_default_endpoints` is idempotent — running it twice produces no extra rows.

- [ ] **Step 3: Verify all tests pass**

Run: `python3 -m pytest tests/test_tdd_workflows_seed.py -v`

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_tdd_workflows_seed.py
git commit -m "test: update workflows_seed for DEFAULT_ENDPOINTS + endpoint_id"
git push
```

---

### Task 25: Rewrite the Workflow Bounce section of `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Read the current Workflow Bounce section**

Run: `grep -n "Workflow Bounce" CLAUDE.md`

Read 60 lines starting from that match.

- [ ] **Step 2: Replace the section**

Find the section that starts with `**Workflow Bounce** (I-19):` and rewrite as follows. The key changes: replace "agents (name + CLI command + system prompt)" and the hardcoded `subprocess.run` description with the endpoint abstraction.

The new top of the section:

```markdown
**Workflow Bounce** (I-19, extended by I-?? endpoint abstraction): Multi-agent prompt routing system. Users define agents (name + system prompt + endpoint_id) and endpoints (runtime: command + args + capabilities). An agent's runtime configuration lives on its endpoint — many agents can share one endpoint, OpenRouter-style providers are modelled as one endpoint per model. Workflows (ordered steps, each with an agent_id) bounce ticket content through the agent sequence; the runner asks `endpoints.build_invocation(endpoint, prompt)` for the argv. Primary agent (step 1) mediates disagreements.

- **DB tables:** `endpoints` (migration 20), `workflow_agents` (migration 4, gained `endpoint_id` in 20), `workflows`, `workflow_runs` (migration 4)
- **API:** `/api/endpoints` (CRUD, system rows 403 on PUT/DELETE), `/api/workflow/agents` (CRUD), `/api/workflow/workflows` (CRUD), ... [keep existing list]
- **CLI:** `tickets-cli.py endpoint list/add/update/remove`, `tickets-cli.py agent list/add/update/remove` (gained `--endpoint-id`), `tickets-cli.py workflow ...`
- **UI:** /workflows page has Endpoints, Agents, and Workflows tabs. Agent dropdown for endpoint filters to CLI-only by default with a "show all" toggle.
- **Execution:** Runner calls `endpoints.build_invocation(endpoint, prompt, session_id=...)` for argv. Endpoint type='cli' runs locally; other types (anthropic_api, openai_api, gemini_api, ssh_cli) are schema-reserved but raise `UnsupportedEndpointType` at invocation (phase 1).
- **Compat fallback:** When `workflow_agents.endpoint_id IS NULL`, the runner uses the legacy `workflow_agents.command`/`args` columns. This is logged once per agent per server boot. Non-NULL `endpoint_id` pointing at missing/non-cli endpoint is a HARD ERROR with no fallback — preserves intent integrity.
```

Add to the Critical gotchas section:

```markdown
- **Endpoint identity is the source of truth for argv.** Legacy `workflow_agents.command`/`args` are compat-only and read ONLY when `endpoint_id IS NULL`. If a system agent ever falls into the compat path, the migration is incomplete — check `WARN runner: agent=<id> using compat command ...` in server logs.
```

- [ ] **Step 3: Verify the doc still parses cleanly (no broken markdown)**

Run: `python3 -c "import re; doc = open('CLAUDE.md').read(); print(f'lines: {len(doc.splitlines())}, headings: {len(re.findall(r\"^#+ \", doc, re.M))}')"`

Expected: prints line count + heading count (sanity check, not strict).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: rewrite Workflow Bounce section for endpoint abstraction"
git push
```

---

## Phase J — Deploy & Verify

### Task 26: Deploy to `~/.claude/ticket-takeaway/` and restart serve.py

This phase runs on the WSL machine (per memory: server runs on WSL, CLI/dashboard writes belong on WSL).

- [ ] **Step 1: Copy changed files to the deployed location**

```bash
cd ~/projects/ticket-takeaway
# New module
cp src/endpoints.py ~/.claude/ticket-takeaway/endpoints.py
# Modified
cp src/db.py ~/.claude/ticket-takeaway/db.py
cp src/workflows_seed.py ~/.claude/ticket-takeaway/workflows_seed.py
cp src/runners.py ~/.claude/ticket-takeaway/runners.py
cp src/serve.py ~/.claude/ticket-takeaway/serve.py
cp src/tickets-cli.py ~/.claude/ticket-takeaway/tickets-cli.py
cp src/compare_seed_to_db.py ~/.claude/ticket-takeaway/compare_seed_to_db.py
```

- [ ] **Step 2: Stop the running server (if any) and restart**

```bash
pkill -f "python3.*serve.py" || true
sleep 1
nohup python3 ~/.claude/ticket-takeaway/serve.py > ~/.claude/ticket-takeaway/serve.log 2>&1 &
sleep 3
```

- [ ] **Step 3: Verify migration ran and endpoints exist**

```bash
sqlite3 ~/.claude/ticket-takeaway/tickets.db <<EOF
SELECT version FROM _migrations WHERE version = 20;
SELECT id, system FROM endpoints;
SELECT id, endpoint_id FROM workflow_agents WHERE id LIKE 'agent_%';
EOF
```

Expected: shows version 19, the 4 seeded endpoints (all system=1), and the 6 system agents all with non-NULL `endpoint_id`.

- [ ] **Step 4: Run the audit script**

```bash
python3 ~/.claude/ticket-takeaway/compare_seed_to_db.py
```

Expected: zero issues across Agents, Workflows, Endpoints.

- [ ] **Step 5: Check server log for compat-path warnings**

```bash
grep "WARN runner.*using compat" ~/.claude/ticket-takeaway/serve.log | head -20
```

Expected: **no matches**. If any system agent (id starts with `agent_`) appears here, the migration didn't wire that agent — investigate immediately.

- [ ] **Step 6: Smoke test the dashboard**

Open https://tt.rhino-balance.ts.net in a browser. Confirm:
- Workflows & Agents page loads, Endpoints tab present
- Endpoints tab shows 4 seeded rows with banners (system, locked)
- Agents tab shows endpoint dropdown on each row
- A test workflow runs successfully against a real ticket

- [ ] **Step 7: Final commit (deployment marker, no code change)**

```bash
git commit --allow-empty -m "chore(deploy): model endpoint abstraction live on WSL"
git push
```

---

## Final verification

After all 26 tasks complete:

- [ ] **All TDD tests pass:** `python3 -m pytest tests/test_tdd_endpoints.py tests/test_tdd_consultant_seed.py tests/test_tdd_lane_a_primitives.py tests/test_tdd_workflows_seed.py -v`
- [ ] **All smoke tests pass:** `python3 -m pytest tests/test_smoke_endpoints.py -v` (requires serve.py running)
- [ ] **Full test suite passes:** `python3 -m pytest tests/ -v` (allow for unrelated environment-gated tests to skip)
- [ ] **Audit script clean:** `python3 src/compare_seed_to_db.py` reports zero issues
- [ ] **No compat-path warnings for system agents** in server logs since deploy
- [ ] **Hermes endpoint exists and is creatable as an agent target via UI**

---

## Out of scope (separate tickets after this branch)

These are explicitly NOT in this plan. Do not implement.

- Anthropic API endpoint execution
- OpenAI-compatible (OpenRouter / Codex API / vLLM / llama.cpp) endpoint execution
- Gemini API endpoint execution
- SSH-tunnelled CLI endpoint execution
- Secret storage UI (today: `api_key_env` references env var by name)
- Adding a Hermes agent (persona) to default workflows — product decision
- Dropping `workflow_agents.command`/`args` columns — one release later, after confirming no compat-path warnings

---

## Shipped (as built) — 2026-05-12

Merged via PR #11 (merge commit `70aee2b`). Status: prod-live on main.

**Migration range owned by this feature:** #20 only.

(Plan was written assuming migration #19. Main's `feat/pwa-mobile` branch landed first with its own #19 — ticket_created backfill — so this work was renumbered to #20 during merge conflict resolution. Lesson captured in `~/.claude/projects/-Users-llm-projects-ticket-takeaway/memory/feedback_migration_number_collision.md`.)

**Deltas vs plan:**

- **T20 over-built.** The plan said "add Endpoint dropdown on agent rows" but didn't say "and remove the legacy Command + Args input rows." The implementer left both in place. Caught pre-merge — fixed in commit `06134b8` (drop legacy fields from agent UI) + JS update.
- **T11 NULL-command guard.** Code review caught a HIGH-severity gap in the compat path: a synthetic Endpoint built for an agent with `endpoint_id IS NULL` AND `command IS NULL` would crash subprocess with `TypeError: expected str, got NoneType`. Fixed in `40be277` with an explicit `EndpointMisconfigured` raise and a regression test.
- **System-agent endpoint editability** added late (post-T20) in response to user feedback: the dropdown was disabled on system rows, blocking the most common runtime customisation case. Resolution: UI unlocks the endpoint dropdown specifically (persona stays locked), seed's UPDATE clause drops `endpoint_id` (preserves user choice across re-seeds), PUT `/api/workflow/agents/{id}` allows `endpoint_id` changes on system rows with 403 on other fields. Commit `8394a18`.
- **WSL deploy phase removed.** Plan included a "deploy to WSL" step (T26) based on a stale memory. Reality: production runs on this Mac via Tailscale Serve at `tt.rhino-balance.ts.net`; merging to main + restarting the local serve.py wrapper is the whole deploy. T26 collapsed to a "post-merge restart" note.
- **macOS dev server bind hack.** Pre-merge dev-server smoke required temporarily patching `serve.py:11846`'s hardcoded `127.0.0.1` binding to `0.0.0.0` so the server was reachable over the LAN/tailnet. Used a sed-patch-then-revert pattern (worktree only, never on main). Worth lifting into `serve.py` itself as a `--bind` flag in a future cleanup.

**Test surface:** 27 TDD endpoints + 10 smoke endpoints + 7 compat checkpoint + 161 in updated fixture suites = 205 total. All green pre- and post-merge.

**Follow-up ticket:** I-42 "Rethink system-row lock: lock on workflow usage, not seed provenance" — the targeted fix for system-agent endpoint editing exposes the deeper design issue that locking should track usage, not seed origin. Spec-level rethink owed.

**Deferred from this branch:**
- API endpoint execution (Anthropic / OpenAI-compatible / Gemini) — schema reserves slots, runner raises `UnsupportedEndpointType` at invocation
- SSH-tunnelled CLI execution — same
- Compat column drop (`workflow_agents.command` / `args`) — one release after confirming zero compat-path warnings in production logs
