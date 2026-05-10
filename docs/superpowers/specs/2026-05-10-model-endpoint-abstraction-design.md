# Model Endpoint Abstraction — Design

**Branch:** `feat/model-endpoints`
**Date:** 2026-05-10
**Status:** Draft, awaiting review

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
7. Compat fallback: `workflow_agents.command` and `.args` stay in the schema, populated by the migration, read only when `endpoint_id IS NULL` or the linked endpoint is missing

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

## Schema (migration #19)

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
-- They are populated by the migration, read by the runner only when
-- endpoint_id IS NULL or the linked endpoint row is missing/non-cli.
```

### Foreign-key behaviour

`ON DELETE SET NULL`: deleting an endpoint nulls the FK on dependent agents rather than cascade-deleting the agents. The UI surfaces an "endpoint missing" warning on affected agents; the runner refuses to dispatch a step whose agent has neither an endpoint nor a fallback `command`.

### Data migration (in the same migration step)

For each existing row in `workflow_agents`:

1. Compute a key `(command, json(args))`
2. Group rows by that key
3. For each group, create one endpoint row:
   - `endpoint_type='cli'`
   - `command`, `args` copied verbatim
   - `prompt_mode='template'`
   - `system` derived from the group's source agent rows: `1` if any source agent had `system=1`, else `0`
   - `name` synthesised: `"<command> <args-summary>"` (e.g. `"claude -p"`, `"codex exec read-only"`)
   - `id` slugified from name with collision-avoidance suffix
   - `capabilities.sessions=true` if any source agent had `persist_session=1`
   - `session_config.resume_args` copied from the existing per-CLI session-resume logic in `serve.py:1924-2013`:
     - claude → `["--resume", "{session_id}"]`
     - codex → `["exec", "resume", "{session_id}"]`
4. Set every source agent's `endpoint_id` to the new endpoint's id
5. Leave `workflow_agents.command` and `.args` populated for fallback safety

The migration is idempotent: rerunning it on an already-migrated DB is a no-op (it sees `endpoint_id IS NOT NULL` for every agent and skips).

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
- Otherwise: parse `args` as JSON, substitute `{prompt}` (and `{session_id}` if `session_id` is provided and `session_config.resume_args` is set), append positionally if no `{prompt}` placeholder

### `endpoints.extract_session_id` contract

```python
def extract_session_id(endpoint, stdout, stderr, started_before) -> str | None:
    """
    Mine a session id from a completed invocation's output, if the endpoint
    advertises capabilities.sessions=True.

    Returns the session id or None.
    """
```

Phase 1 implementation: dispatch on `endpoint.command` for the existing claude/codex pattern matchers (preserving today's behaviour in `serve.py:1961-2013`). New endpoints that want session support specify their own pattern via `session_config.session_id_regex` (string regex applied to combined stdout+stderr). Hermes endpoint has `capabilities.sessions=False`, so this function is never invoked for hermes.

## API surface

All routes are global (no project scope), matching the existing `/api/workflow/agents` shape:

| Method | Route | Body | Returns |
|---|---|---|---|
| GET | `/api/endpoints` | — | `{"endpoints": [...]}` |
| POST | `/api/endpoints` | `{id, name, endpoint_type, command?, args?, prompt_mode?, timeout_s?, capabilities?, session_config?, provider?, model?, base_url?, api_key_env?}` | created endpoint or 400/409 |
| PUT | `/api/endpoints/{id}` | partial update | updated endpoint or 403 `system_endpoint` |
| DELETE | `/api/endpoints/{id}` | — | 204 or 403 `system_endpoint` or 409 if any agent points at it (warn but do not block; FK is `SET NULL`) |

### Validation

- `id` matches `^[a-zA-Z0-9_-]+$`
- `endpoint_type` must be in the CHECK list
- For `endpoint_type='cli'`: `command` is required
- For `endpoint_type` ending in `_api`: `provider` and `api_key_env` are required (validated at write time even though execution is unsupported, so we never persist obviously-broken API rows)
- `args` must be valid JSON; JSON decode error → 400
- `prompt_mode` defaults to `template`
- System rows (`system=1`) reject PUT/DELETE with 403 `system_endpoint` (mirrors today's `system_agent` 403 contract)

## CLI surface

```
tickets-cli.py endpoint list
tickets-cli.py endpoint add <id> --type cli --cmd claude --args '["-p","{prompt}"]' [--name ...] [--timeout-s 120]
tickets-cli.py endpoint update <id> [--cmd ...] [--args ...] [--timeout-s ...] [--name ...]
tickets-cli.py endpoint remove <id>
```

Existing `tickets-cli.py agent ...` subcommands gain an `--endpoint-id` flag on `add` and `update`. The `--cmd` and `--args` flags on agent commands are kept (they write to the compat columns) but emit a deprecation warning suggesting `--endpoint-id`.

## UI surface

In `/workflows` (the Workflows & Agents page):

- **Existing Agents tab**: each agent row gains an "Endpoint" dropdown populated from `/api/endpoints`. Saving an agent writes `endpoint_id`. System agents render the dropdown read-only as today.
- **New Endpoints tab**: same row-edit pattern as agents. Fields: id, name, endpoint_type (dropdown of all five types), command, args (JSON textarea), prompt_mode (dropdown of `template`/`stdin`), timeout_s.
- **Non-CLI types in the type dropdown** are selectable but show a yellow "execution not yet implemented" footer beneath the form. This makes the seam visible without misleading users about phase-1 capability.
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

## Error handling

| Situation | Behaviour |
|---|---|
| Agent has `endpoint_id` pointing at a missing endpoint row | Runner falls back to agent's compat `command`/`args`. Conversation log warns: "endpoint X not found, using legacy command". |
| Agent has `endpoint_id=NULL` and compat `command IS NOT NULL` | Runner uses compat path silently in phase 1 (will warn in a later phase). |
| Agent has `endpoint_id=NULL` and `command IS NULL` | Step fails with conversation message "agent X has no endpoint and no command — refusing to dispatch". |
| Endpoint has `endpoint_type != 'cli'` | `build_invocation` raises `UnsupportedEndpointType`. Runner catches and writes a clear conversation message naming the type. |
| Endpoint has invalid `args` JSON | `build_invocation` raises `EndpointMisconfigured`. Runner writes the parse error to the conversation. |
| Endpoint deleted while in use | FK `SET NULL` on agents that referenced it. UI shows "endpoint missing" warning on those agents. |
| `prompt_mode='stdin'` | Phase 1: `NotImplementedError`. Reserved for future. |

## Testing

### TDD (`tests/test_tdd_endpoints.py`)

- `build_invocation` with `{prompt}` placeholder substitutes correctly
- `build_invocation` with no placeholder appends positionally
- `build_invocation` with `{session_id}` placeholder injects resume args from `session_config`
- `build_invocation` raises `UnsupportedEndpointType` for `anthropic_api`, `openai_api`, `gemini_api`, `ssh_cli`
- `build_invocation` raises `EndpointMisconfigured` for invalid args JSON
- Migration #19 applied to a DB with sample legacy agents produces:
  - One endpoint per distinct `(command, args)` group
  - Every agent has `endpoint_id` set
  - System agents map to `system=1` endpoints; user agents map to `system=0` endpoints
- Migration is idempotent (rerunning is a no-op)

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

### Compat-path runtime check

Smoke test that runs an existing system workflow end-to-end against a real CLI on the dev machine, asserting it still succeeds. This validates that the migration didn't break any existing path.

## Rollout

1. Branch `feat/model-endpoints` (already created)
2. Build TDD tests first (red)
3. Implement schema + migration (green for migration tests)
4. Implement `endpoints.py` (green for build_invocation tests)
5. Wire runner through `endpoints.build_invocation`
6. Implement HTTP API + CLI (green for smoke tests)
7. Implement UI changes
8. Update `workflows_seed.py` and run the round-trip audit (`compare_seed_to_db.py`)
9. Manual end-to-end check of all 6 default agents
10. Deploy to `~/.claude/ticket-takeaway/` (per CLAUDE.md deployment notes), restart `serve.py`, smoke test on real WSL board

## Follow-ups (separate tickets, out of this branch)

- Drop `workflow_agents.command` and `.args` columns once we're confident the compat path is dead (one release later)
- Implement `endpoint_type='anthropic_api'` execution
- Implement `endpoint_type='openai_api'` execution (gives us OpenRouter + Codex API + any compatible)
- Implement `endpoint_type='gemini_api'` execution
- Implement `endpoint_type='ssh_cli'` execution
- Add a Hermes agent (persona) targeting `hermes-cli` (product decision: which workflows, if any, default to using it)
- Secret storage UI (today: `api_key_env` references an env var, no in-DB secrets)

## Open questions

None. All foundational decisions have been made:

- Conceptual split: agent (persona) vs endpoint (runtime) ✓
- One agent → one endpoint, no fallback chain ✓
- OpenRouter modelled as one endpoint per model ✓
- Phase 1 ships CLI execution only; API/SSH structurally permitted but rejected at runtime ✓
- `prompt_mode` enum is `template` + `stdin` only ✓
- Compat columns on `workflow_agents` retained for one release ✓
- Hermes seeded as endpoint only; no default agent or workflow change ✓
