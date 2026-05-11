# Model Endpoint Abstraction — Design

**Branch:** `feat/model-endpoints`
**Date:** 2026-05-10 (revised 2026-05-11 after 2-round Codex review)
**Status:** Revised, awaiting final approval

## Problem

Today, a ticket-takeaway "agent" bundles three things on a single row in `workflow_agents`:

1. **Persona** — `name`, `system_prompt`
2. **Runtime** — `command`, `args` (always shells out to a CLI)
3. **Identity/lock** — `id`, `system` flag, `created_at`

This conflation has two costs:

- Agents that should share a runtime (e.g. multiple personas all running through `claude -p`) duplicate `command` + `args` configuration. Changing a flag means editing every agent.
- The runtime is hardcoded to "spawn a CLI". There is no seam for API-based runtimes (Anthropic API, OpenAI/Codex API, Gemini API, OpenRouter), and no seam for remote runtimes (SSH-tunnelled CLI on another machine).

We want to add **hermes-agent** (a CLI tool) and structurally prepare for API and SSH endpoints later, without committing to API complexity (auth, rate limits, streaming, cost accounting) right now.

## Solution at a glance

Split `workflow_agents` into two layers:

```
Agent                          Endpoint
─────                          ────────
id, name                       id, name, endpoint_type
system_prompt                  command, args, prompt_mode      ← used by cli/ssh_cli
endpoint_id  ──────────────►   provider, model, base_url,
persist_session_pref           api_key_env                     ← used by *_api (inert in P1)
system, created_at             timeout_s, capabilities,
                               session_config
                               system, created_at
```

- **Agent** owns persona + system prompt + which endpoint to use
- **Endpoint** owns runtime configuration: how to reach a model
- Many-to-one: many agents can share one endpoint
- OpenRouter (and similar gateways) are modelled as **one endpoint per model**, not as a single endpoint with a model dropdown

## Phase 1 scope

This branch ships:

1. The endpoint abstraction layer (DB table, dataclass, CRUD)
2. The migration that splits every existing `workflow_agents` row into one endpoint + one updated agent row
3. Execution support for `endpoint_type='cli'` only
4. Seeded system endpoints: `claude-cli`, `codex-cli`, `codex-exec-readonly`, `hermes-cli`
5. `/api/endpoints` HTTP CRUD + `tickets-cli.py endpoint ...` subcommands
6. Endpoint dropdown on the agents editor in `/workflows`
7. Compat fallback: `workflow_agents.command` and `.args` stay in the schema, populated by the migration, read **only when `endpoint_id IS NULL`** (non-CLI linked endpoints raise; missing-but-referenced endpoints raise — see Error handling)

**Out of phase 1**: API endpoint execution (Anthropic / OpenAI / Gemini / OpenRouter), SSH execution, streaming, cost/rate-limit handling, model picker UI for API endpoints, secret storage UI. Schema permits non-`cli` endpoint rows; the runner refuses to dispatch them with a clear error.

## Conceptual model

### What an endpoint is

An **endpoint** is the answer to "how do we actually invoke a model?" Phase 1 executes exactly one endpoint type:

- `cli` — spawn a local subprocess (claude, codex, hermes, ...)

Four endpoint types have schema slots reserved but are rejected at execution time:

- `ssh_cli` — spawn a CLI over SSH
- `anthropic_api` — direct calls to Anthropic's API
- `openai_api` — direct calls to any OpenAI-compatible endpoint (covers OpenRouter, Codex API, vLLM, llama.cpp, ...)
- `gemini_api` — direct calls to Google's Gemini API

### What an agent is

An **agent** is a persona bound to an endpoint. The Planner agent and the Reviewer agent can both target the `claude-cli` endpoint; what makes them different is `system_prompt`. Phase 1 retains the existing `persist_session` flag on the agent row as a *preference* ("use sessions when the endpoint supports them"); the *capability* lives on the endpoint.

### CLI invocation: template + placeholder

A `cli` endpoint specifies its runtime as:

```python
endpoint.command = "claude"
endpoint.args = ["-p", "{prompt}", "--output-format", "json"]
endpoint.prompt_mode = "template"   # default
endpoint.session_config = {"resume_args": ["--resume", "{session_id}"]}
```

The runner substitutes `{prompt}` (and `{session_id}` when resuming a session) into `args`. If no `{prompt}` placeholder is present, the prompt is appended positionally — that handles the codex case where `args=["exec", "-s", "read-only"]` and the prompt goes at the end.

`prompt_mode='stdin'` is reserved (no day-1 endpoint uses it) for future CLIs that read from stdin.

## Schema (migration #20)

```sql
CREATE TABLE endpoints (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  endpoint_type  TEXT NOT NULL CHECK (endpoint_type IN
                   ('cli','anthropic_api','openai_api','gemini_api','ssh_cli')),
  -- Provider/model metadata (used by API types; nullable for CLI in phase 1)
  provider       TEXT,                       -- 'anthropic'|'openai'|'openrouter'|'google'
  model          TEXT,                       -- e.g. 'anthropic/claude-sonnet-4'
  base_url       TEXT,                       -- e.g. 'https://openrouter.ai/api/v1'
  api_key_env    TEXT,                       -- name of env var holding the key
  -- CLI invocation (used by cli/ssh_cli; nullable for API)
  command        TEXT,                       -- e.g. 'claude', 'codex', 'hermes'
  args           TEXT NOT NULL DEFAULT '[]', -- JSON array; supports {prompt} / {session_id}
  prompt_mode    TEXT NOT NULL DEFAULT 'template'
                   CHECK (prompt_mode IN ('template','stdin')),
  -- Cross-cutting
  timeout_s      INTEGER NOT NULL DEFAULT 120,
  capabilities   TEXT NOT NULL DEFAULT '{}', -- JSON: {"sessions": true, ...}
  session_config TEXT NOT NULL DEFAULT '{}', -- JSON: {"resume_args": [...]}
  system         INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

ALTER TABLE workflow_agents ADD COLUMN endpoint_id TEXT
  REFERENCES endpoints(id) ON DELETE SET NULL;
-- workflow_agents.command and .args remain on the row as compat fallback.
-- They are populated by the migration, read by the runner ONLY when
-- endpoint_id IS NULL. A non-NULL endpoint_id pointing at a missing or
-- non-cli endpoint is a hard error (see Error handling), not a fallback
-- trigger — that prevents silent drift between persisted intent and
-- runtime behaviour.
```

### Foreign-key behaviour

`ON DELETE SET NULL`: deleting an endpoint nulls the FK on dependent agents rather than cascade-deleting the agents. The UI surfaces an "endpoint missing" warning on affected agents; the runner refuses to dispatch a step whose agent has neither an endpoint nor a fallback `command`.

**SQLite FK enforcement prerequisite**: `PRAGMA foreign_keys=ON` is already set per-connection in `get_db()` at `src/db.py:25` — `SET NULL` will fire correctly today. Migration #19 does not need to add the pragma, but must take care not to disable it mid-migration (migration 12's pattern of temporarily toggling FKs off must not be inherited here).

### Migration transactionality

`src/db.py` already wraps each migration as a single-transaction unit: check `_migrations` for the version, do all DDL/DML, INSERT the `_migrations` version row, then `conn.commit()`. Migration #19 follows the same pattern — either the whole migration (CREATE TABLE + ALTER + data backfill + agent rewire) commits, or none of it does. There is no partial-state window. If a future agent finds themselves needing a recovery script for #20, the answer is "it shouldn't be possible — investigate why the transaction half-committed".

### Data migration (in the same migration step)

The migration has three priorities, in order:

1. **Pin known system runtimes to canonical seeded IDs** so the migration and the seed agree on a single row per known endpoint (no duplicates).
2. **Don't lock user agents behind system endpoints** — group by `(command, effective_argv, system)` (see step 1 below for `effective_argv`), not just `(command, args)`, so a user agent that happens to share `command='claude'` with a system agent gets its own user-owned endpoint.
3. **Tolerate malformed legacy data** — a bad row should log and degrade, not abort the migration.

For each existing row in `workflow_agents`:

1. **Compute effective argv** — using a migration-local helper that reproduces today's `_build_agent_cmd(command, args, '{prompt}')` logic with `'{prompt}'` as the literal prompt placeholder. The migration must not call live application code (which is being refactored in step 6 of Rollout) — it carries its own pinned copy of the transformation, so replaying the migration on any future codebase produces identical results. For `command='claude', args=[]` the pinned helper yields `["-p", "{prompt}", "--output-format", "json"]` (command stripped, args only); for `command='codex', args=[]` it yields `["{prompt}"]`. This is the canonical args the endpoint must store — not the legacy `args` field, which omitted the runner-injected flags. If `args` is NULL / empty string / not valid JSON / not an array of strings, log `WARN migration20: agent_id=<id> has malformed args=<repr>, defaulting to []` and treat as `[]` before applying the helper.

2. **Group rows by `(command, effective_argv, system)`** — a 3-tuple, not a 2-tuple. Mixed system/user groups never occur because `system` is in the key.

3. **For each group, decide its endpoint identity**:

   - If `(command, effective_argv, system=1)` matches a row in `DEFAULT_ENDPOINTS` (defined in `workflows_seed.py`) — by command-and-args equality — reuse that endpoint's canonical id (`claude-cli`, `codex-cli`, `codex-exec-readonly`). Do not create a new endpoint row; the seed pass that runs after migration upserts the canonical row at this id.
   - Otherwise, create a new endpoint row with:
     - `endpoint_type='cli'`
     - `command`, `args` = the effective_argv (minus the leading command element)
     - `prompt_mode='template'`
     - `system` = the group's system flag
     - `name` synthesised from command + a one-line args summary
     - `id` slugified from name; on collision with an existing row, append `-2`, `-3`, ...
     - `capabilities.sessions=true` if any source agent had `persist_session=1`
     - `session_config` populated for known commands (`claude` → resume_args + session_id_regex; `codex` → resume_args + session_id_regex), empty `{}` for unknown commands

4. **Rewire agents** — set every source agent's `endpoint_id` to its group's endpoint id (canonical or newly created).

5. **Leave `workflow_agents.command` and `.args` populated** for the compat fallback (used only when `endpoint_id IS NULL`, which after migration is "never" unless future user activity nulls it).

6. **Log a summary** — counts of: endpoints created, endpoints reused (canonical), agents remapped, malformed-args defaults applied, id collisions resolved. Single log line, structured for grep.

**Idempotency**: rerunning is a no-op. The migration checks `_migrations` for version 20 first (existing pattern). Even if forcibly rerun, the per-row logic checks `endpoint_id IS NOT NULL` and skips.

**ID collision with existing user rows**: if the seed's canonical id (`hermes-cli`, `claude-cli`, etc.) collides with a user-created endpoint of the same id, the seed pass refuses to overwrite, logs `WARN seed: skipping system endpoint <id> — user row with same id exists, please rename`, and the system agent reverts to its compat `command`/`args` (which still works). This is the same posture as the existing `compare_seed_to_db.py` audit catches.

## Code layout

```
src/endpoints.py       NEW. Endpoint dataclass, CRUD, build_invocation,
                       extract_session_id. Single chokepoint that refuses
                       non-cli endpoint_type with a clear error.

src/runners.py         UPDATE. AgentRunner asks endpoints.build_invocation()
                       for the argv. No more dispatch on command name.

src/serve.py           UPDATE. Remove _build_agent_cmd, _apply_resume_args,
                       _extract_session_id (move logic into endpoints.py).
                       Add /api/endpoints CRUD routes (GET list, POST,
                       PUT/{id}, DELETE/{id}). Update /workflows page to
                       render endpoint dropdown on agent editor + new
                       Endpoints tab.

src/tickets-cli.py     UPDATE. Add `endpoint list/add/update/remove`
                       subcommands mirroring the existing `agent` ones.

src/workflows_seed.py  UPDATE. Add DEFAULT_ENDPOINTS list. Update each
                       DEFAULT_AGENTS row to reference an endpoint by id
                       instead of carrying command/args inline.

src/db.py              UPDATE. Migration #19 (CREATE TABLE + ALTER + data).
                       Bump _migrations version.
```

### `endpoints.build_invocation` contract

```python
def build_invocation(endpoint, prompt, *, session_id=None) -> list[str]:
    """
    Build the argv for executing `prompt` against `endpoint`.

    Returns argv ready for subprocess.Popen.

    Raises UnsupportedEndpointType if endpoint.endpoint_type is not 'cli'.
    Raises EndpointMisconfigured if command is missing or args is invalid JSON.
    """
```

Phase 1 implementation:

- `endpoint_type != 'cli'` → `raise UnsupportedEndpointType(...)` with message naming the type and pointing at "phase 1 = CLI only"
- `prompt_mode == 'stdin'` → `raise NotImplementedError("stdin prompt_mode is reserved for a future phase")`
- **Normal invocation** (no `session_id`): parse `endpoint.args` as JSON, validate as array of strings, substitute `{prompt}` into each element; if no element contains `{prompt}`, append `prompt` positionally at the end. Return `[endpoint.command] + substituted_args`.
- **Session resume** (`session_id` is provided AND `endpoint.session_config.resume_args` is set): `resume_args` **fully replaces** `endpoint.args` for this invocation. Substitute both `{prompt}` and `{session_id}` into `resume_args`. Return `[endpoint.command] + substituted_resume_args`. This single replacement contract fits both claude (`["-p","{prompt}","--output-format","json","--resume","{session_id}"]`) and codex (`["exec","resume","{session_id}"]` — note codex's resume form doesn't even use the prompt placeholder, which is fine: missing placeholder + present `prompt` = positional append).
- **Session resume without `resume_args`**: ignore the `session_id`, fall through to normal invocation, log a runtime warning that the endpoint advertised session support but provided no resume template.
- **Invalid args**: parse failure or non-string elements → `raise EndpointMisconfigured` with the parse error.

### `endpoints.extract_session_id` contract

```python
def extract_session_id(endpoint, stdout, stderr, started_before) -> str | None:
    """
    Mine a session id from a completed invocation's output, if the endpoint
    advertises capabilities.sessions=True.

    Returns the session id or None.
    """
```

Phase 1 implementation: **fully config-driven**. Endpoints that advertise `capabilities.sessions=True` must also set `session_config.session_id_regex` (string regex applied to combined stdout+stderr; the first capture group is the session id) and optionally `session_config.session_id_fallback_dir` (filesystem path to scan by mtime if regex misses, e.g. `~/.codex/sessions/`). The seeded `claude-cli` and `codex-cli` endpoints carry these — no command-name dispatch lives in code. The existing `serve.py:1961-2013` behaviour is migrated to data: the regex and fallback-dir are translated into `session_config` entries on the seeded endpoints. Hermes has `capabilities.sessions=False`, so this function is never invoked for hermes.

**Rationale for going fully config-driven**: when phase 2 adds API endpoints, their session model will live in `session_config` too (e.g. Anthropic's `conversation_id`, OpenAI's `response_id`). Keeping any command-name dispatch in code creates a "magic for built-ins, config for everything else" inconsistency that costs a maintainer 10 minutes the first time they try to extend it.

## API surface

All routes are global (no project scope), matching the existing `/api/workflow/agents` shape:

| Method | Route | Body | Returns |
|---|---|---|---|
| GET | `/api/endpoints` | — | `{"endpoints": [...]}` |
| POST | `/api/endpoints` | `{id, name, endpoint_type, command?, args?, prompt_mode?, timeout_s?, capabilities?, session_config?, provider?, model?, base_url?, api_key_env?}` | 201 with created endpoint, 400 on validation failure, 409 on duplicate id |
| PUT | `/api/endpoints/{id}` | partial update | updated endpoint or 403 `system_endpoint` |
| DELETE | `/api/endpoints/{id}` | — | 204 on success, 403 `system_endpoint` on locked rows. Deletes always succeed for user rows even if agents reference the endpoint — the FK `SET NULL` nulls those agents' `endpoint_id`. The response body includes `{"agents_unlinked": <count>}` so the caller knows. No 409. |

### Validation

- `id` matches `^[a-zA-Z0-9_-]+$`
- `endpoint_type` must be in the CHECK list
- For `endpoint_type='cli'`: `command` is required
- For `endpoint_type` ending in `_api`: `provider` and `api_key_env` are required (validated at write time even though execution is unsupported, so we never persist obviously-broken API rows)
- `args` must be a **JSON array whose elements are all strings**. Parse failure or non-string element → 400 with the offending element index in the error body.
- `prompt_mode` defaults to `template`
- System rows (`system=1`) reject PUT/DELETE with 403 `system_endpoint` (mirrors today's `system_agent` 403 contract)

**Why HTTP allows all type slots but CLI doesn't**: the HTTP API is the future admin surface and will be the entry point for seed scripts / external automation that creates non-CLI endpoint rows in phase 2+. The CLI is the phase-1 user-facing surface and is intentionally locked down to `--type cli` to keep the day-1 product story tight. This parity gap is deliberate; if you (future maintainer) are tempted to "fix" it by also accepting `--type anthropic_api` on the CLI, first add a follow-up ticket — it's a phase-2 decision, not a consistency cleanup.

## CLI surface

```
tickets-cli.py endpoint list
tickets-cli.py endpoint add <id> --type cli --cmd claude --args '["-p","{prompt}"]' [--name ...] [--timeout-s 120]
tickets-cli.py endpoint update <id> [--cmd ...] [--args ...] [--timeout-s ...] [--name ...]
tickets-cli.py endpoint remove <id>
```

**Phase-1 constraint**: `endpoint add` accepts **only** `--type cli`. Passing any other type returns an error: `endpoint add: --type X requires API endpoint execution support (not in phase 1). Create via the HTTP API instead.` API/SSH endpoint rows can be created via `POST /api/endpoints` (where they validate but cannot execute).

Existing `tickets-cli.py agent ...` subcommands gain an `--endpoint-id` flag on `add` and `update`. The `--cmd` and `--args` flags on agent commands are kept (they write to the compat columns) but emit a deprecation warning suggesting `--endpoint-id`.

## UI surface

In `/workflows` (the Workflows & Agents page):

- **Existing Agents tab**: each agent row gains an "Endpoint" dropdown populated from `/api/endpoints`. **By default the dropdown filters to `endpoint_type='cli'` only** — non-CLI endpoints are hidden because picking one would guarantee a runtime failure. A small `Show all (incl. non-executable)` checkbox below the dropdown reveals all endpoints with a `⚠ execution not implemented` suffix on non-CLI options. This prevents accidental foot-guns while still letting power users pin an agent to a future endpoint deliberately. Saving an agent with a non-CLI endpoint shows a confirm dialog: "This endpoint type cannot execute in phase 1. The agent will fail on next run. Continue?" Saving an agent writes `endpoint_id`. System agents render the dropdown read-only as today.
- **New Endpoints tab**: same row-edit pattern as agents. Fields: id, name, endpoint_type (dropdown of all five types), command, args (JSON textarea), prompt_mode (dropdown of `template`/`stdin`), timeout_s.
- **Non-CLI types in the endpoint editor's type dropdown** are selectable but show a yellow "execution not yet implemented" footer beneath the form. This makes the seam visible without misleading users about phase-1 capability.
- **System endpoint rows** render with the same `system=1` lock pattern as system agents: inputs disabled, banner pointing at `workflows_seed.py`.

## Seeded system endpoints

```python
DEFAULT_ENDPOINTS = [
    Endpoint(
        id="claude-cli",
        name="Claude CLI",
        endpoint_type="cli",
        system=1,
        command="claude",
        args=["-p", "{prompt}", "--output-format", "json"],
        prompt_mode="template",
        capabilities={"sessions": True},
        session_config={"resume_args": ["--resume", "{session_id}"]},
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
        session_config={"resume_args": ["exec", "resume", "{session_id}"]},
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

Existing system agents are updated to reference these:

| Agent | Old `command` / `args` | New `endpoint_id` |
|---|---|---|
| agent_planner | claude / [] | claude-cli |
| agent_consultant | codex / ["exec", "-s", "read-only"] | codex-exec-readonly |
| agent_orchestrator | claude / [] | claude-cli |
| agent_worker | claude / [] | claude-cli |
| agent_summarizer | claude / [] | claude-cli |
| agent_validator | claude / [] | claude-cli |

`hermes-cli` ships seeded but is **not wired into any default agent or workflow**. Users opt in by editing an existing agent's endpoint or creating a new agent that targets it.

### Seed reconciliation policy

Seeded system endpoints (`DEFAULT_ENDPOINTS`) follow the same upsert-by-id pattern as `DEFAULT_AGENTS`: on every server boot, the seed pass overwrites the system row's fields (name, command, args, capabilities, session_config) with the canonical values from source. Local manual edits to system endpoint rows are **lost on next boot** — the row's source of truth is `workflows_seed.py`. This matches today's `system=1` agent and workflow behaviour.

**Collision with user-owned rows**: if a `system=0` row already exists with the same id as a system endpoint we're about to upsert, the seed refuses to overwrite, logs `WARN seed: skipping system endpoint <id> — user row with same id exists, please rename`, and leaves the user row intact. System agents that expected the canonical id fall back to their compat `command`/`args` until the user resolves the collision. The existing `src/compare_seed_to_db.py` audit must be extended to surface these warnings (see Adjacent changes).

## Error handling

| Situation | Behaviour |
|---|---|
| Agent has `endpoint_id` pointing at a missing endpoint row | **Hard error**, no fallback. Step fails with conversation message: "agent X references endpoint Y which does not exist". Server log: `ERROR runner: agent=<id> endpoint_id=<id> missing — possible data integrity issue, did a delete bypass the FK?` |
| Agent has `endpoint_id=NULL` and compat `command IS NOT NULL` | Runner uses compat path. Conversation: no message (silent to the user). Server log: `WARN runner: agent=<id> using compat command=<cmd> args=<args> — endpoint_id is NULL (legacy or unmigrated)`. Logged once per agent per server boot to avoid log spam. |
| Agent has `endpoint_id=NULL` and `command IS NULL` | Step fails with conversation message: "agent X has no endpoint and no command — refusing to dispatch". Server log: `ERROR runner: agent=<id> has no endpoint and no compat command — refusing to dispatch`. |
| Endpoint has `endpoint_type != 'cli'` | `build_invocation` raises `UnsupportedEndpointType`. Runner catches and writes conversation message naming the type. Server log: `WARN runner: agent=<id> endpoint=<id> type=<type> not executable in phase 1`. **No compat fallback** — the user explicitly chose this endpoint, so failing loudly is correct. |
| Endpoint has invalid `args` JSON / non-string elements | `build_invocation` raises `EndpointMisconfigured`. Runner writes the parse error to the conversation. Server log: `ERROR runner: endpoint=<id> args misconfigured: <error>`. |
| Endpoint deleted while in use (user-owned endpoint) | FK `SET NULL` on agents that referenced it. UI shows "endpoint missing" warning on those agents. Next dispatch falls under "endpoint_id IS NULL" rules above. |
| `prompt_mode='stdin'` | Phase 1: `NotImplementedError`. Server log: `WARN: endpoint=<id> prompt_mode='stdin' is reserved for future phase`. |
| Endpoint `capabilities.sessions=True` but `session_config.resume_args` missing | Session not resumed; normal invocation runs. Server log: `WARN runner: endpoint=<id> advertises sessions but has no resume_args template — session resume skipped`. |

## Testing

### TDD (`tests/test_tdd_endpoints.py`)

`build_invocation`:
- `{prompt}` placeholder substitutes correctly (single + multiple occurrences)
- No `{prompt}` placeholder → `prompt` appended positionally at end
- `session_id` provided + `resume_args` set → `resume_args` fully replaces `args`; both `{prompt}` and `{session_id}` substituted
- `session_id` provided + `resume_args` missing → normal invocation runs, warning logged
- `endpoint_type` in {`anthropic_api`, `openai_api`, `gemini_api`, `ssh_cli`} → `UnsupportedEndpointType`
- `args` not a JSON array → `EndpointMisconfigured`
- `args` array contains non-string element → `EndpointMisconfigured` with element index in message
- `prompt_mode='stdin'` → `NotImplementedError`

`extract_session_id`:
- Endpoint with `session_id_regex` matches stdout → returns first capture group
- Endpoint with `session_id_regex` matches stderr → returns first capture group
- Endpoint with `session_id_regex` no match, `session_id_fallback_dir` set, fresh file present → returns inferred id
- Endpoint with `capabilities.sessions=False` → never called (asserted by caller test)

Migration #19 applied to a DB with sample legacy agents:
- Known system runtimes (claude/[], codex/[exec,-s,read-only]) map to canonical seeded ids (`claude-cli`, `codex-exec-readonly`)
- User agent with `command='claude', args=[]` does NOT share endpoint with system planner — gets its own user endpoint (grouping key includes `system`)
- Every agent has `endpoint_id` set post-migration
- Malformed `args` JSON in source row → defaulted to `[]`, agent still gets endpoint, warning logged
- Migration is idempotent (rerunning is a no-op)
- Migration runs in a single transaction (assertion: simulate failure mid-data-step, assert _migrations version row absent and endpoints table absent post-rollback)

### Smoke (`tests/test_smoke_endpoints.py`)

- `GET /api/endpoints` after fresh seed returns 4 system endpoints
- `POST /api/endpoints` creates a user endpoint
- `PUT /api/endpoints/{system-id}` returns 403 `system_endpoint`
- `DELETE /api/endpoints/{system-id}` returns 403 `system_endpoint`
- `POST /api/endpoints` with `endpoint_type='openai_api'` and missing `api_key_env` returns 400
- `POST /api/endpoints` with invalid JSON in `args` returns 400
- Agent edit form in `/workflows` includes endpoint dropdown populated from API

### E2E (deferred)

Endpoint editing is a configuration surface, not a user journey. No E2E tests in phase 1.

### Mocked-subprocess invocation check (CI-safe)

TDD-layer test using `unittest.mock.patch('subprocess.Popen')` that asserts the runner, given a migrated agent + endpoint pair, produces the expected argv. Covers all 6 system agents post-migration. Runs in CI on any machine — no binary dependencies.

### Real-CLI smoke check (manual / gated)

Optional `@pytest.mark.real_cli` test that runs an existing system workflow end-to-end against a real CLI on the dev machine. Skipped automatically when the relevant binary (`claude`, `codex`, etc.) is not on `PATH` via `pytest.importorskip`-style skip-if guards. Validates the migration didn't break a real path on the developer's machine but doesn't gate CI.

## Operational observability

Three log channels, distinct from the user-facing conversation log:

**Migration-time** (one summary log line, structured for grep): `INFO migration20: created=<n> reused=<n> agents_remapped=<n> malformed_args_defaulted=<n> id_collisions_resolved=<n>`. Plus per-row `WARN migration20: ...` lines for each malformed-args default and each id collision.

**Seed-time** (per-server-boot): `INFO seed: endpoints_upserted=<n> endpoints_skipped_collision=<n>` plus per-collision `WARN seed: skipping system endpoint <id> — user row with same id exists, please rename`.

**Runtime** (per-dispatch when an anomaly is detected): see the Error-handling table. Compat-path warnings are throttled to once per agent per server boot to prevent log spam; an in-process set tracks `seen_compat_agent_ids`.

These give a maintainer the visibility to answer "is this board still on the compat path for any agent?" without reading the DB by hand. The audit script (`compare_seed_to_db.py`, extended for endpoints) is the standalone equivalent for offline checks.

## Rollout

1. Branch `feat/model-endpoints` (already created)
2. Write `workflows_seed.py` updates first — `DEFAULT_ENDPOINTS` list, agents rewired to canonical endpoint ids — so migration tests can import the canonical mapping.
3. Build TDD tests (red): `test_tdd_endpoints.py` covers `build_invocation`, `extract_session_id`, and migration #20 against in-memory DBs.
4. Implement migration #20 in `src/db.py` (green for migration tests). Verify the single-transaction guarantee holds; verify `PRAGMA foreign_keys=ON` is still active mid-migration.
5. Implement `src/endpoints.py` (green for `build_invocation` and `extract_session_id` tests).
6. Wire runner through `endpoints.build_invocation`. Remove `_build_agent_cmd`, `_apply_resume_args`, `_extract_session_id` from `serve.py`.
7. **Compatibility checkpoint** — run all 6 system workflows end-to-end on the dev machine (mocked-subprocess + real-CLI where binaries present). Assert zero `WARN runner: ... using compat command ...` lines emitted for any system agent. If any compat-path warning fires for a system agent, the migration is incomplete — block here, fix it, rerun. Do not proceed to UI work until the checkpoint is green.
8. Implement HTTP API (`/api/endpoints` CRUD) and CLI (`tickets-cli.py endpoint ...`); green smoke tests.
9. Implement UI changes (Endpoints tab, agent dropdown filter).
10. Extend `src/compare_seed_to_db.py` with `_audit_endpoints()` — same pattern as `_audit_agents()` / `_audit_workflows()`.
11. Update `CLAUDE.md` (rewrite the Workflow Bounce section to reflect the agent/endpoint split — see Adjacent changes).
12. Deploy to `~/.claude/ticket-takeaway/` (per CLAUDE.md deployment notes), restart `serve.py`, run the audit script, smoke test on real WSL board.

## Adjacent changes (in this branch, beyond core implementation)

These three changes ship with the feature — not follow-ups, but easy to miss:

- **`CLAUDE.md` Workflow Bounce section** — rewrite to describe the agent/endpoint split. The existing section (lines ~58-90) currently says "agents (name + CLI command + system prompt)" and describes the runner as "`subprocess.run(["claude", "-p", ...])` per step". Both are now wrong. Update to: "agents (name + system prompt + endpoint), endpoints (command + args + capabilities)", and replace the subprocess line with "runner asks `endpoints.build_invocation(endpoint, prompt)` for the argv". Add a one-line note in Critical gotchas: "Endpoint identity is the source of truth for argv; legacy `workflow_agents.command`/`args` are compat-only and read only when `endpoint_id IS NULL`."
- **`src/compare_seed_to_db.py` extension** — add `_audit_endpoints(conn) -> int` following the existing `_audit_agents()` / `_audit_workflows()` pattern. Compares `DEFAULT_ENDPOINTS` to the live `endpoints` table both directions; reports drift (system endpoint edited locally) and cruft (system endpoint deleted, user row with system id). Called from the main audit dispatcher. Same return-int-of-issues contract as the others.
- **Existing test fixtures** — `tests/test_tdd_consultant_seed.py`, `tests/test_tdd_lane_a_primitives.py`, and `tests/test_tdd_workflows_seed.py` reference the `workflow_agents` schema (especially `persist_session` and `system` columns) and the `seed_default_agents()` function. The agent rewire + endpoint addition will change what those tests observe — they may need fixture updates to set `endpoint_id` on inserted agent rows or to assert the post-migration schema. Audit and patch as part of the implementation, not as a follow-up.

## Follow-ups (separate tickets, out of this branch)

- Drop `workflow_agents.command` and `.args` columns once we're confident the compat path is dead (one release later)
- Implement `endpoint_type='anthropic_api'` execution
- Implement `endpoint_type='openai_api'` execution (gives us OpenRouter + Codex API + any compatible)
- Implement `endpoint_type='gemini_api'` execution
- Implement `endpoint_type='ssh_cli'` execution
- Add a Hermes agent (persona) targeting `hermes-cli` (product decision: which workflows, if any, default to using it)
- Secret storage UI (today: `api_key_env` references an env var, no in-DB secrets)

## Open questions

None. All foundational decisions have been made and survived a 2-round Codex review:

- Conceptual split: agent (persona) vs endpoint (runtime) ✓
- One agent → one endpoint, no fallback chain ✓
- OpenRouter modelled as one endpoint per model ✓
- Phase 1 ships CLI execution only; API/SSH structurally permitted but rejected at runtime ✓
- `prompt_mode` enum is `template` + `stdin` only ✓
- Compat columns on `workflow_agents` retained for one release ✓
- Hermes seeded as endpoint only; no default agent or workflow change ✓
- Migration grouping key is `(command, args, system)` — user agents don't get locked behind system endpoints ✓
- `resume_args` fully replaces `args` when `session_id` is present (single contract) ✓
- `extract_session_id` is fully config-driven (no command-name dispatch in code) ✓
- `args` validates as a JSON array of strings (not just any valid JSON) ✓
- Agent endpoint dropdown filters to CLI by default with "show all" toggle ✓
- Migration runs in a single transaction; FK pragma already enforced per-connection ✓
