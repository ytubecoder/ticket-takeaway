# Follow the Action — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A toggleable Follow mode on every kanban board that watches `activity_events` across all watched projects and directs attention to each action — spotlighting cards, captioning what happened, and navigating between project boards.

**Architecture:** New global polled endpoint `/api/activity/feed` (since-cursor over the single cross-project `activity_events` table) → client-side follow engine emitted by `generate.py` (poll → coalesce → play steps at fixed pace; cross-project steps are real navigations masked by a departure overlay, resumed via a sessionStorage arrival handoff). Spec: `docs/superpowers/specs/2026-07-30-follow-the-action-design.md` — read it before starting.

**Tech Stack:** Python 3 stdlib (sqlite3, http.server), vanilla JS/CSS emitted from Python strings, pytest.

## Global Constraints

- Work on branch `follow-the-action` off `main`. Never commit `PRODUCT_BACKLOG.md` in feature commits (it has unrelated pending changes; board sync owns it).
- `src/` is canonical; `~/.claude/ticket-takeaway/` is runtime. Deploy step = Task 8; do not cp mid-task.
- Verify gate: `WORKFLOW.toml [verify]` is pinned to `python3 -m pytest tests/test_tdd_*.py` — never widen it. Smoke/E2E hit the **real** `tickets.db`; run them deliberately, never in a loop.
- All feed consumers in JS unwrap with `data.events || data || []` (wrapped-response convention).
- Emitting JS from Python: do **NOT** use triple-quoted f-strings for the new follow-mode JS (brace-doubling and `\'` gotchas per CLAUDE.md). Use a plain string template + `.replace("__TOKEN__", json.dumps(...))`. Inline `onclick` needs `&apos;`/`&quot;` for nested quotes — the new code avoids inline onclick entirely (addEventListener only).
- `ruff format`/`ruff check` run via hooks; fix findings before finishing a task. SIM118 stays ignored (sqlite3.Row).
- Port 8787 may be owned by another app; use `--port 8790` for local verification servers, and check `lsof -nP -iTCP:8790 -sTCP:LISTEN` first. Production writer is port 8788 — only restart it in Task 8.
- macOS: `socket.getfqdn` can hang serve.py startup ~30s. If the test server prints `Serving N project(s)` then stalls, start it via a wrapper that monkey-patches `HTTPServer.server_bind` (reference: `/tmp/start-kitchen-demo.py`).

---

### Task 0: Branch + board ticket

**Files:** none (git + board only)

- [ ] **Step 1: Branch off up-to-date main**

```bash
cd ~/projects/ticket-takeaway
git fetch origin main && git checkout main && git pull
git checkout -b follow-the-action
```

- [ ] **Step 2: Create the board ticket and move it to WIP**

```bash
CLI=~/.claude/ticket-takeaway/tickets-cli.py
python3 $CLI add ticket-takeaway "Follow the Action — global activity-follow mode for the kanban" --section wip --priority high --tag dashboard --tag automation
python3 $CLI list --project ticket-takeaway | grep -i "follow the action"   # note the B-xx id
```

Record the ticket id — Task 8 moves it to `review`. Do NOT run `accept` (user approval required, always).

- [ ] **Step 3: Declare lane A (OpenSpec change)**

```bash
python3 $CLI spec ticket-takeaway <B-xx> --lane A
git add openspec/ && git commit -m "chore: openspec change scaffold for follow-the-action (<B-xx>)"
```

If `spec` scaffolds files outside `openspec/`, inspect with `git status` and commit only the scaffold. Leave `PRODUCT_BACKLOG.md` uncommitted.

---

### Task 1: Event taxonomy — cover the full vocabulary + follow precedence

**Files:**
- Modify: `src/constants.py:277-331`
- Test: `tests/test_tdd_activity_feed.py` (new file, first tests)

**Interfaces:**
- Produces: extended `EVENT_KIND_GROUPS` (new keys below), new `FOLLOW_KIND_PRECEDENCE: list[str]`. Task 6 embeds both into JS via `json.dumps`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tdd_activity_feed.py`:

```python
"""TDD for the cross-project activity feed (Follow the Action mode)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import constants


class TestTaxonomy:
    # Every event kind emit_event() is actually called with today (grep'd from
    # actions.py / kitchen.py / runners.py / serve.py / tickets-cli.py / seek.py)
    EMITTED_KINDS = {
        "ticket_created", "section_change", "status_change", "criteria_check",
        "field_changed", "gate_override", "mode_changed", "pause_set",
        "pause_cleared", "kitchen_paused", "kitchen_resumed", "run_started",
        "run_stalled", "run_failed", "run_succeeded", "run_cancelled",
        "run_discarded", "workspace_created", "handoff_recorded",
        "agent_output", "hook_started", "hook_succeeded", "hook_failed",
        "input_provided", "pane_linked", "pane_unlinked",
    }

    def test_every_emitted_kind_has_a_group(self):
        missing = self.EMITTED_KINDS - set(constants.EVENT_KIND_GROUPS)
        assert not missing, f"kinds without a group: {sorted(missing)}"

    def test_every_group_has_a_color(self):
        for kind, group in constants.EVENT_KIND_GROUPS.items():
            assert group in constants.EVENT_GROUP_COLORS, (kind, group)

    def test_precedence_is_known_kinds_and_starts_with_moves(self):
        assert constants.FOLLOW_KIND_PRECEDENCE[0] == "section_change"
        assert constants.FOLLOW_KIND_PRECEDENCE[1] == "ticket_created"
        for k in constants.FOLLOW_KIND_PRECEDENCE:
            assert k in constants.EVENT_KIND_GROUPS, k
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_tdd_activity_feed.py -v`
Expected: FAIL — missing kinds (`run_discarded`, `run_stalled`, `kitchen_paused`, …) and `AttributeError: FOLLOW_KIND_PRECEDENCE`.

- [ ] **Step 3: Extend constants.py**

Add to `EVENT_KIND_GROUPS` (keep existing entries untouched):

```python
    "criteria_removed": "Criteria",
    "criteria_changed": "Criteria",
    "run_stalled": "Run",
    "run_discarded": "Run",
    "mode_changed": "Pause",
    "kitchen_paused": "Pause",
    "kitchen_resumed": "Pause",
    "pane_linked": "Workspace",
    "pane_unlinked": "Workspace",
```

After `EVENT_GROUP_COLORS`, add:

```python
# Headline pick order for Follow-mode coalesced steps: when several events on
# one ticket collapse into a single animated step, the earliest kind in this
# list becomes the caption; the rest render as "+N more". Unknown kinds rank
# below everything listed.
FOLLOW_KIND_PRECEDENCE: list[str] = [
    "section_change",
    "ticket_created",
    "status_change",
    "gate_override",
    "run_failed",
    "run_stalled",
    "run_succeeded",
    "run_cancelled",
    "run_discarded",
    "run_started",
    "input_provided",
    "kitchen_paused",
    "kitchen_resumed",
    "pause_set",
    "pause_cleared",
    "mode_changed",
    "handoff_recorded",
    "agent_output",
    "hook_failed",
    "hook_started",
    "hook_succeeded",
    "workspace_created",
    "criteria_check",
    "criteria_added",
    "criteria_removed",
    "criteria_changed",
    "field_changed",
    "pane_linked",
    "pane_unlinked",
]
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_tdd_activity_feed.py -v` → PASS.
Also run the full gate: `python3 -m pytest tests/test_tdd_*.py -q` → no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/constants.py tests/test_tdd_activity_feed.py
git commit -m "feat: full event-kind taxonomy coverage + follow precedence order"
```

---

### Task 2: `actions.get_activity_feed()` (TDD)

**Files:**
- Modify: `src/actions.py` (below `get_ticket_activity`, ~line 2266)
- Test: `tests/test_tdd_activity_feed.py` (extend)

**Interfaces:**
- Consumes: `emit_event()` (actions.py:231), `ActorContext` (actions.py:207), `EVENT_KIND_GROUPS`.
- Produces:
  ```python
  def _resolve_agent_actor_names(conn, rows) -> dict[str, str]   # run_id -> display name
  def get_activity_feed(conn, since_id=None, limit=100, projects=None) -> dict
  # returns {"latest_id": int, "events": [ {id, project_id, project_name,
  #   subject_type, subject_id, ticket_title, section, event_kind, payload,
  #   actor_type, actor_name, occurred_at} ]}
  ```
  Tasks 3 and 4 call `get_activity_feed` with these exact names.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_tdd_activity_feed.py`)

```python
import json
import sqlite3

import pytest

import actions
from db import init_db


PROJECTS = [
    {"id": "p1", "name": "Project One", "watched": True},
    {"id": "p2", "name": "Project Two", "watched": False},
]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    c.execute(
        "INSERT INTO tickets (id, project_id, title, section, status) "
        "VALUES ('B-01', 'p1', 'First ticket', 'WIP', 'in-progress')"
    )
    c.commit()
    return c


def _emit(c, pid, kind, subject="B-01", payload=None, actor=None, subject_type="ticket"):
    eid = actions.emit_event(
        c, pid, subject_type, subject, kind, payload or {},
        actor or actions.ActorContext.human(),
    )
    c.commit()
    return eid


class TestActivityFeed:
    def test_no_since_returns_latest_only(self, conn):
        _emit(conn, "p1", "status_change", payload={"before": "a", "after": "b"})
        feed = actions.get_activity_feed(conn, projects=PROJECTS)
        assert feed["events"] == []
        assert feed["latest_id"] >= 1

    def test_empty_db_latest_zero(self, conn):
        feed = actions.get_activity_feed(conn, projects=PROJECTS)
        assert feed == {"latest_id": 0, "events": []}

    def test_since_ordering_and_limit(self, conn):
        ids = [_emit(conn, "p1", "field_changed") for _ in range(5)]
        feed = actions.get_activity_feed(conn, since_id=ids[1], limit=2, projects=PROJECTS)
        got = [e["id"] for e in feed["events"]]
        assert got == [ids[2], ids[3]]  # ascending, oldest first, limited

    def test_discarded_events_excluded(self, conn):
        keep = _emit(conn, "p1", "status_change")
        drop = _emit(conn, "p1", "status_change")
        conn.execute("UPDATE activity_events SET discarded_run_id = 99 WHERE id = ?", (drop,))
        conn.commit()
        feed = actions.get_activity_feed(conn, since_id=0, projects=PROJECTS)
        assert [e["id"] for e in feed["events"]] == [keep]

    def test_unwatched_project_filtered_but_latest_global(self, conn):
        _emit(conn, "p2", "status_change")   # unwatched
        feed = actions.get_activity_feed(conn, since_id=0, projects=PROJECTS)
        assert feed["events"] == []
        assert feed["latest_id"] >= 1        # latest_id is global, unfiltered

    def test_kitchen_sentinel_included_named(self, conn):
        _emit(conn, "_kitchen", "kitchen_paused", subject="lifecycle",
              subject_type="investigation", actor=actions.ActorContext.system())
        feed = actions.get_activity_feed(conn, since_id=0, projects=PROJECTS)
        assert len(feed["events"]) == 1
        assert feed["events"][0]["project_name"] == "Kitchen"

    def test_ticket_enrichment_and_deleted_null(self, conn):
        _emit(conn, "p1", "status_change")                       # exists
        _emit(conn, "p1", "status_change", subject="B-99")       # no such ticket
        feed = actions.get_activity_feed(conn, since_id=0, projects=PROJECTS)
        by_subject = {e["subject_id"]: e for e in feed["events"]}
        assert by_subject["B-01"]["ticket_title"] == "First ticket"
        assert by_subject["B-01"]["section"] == "WIP"
        assert by_subject["B-99"]["ticket_title"] is None
        assert by_subject["B-99"]["section"] is None

    def test_agent_actor_name_resolved(self, conn):
        conn.execute(
            "INSERT INTO runs (project_id, subject_type, subject_id, runner_kind, "
            "status, triggered_by, metadata_json) "
            "VALUES ('p1', 'ticket', 'B-01', 'agent', 'succeeded', 'human', "
            "'{\"workflow_name\": \"bounce-workflow\"}')"
        )
        run_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        conn.commit()
        _emit(conn, "p1", "section_change",
              payload={"before": "Backlog", "after": "WIP"},
              actor=actions.ActorContext.agent(str(run_id)))
        feed = actions.get_activity_feed(conn, since_id=0, projects=PROJECTS)
        assert feed["events"][0]["actor_name"] == "bounce-workflow"

    def test_projects_none_yields_only_kitchen(self, conn):
        _emit(conn, "p1", "status_change")
        feed = actions.get_activity_feed(conn, since_id=0, projects=None)
        assert feed["events"] == []
```

Note: if the `runs` INSERT fails on NOT NULL columns, check `grep -A20 "CREATE TABLE runs" src/db.py` and supply the minimum required columns — keep `metadata_json` with `workflow_name`.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_tdd_activity_feed.py -v`
Expected: FAIL — `AttributeError: module 'actions' has no attribute 'get_activity_feed'`.

- [ ] **Step 3: Implement in actions.py**

First refactor: inside `get_ticket_activity` (actions.py:2201-2241), extract the actor-name block into a module-level helper and call it from both places:

```python
def _resolve_agent_actor_names(conn: sqlite3.Connection, rows) -> dict[str, str]:
    """Map agent actor_ids (run ids) -> display names.

    Kitchen runs (numeric ids) carry workflow_name/agent_name in metadata_json;
    workflow_bounce runs (uuid ids) join workflow_runs -> workflows.name.
    """
    agent_run_ids: set[str] = {
        r["actor_id"] for r in rows if r["actor_type"] == "agent" and r["actor_id"]
    }
    actor_name_by_run: dict[str, str] = {}
    if not agent_run_ids:
        return actor_name_by_run
    numeric_ids = [rid for rid in agent_run_ids if rid.isdigit()]
    if numeric_ids:
        placeholders = ",".join("?" * len(numeric_ids))
        kitchen_rows = conn.execute(
            f"SELECT id, metadata_json FROM runs WHERE id IN ({placeholders})",
            numeric_ids,
        ).fetchall()
        for kr in kitchen_rows:
            try:
                meta = json.loads(kr["metadata_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            name = meta.get("workflow_name") or meta.get("agent_name")
            if name:
                actor_name_by_run[str(kr["id"])] = name
    uuid_ids = [rid for rid in agent_run_ids if not rid.isdigit()]
    if uuid_ids:
        placeholders = ",".join("?" * len(uuid_ids))
        try:
            wf_rows = conn.execute(
                f"SELECT wr.id AS id, w.name AS name "
                f"FROM workflow_runs wr LEFT JOIN workflows w ON w.id = wr.workflow_id "
                f"WHERE wr.id IN ({placeholders})",
                uuid_ids,
            ).fetchall()
            for wr in wf_rows:
                if wr["name"]:
                    actor_name_by_run[wr["id"]] = wr["name"]
        except sqlite3.OperationalError:
            pass
    return actor_name_by_run
```

Replace the inlined block in `get_ticket_activity` with `actor_name_by_run = _resolve_agent_actor_names(conn, rows)`.

Then add, below `get_ticket_activity`:

```python
def get_activity_feed(
    conn: sqlite3.Connection,
    since_id: int | None = None,
    limit: int = 100,
    projects: list[dict] | None = None,
) -> dict:
    """Cross-project forward feed over activity_events for Follow mode.

    `projects` is a list of registry dicts ({id, name, watched}). Events from
    unwatched projects are excluded; the `_kitchen` sentinel always passes.
    latest_id is the global unfiltered MAX(id) — filters only remove rows, so
    a cursor initialized here is always a correct "now".

    No since_id -> cursor-init shape: {"latest_id": N, "events": []}.
    """
    limit = max(1, min(int(limit or 100), 500))
    latest_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM activity_events"
    ).fetchone()["m"]
    if since_id is None:
        return {"latest_id": latest_id, "events": []}

    projects = projects or []
    name_by_id = {p["id"]: p.get("name", p["id"]) for p in projects}
    name_by_id["_kitchen"] = "Kitchen"
    allowed = [p["id"] for p in projects if p.get("watched", True)]
    allowed.append("_kitchen")

    placeholders = ",".join("?" * len(allowed))
    rows = conn.execute(
        f"SELECT id, project_id, subject_type, subject_id, actor_type, "
        f"       actor_id, event_kind, payload_json, occurred_at "
        f"FROM activity_events "
        f"WHERE id > ? AND discarded_run_id IS NULL "
        f"  AND project_id IN ({placeholders}) "
        f"ORDER BY id ASC LIMIT ?",
        [since_id, *allowed, limit],
    ).fetchall()

    ticket_meta: dict[tuple[str, str], sqlite3.Row] = {}
    ticket_keys = {
        (r["project_id"], r["subject_id"])
        for r in rows
        if r["subject_type"] == "ticket"
    }
    for pid, tid in ticket_keys:
        t = conn.execute(
            "SELECT title, section FROM tickets WHERE project_id = ? AND id = ?",
            (pid, tid),
        ).fetchone()
        if t:
            ticket_meta[(pid, tid)] = t

    actor_name_by_run = _resolve_agent_actor_names(conn, rows)

    events = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        tmeta = ticket_meta.get((r["project_id"], r["subject_id"]))
        actor_name = None
        if r["actor_type"] == "agent" and r["actor_id"]:
            actor_name = actor_name_by_run.get(r["actor_id"])
        events.append(
            {
                "id": r["id"],
                "project_id": r["project_id"],
                "project_name": name_by_id.get(r["project_id"], r["project_id"]),
                "subject_type": r["subject_type"],
                "subject_id": r["subject_id"],
                "ticket_title": tmeta["title"] if tmeta else None,
                "section": tmeta["section"] if tmeta else None,
                "event_kind": r["event_kind"],
                "payload": payload,
                "actor_type": r["actor_type"],
                "actor_name": actor_name,
                "occurred_at": r["occurred_at"],
            }
        )
    return {"latest_id": latest_id, "events": events}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_tdd_activity_feed.py -v` → PASS.
Then the full gate: `python3 -m pytest tests/test_tdd_*.py -q` → no regressions (the refactor touched `get_ticket_activity` — its existing tests must stay green).

- [ ] **Step 5: Commit**

```bash
git add src/actions.py tests/test_tdd_activity_feed.py
git commit -m "feat: actions.get_activity_feed cross-project since-cursor feed"
```

---

### Task 3: serve.py global route `GET /api/activity/feed`

**Files:**
- Modify: `src/serve.py` (global GET routes — insert directly after the `/api/kitchen/feed` block that ends at serve.py:9080)
- Test: `tests/test_smoke_activity_feed.py` (new; deliberate-run only)

**Interfaces:**
- Consumes: `actions.get_activity_feed(conn, since_id=, limit=, projects=)` from Task 2; existing `_PROJECTS_CACHE`, `_PROJECTS_CACHE_LOCK`, `_db_lock`, `get_db`, `init_db`, `self._send_json`.
- Produces: `GET /api/activity/feed?since_id=&limit=` returning Task 2's dict verbatim.

- [ ] **Step 1: Add the route**

Insert after the `/api/kitchen/feed` handler (match the module's existing import name for actions — serve.py already imports it):

```python
            # Global activity feed — drives kanban Follow mode. Polled ~2s by
            # boards with Follow enabled. since_id absent -> cursor init only.
            if remainder.startswith("/api/activity/feed"):
                query = urlparse(self.path).query
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                try:
                    since_id = int(params["since_id"]) if "since_id" in params else None
                except ValueError:
                    since_id = None
                try:
                    limit = int(params.get("limit", "100"))
                except ValueError:
                    limit = 100
                with _PROJECTS_CACHE_LOCK:
                    projects = list(_PROJECTS_CACHE.values())
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    feed = actions.get_activity_feed(
                        conn, since_id=since_id, limit=limit, projects=projects
                    )
                    conn.close()
                self._send_json(feed)
                return
```

Place it in the **global** (no-project) GET section only — the client polls the absolute `/api/activity/feed` path.

- [ ] **Step 2: Write the smoke test** (`tests/test_smoke_activity_feed.py`)

```python
"""Smoke: /api/activity/feed shape. Hits the REAL tickets.db via the
dashboard_server fixture — run deliberately, never as a gate."""

import json
import urllib.request


def _get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return json.loads(r.read())


def test_cursor_init_shape(dashboard_server):
    d = _get(dashboard_server, "/api/activity/feed")
    assert set(d) == {"latest_id", "events"}
    assert d["events"] == []
    assert isinstance(d["latest_id"], int)


def test_since_and_bad_params(dashboard_server):
    d = _get(dashboard_server, "/api/activity/feed?since_id=0&limit=5")
    assert isinstance(d["events"], list)
    assert len(d["events"]) <= 5
    if d["events"]:
        e = d["events"][0]
        assert {"id", "project_id", "project_name", "subject_id",
                "event_kind", "actor_type"} <= set(e)
    # junk params must not 500
    d2 = _get(dashboard_server, "/api/activity/feed?since_id=abc&limit=zz")
    assert set(d2) == {"latest_id", "events"}
```

Check `tests/conftest.py` for the `dashboard_server` fixture's base-URL shape (string vs object) and adapt `_get` accordingly.

- [ ] **Step 3: Verify by hand against a throwaway server**

```bash
lsof -nP -iTCP:8790 -sTCP:LISTEN   # must be empty
python3 ~/.claude/ticket-takeaway/serve.py --port 8790 &   # NOT yet redeployed — run src copy instead:
# python3 src/serve.py --port 8790 &   (check serve.py's arg convention with --help)
curl -s "http://localhost:8790/api/activity/feed" | python3 -m json.tool
curl -s "http://localhost:8790/api/activity/feed?since_id=0&limit=3" | python3 -m json.tool
kill %1
```

Expected: first call `{"latest_id": N, "events": []}`; second call ≤3 enriched events. (If startup stalls after "Serving N project(s)", use the getfqdn workaround from Global Constraints.)

- [ ] **Step 4: Commit**

```bash
git add src/serve.py tests/test_smoke_activity_feed.py
git commit -m "feat: global /api/activity/feed endpoint"
```

---

### Task 4: CLI `feed` subcommand

**Files:**
- Modify: `src/tickets-cli.py` (parser block ~line 2541+, plus the command dispatch)

**Interfaces:**
- Consumes: `actions.get_activity_feed` (Task 2), registry at `constants.DASHBOARD_DIR / "registry.json"` (shape: `{"projects": [{id, name, path, active, ...}]}`).
- Produces: `tickets-cli.py feed [--since N] [--limit N] [--json]`.

- [ ] **Step 1: Add the parser** (next to the other `sub.add_parser` calls):

```python
    p_feed = sub.add_parser("feed", help="Cross-project activity feed (read-only)")
    p_feed.add_argument("--since", type=int, default=None, help="events with id > SINCE")
    p_feed.add_argument("--limit", type=int, default=50)
    p_feed.add_argument("--json", action="store_true", dest="as_json")
```

- [ ] **Step 2: Add the handler** in the command dispatch (match the surrounding `args.<attr>` naming used by neighboring commands):

```python
    elif args.cmd == "feed":
        registry_path = constants.DASHBOARD_DIR / "registry.json"
        try:
            projects = json.loads(registry_path.read_text()).get("projects", [])
        except (OSError, json.JSONDecodeError):
            projects = []
        conn = get_db()
        init_db(conn)
        feed = actions.get_activity_feed(
            conn, since_id=args.since, limit=args.limit, projects=projects
        )
        conn.close()
        if args.as_json:
            print(json.dumps(feed, indent=2))
        else:
            for ev in feed["events"]:
                title = ev.get("ticket_title") or ""
                print(
                    f"{ev['id']:>6}  {ev['occurred_at']}  {ev['project_id']:<16} "
                    f"{ev['actor_type']:<6} {ev['event_kind']:<18} "
                    f"{ev['subject_id']} {title}"
                )
            print(f"latest_id: {feed['latest_id']}")
```

Reuse the CLI's existing db/constants imports — check the file head; do not re-import differently.

- [ ] **Step 3: Verify by hand**

```bash
python3 src/tickets-cli.py feed --limit 5 --since 0
python3 src/tickets-cli.py feed            # latest_id only, no rows
python3 src/tickets-cli.py feed --json --limit 2 | python3 -m json.tool
```

Expected: table rows + `latest_id:` line; JSON parses.

- [ ] **Step 4: Commit**

```bash
git add src/tickets-cli.py
git commit -m "feat: cli feed subcommand (cross-project activity)"
```

---

### Task 5: Board UI shell — CSS, Follow chip, ticker, departure overlay

**Files:**
- Modify: `src/generate.py` — new `build_follow_mode_css()` near the other builders (~line 840 region); chip markup in the filter bar (~line 4073-4090); ticker + overlay divs near the end of the board body; wire CSS into `generate_html` assembly (~line 1855).

**Interfaces:**
- Produces DOM ids Task 6 depends on, exactly: `#followChip` (button, contains `<span class="follow-dot">`), `#followTicker` (contains `#followTickerText`, `#followTickerQueue`), `#followDepart` (contains `.follow-depart-caption`). CSS classes: `.follow-spotlight` (on `.card`), `.follow-section-pulse`, `#followDepart.visible`.

- [ ] **Step 1: Add `build_follow_mode_css()`** (plain string, no f-string needed):

```python
def build_follow_mode_css() -> str:
    """CSS for Follow mode: chip dot, ticker bar, departure overlay, spotlight."""
    return """
#followChip .follow-dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: #22c55e; margin-left: 5px; animation: kitchen-pulse 1.6s ease-in-out infinite;
}
#followTicker {
  position: fixed; bottom: 0; left: 0; right: 0; z-index: 900;
  display: flex; align-items: center; gap: 10px; padding: 7px 14px;
  background: var(--bg-secondary); border-top: 1px solid var(--border);
  border-left: 4px solid #64748b; font-size: 12.5px; cursor: pointer;
  color: var(--text-primary);
}
#followTicker .follow-queue { color: var(--text-secondary); font-family: var(--font-mono); font-size: 11px; }
#followDepart {
  position: fixed; inset: 0; z-index: 2000; display: flex;
  align-items: center; justify-content: center; text-align: center;
  background: color-mix(in srgb, var(--bg-primary) 82%, transparent);
  backdrop-filter: blur(2px); opacity: 0; pointer-events: none;
  transition: opacity 0.3s ease;
}
#followDepart.visible { opacity: 1; pointer-events: auto; }
#followDepart .follow-depart-caption {
  font-size: 17px; font-weight: 600; max-width: 640px; padding: 18px 26px;
  background: var(--bg-secondary); border: 1px solid var(--border-strong);
  border-radius: 10px; animation: panelSlide 0.25s ease;
}
@keyframes follow-spotlight-ring {
  0%   { box-shadow: 0 0 0 0 rgba(59, 130, 246, 0.65); }
  40%  { box-shadow: 0 0 0 9px rgba(59, 130, 246, 0.18); }
  100% { box-shadow: 0 0 0 0 rgba(59, 130, 246, 0); }
}
.card.follow-spotlight { animation: follow-spotlight-ring 1.2s ease 2; }
.follow-section-pulse { animation: tt-pulse 1s ease 2; }
@media (prefers-reduced-motion: reduce) {
  #followChip .follow-dot, .card.follow-spotlight, .follow-section-pulse { animation: none; }
  #followDepart { transition: none; }
}
"""
```

Verify the keyframe names it reuses exist in the page CSS (`kitchen-pulse` generate.py:2215, `panelSlide` :2450, `tt-pulse` :3098); if a name differs, match the real one.

- [ ] **Step 2: Add the chip** at the end of the filter-bar chip row (after the Needs Attention wrap, inside `<div class="filter-bar" id="filterBar">`, generate.py:4073+):

```html
    <button class="filter-btn" id="followChip" data-testid="follow-chip"
            title="Follow the action: auto-navigate to tickets as agents act on them (all projects)">Follow<span class="follow-dot" style="display:none;"></span></button>
```

Note: `#followChip` deliberately has **no** `data-filter`/`data-group`, so `applyFilters()` ignores it.

- [ ] **Step 3: Add ticker + overlay markup** right before the closing `</body>` region of the board template (near where the app-toast div lives — search `id="app-toast"`):

```html
<div id="followTicker" style="display:none;">
  <span id="followTickerText"></span>
  <span class="follow-queue" id="followTickerQueue"></span>
</div>
<div id="followDepart"><div class="follow-depart-caption"></div></div>
```

- [ ] **Step 4: Wire CSS into the page.** In `generate_html`, next to `_rail_css = build_nav_rail_css()` (generate.py:1855), add `_follow_css = build_follow_mode_css()` and emit it inside the page `<style>` block the same way `_rail_css` is emitted (find `{_rail_css}` in the template and add `{_follow_css}` beside it).

- [ ] **Step 5: Regenerate + eyeball, then commit**

```bash
python3 src/generate.py            # regenerates docs/sdlc-dashboard.html
grep -c "followChip\|followTicker\|followDepart" docs/sdlc-dashboard.html   # expect >= 3
python3 -m pytest tests/test_tdd_*.py -q
git add src/generate.py
git commit -m "feat: follow-mode UI shell (chip, ticker, departure overlay, spotlight css)"
```

(`docs/sdlc-dashboard.html` is generated output — commit it only if the repo already tracks regenerated copies; check `git status` behavior of previous commits and follow suit.)

---

### Task 6: Follow engine JS + diff-poll scroll suppression

**Files:**
- Modify: `src/generate.py` — new `build_follow_mode_js()`; wire into `generate_html` next to `_rail_js`; one-line change at generate.py:4873.

**Interfaces:**
- Consumes: DOM ids from Task 5; `constants.EVENT_KIND_GROUPS`, `constants.EVENT_GROUP_COLORS`, `constants.FOLLOW_KIND_PRECEDENCE` (Task 1); endpoint from Task 3; existing metas `current-project` / `projects-list` (JSON array of `{id, name}`, injected by serve.py at serve time); cards keyed by `[data-item-id]` with `dataset.section`; ticket overlay `#ticket-detail-overlay` (`hidden` class); hash router `#ticket/{id}`.
- Produces: `window.__ttFollowActive` (read by the diff-poll suppression); localStorage `tt-follow-enabled`, `tt-follow-cursor`; sessionStorage `tt-follow-arrival`.

- [ ] **Step 1: Suppress the diff-poll's auto-scroll while Follow is on.** At generate.py:4873 change:

```js
if (firstChanged) setTimeout(function() {{ firstChanged.scrollIntoView({{ behavior: 'smooth', block: 'center' }}); }}, 100);
```

to:

```js
if (firstChanged && !window.__ttFollowActive) setTimeout(function() {{ firstChanged.scrollIntoView({{ behavior: 'smooth', block: 'center' }}); }}, 100);
```

- [ ] **Step 2: Add `build_follow_mode_js()`.** Plain string template + token replacement — **not** an f-string (house gotcha):

```python
def build_follow_mode_js() -> str:
    """Follow-mode engine. Plain-string template with json token injection —
    deliberately NOT an f-string (brace/quote gotchas per CLAUDE.md)."""
    template = r"""
(function () {
  var ENABLED_KEY = 'tt-follow-enabled';
  var CURSOR_KEY = 'tt-follow-cursor';
  var ARRIVAL_KEY = 'tt-follow-arrival';
  var POLL_MS = 2000, STEP_MS = 1600, OVERFLOW_LIMIT = 40;
  var ARRIVAL_TTL_MS = 30000, DEPART_MS = 1000;
  var KIND_GROUPS = __KIND_GROUPS__;
  var GROUP_COLORS = __GROUP_COLORS__;
  var PRECEDENCE = __PRECEDENCE__;

  function meta(n) { var m = document.querySelector('meta[name="' + n + '"]'); return m ? m.getAttribute('content') : null; }
  var pid = meta('current-project');
  var projectsRaw = meta('projects-list');
  if (!pid || !projectsRaw || location.protocol === 'file:') return;
  var projects = [];
  try { projects = JSON.parse(projectsRaw) || []; } catch (e) { return; }
  var nameByPid = {};
  projects.forEach(function (p) { nameByPid[p.id] = p.name || p.id; });

  var chip = document.getElementById('followChip');
  var ticker = document.getElementById('followTicker');
  var tickerText = document.getElementById('followTickerText');
  var tickerQueue = document.getElementById('followTickerQueue');
  var departOverlay = document.getElementById('followDepart');
  if (!chip || !ticker || !departOverlay) return;

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  var enabled = lsGet(ENABLED_KEY) === '1';
  var cursor = parseInt(lsGet(CURSOR_KEY) || '0', 10) || 0;
  var queue = [];       // [{events: [ev, ...]}] coalesced steps
  var playing = false;
  var failCount = 0;
  var reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

  var dragging = false;
  document.addEventListener('dragstart', function () { dragging = true; });
  document.addEventListener('dragend', function () { dragging = false; });

  function interactionBlocked() {
    var ov = document.getElementById('ticket-detail-overlay');
    if (ov && !ov.classList.contains('hidden')) return true;
    if (document.body.classList.contains('bounce-open')) return true;
    if (dragging) return true;
    var ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) return true;
    return false;
  }

  function setChipState() {
    chip.classList.toggle('active', enabled);
    chip.querySelector('.follow-dot').style.display = enabled ? '' : 'none';
    ticker.style.display = enabled ? '' : 'none';
    window.__ttFollowActive = enabled;
  }

  function apiFetch(sinceId) {
    var u = '/api/activity/feed' + (sinceId != null ? '?since_id=' + sinceId + '&limit=100' : '');
    return fetch(u).then(function (r) {
      if (!r.ok) throw new Error('http ' + r.status);
      return r.json();
    });
  }

  function initCursor(cb) {
    apiFetch(null).then(function (d) {
      cursor = d.latest_id || 0;
      lsSet(CURSOR_KEY, String(cursor));
      failCount = 0;
      if (cb) cb();
    }).catch(function () { failCount++; updateIdleTicker(); });
  }

  function groupKey(ev) { return ev.project_id + '|' + ev.subject_type + '|' + ev.subject_id; }

  function headline(events) {
    var best = events[0], bestRank = 9999;
    events.forEach(function (ev) {
      var r = PRECEDENCE.indexOf(ev.event_kind);
      if (r === -1) r = 5000;
      if (r < bestRank) { bestRank = r; best = ev; }
    });
    return best;
  }

  function enqueue(events) {
    events.forEach(function (ev) {
      var last = queue[queue.length - 1];
      if (last && groupKey(last.events[0]) === groupKey(ev)) last.events.push(ev);
      else queue.push({ events: [ev] });
    });
  }

  function queuedEventCount() {
    return queue.reduce(function (n, s) { return n + s.events.length; }, 0);
  }

  function poll() {
    if (!enabled || document.hidden) return;
    apiFetch(cursor).then(function (d) {
      failCount = 0;
      var evs = d.events || [];
      if (evs.length) enqueue(evs);
      if (queuedEventCount() > OVERFLOW_LIMIT) {
        var skipped = queuedEventCount();
        queue = [];
        cursor = Math.max(cursor, d.latest_id || 0);
        lsSet(CURSOR_KEY, String(cursor));
        showTicker('skipped ' + skipped + ' actions (burst)', '#94a3b8', '');
      }
      playNext();
      updateIdleTicker();
    }).catch(function () { failCount++; updateIdleTicker(); });
  }

  function actorLabel(ev) {
    if (ev.actor_type === 'agent') return '🤖 ' + (ev.actor_name || 'agent');
    if (ev.actor_type === 'system') return '⚙️ system';
    return '👤 human';
  }

  function subjectLabel(ev) {
    var t = ev.subject_id;
    if (ev.ticket_title) t += ' “' + ev.ticket_title + '”';
    return t;
  }

  function captionFor(ev, extra) {
    var p = ev.payload || {};
    var text;
    switch (ev.event_kind) {
      case 'section_change': text = 'moved ' + subjectLabel(ev) + ' ' + (p.before || '?') + ' → ' + (p.after || '?'); break;
      case 'ticket_created': text = 'created ' + subjectLabel(ev) + ' in ' + (p.section || '?'); break;
      case 'status_change': text = 'set ' + subjectLabel(ev) + ' ' + (p.before || '?') + ' → ' + (p.after || '?'); break;
      case 'run_started': text = 'started a run on ' + subjectLabel(ev); break;
      case 'run_succeeded': text = 'run succeeded on ' + subjectLabel(ev); break;
      case 'run_failed': text = 'run FAILED on ' + subjectLabel(ev); break;
      case 'kitchen_paused': text = 'Kitchen paused'; break;
      case 'kitchen_resumed': text = 'Kitchen resumed'; break;
      default: text = ev.event_kind.replace(/_/g, ' ') + ' · ' + subjectLabel(ev);
    }
    return actorLabel(ev) + ' ' + text + (extra ? ' (+' + extra + ' more)' : '');
  }

  function colorFor(ev) { return GROUP_COLORS[KIND_GROUPS[ev.event_kind]] || '#64748b'; }

  function showTicker(text, color, ticketId) {
    tickerText.textContent = text;
    ticker.style.borderLeftColor = color || '#64748b';
    tickerText.dataset.tid = ticketId || '';
    tickerQueue.textContent = queue.length > 0 ? '· ' + queue.length + ' queued' : '';
  }

  function updateIdleTicker() {
    if (!enabled || playing) return;
    if (failCount >= 5) { showTicker('feed offline — retrying', '#ef4444', ''); return; }
    if (!queue.length) showTicker('following · live', '#22c55e', '');
  }

  function playNext() {
    if (playing || !enabled || !queue.length) return;
    if (interactionBlocked()) { setTimeout(playNext, 1000); return; }
    var step = queue.shift();
    var ev = headline(step.events);
    var maxId = step.events.reduce(function (m, e) { return Math.max(m, e.id); }, 0);
    playing = true;
    if (ev.project_id !== pid && ev.project_id !== '_kitchen' && nameByPid[ev.project_id]) {
      depart(step, ev, maxId);
      return;
    }
    playLocal(step, ev, maxId);
  }

  function finishStep(maxId) {
    cursor = Math.max(cursor, maxId);
    lsSet(CURSOR_KEY, String(cursor));
    setTimeout(function () {
      playing = false;
      updateIdleTicker();
      playNext();
    }, STEP_MS);
  }

  function playLocal(step, ev, maxId) {
    var extra = step.events.length - 1;
    showTicker(captionFor(ev, extra), colorFor(ev), ev.subject_type === 'ticket' ? ev.subject_id : '');
    if (ev.subject_type === 'ticket') spotlight(ev.subject_id);
    finishStep(maxId);
  }

  function spotlight(ticketId) {
    var card = null;
    document.querySelectorAll('[data-item-id]').forEach(function (el) {
      if (!card && el.dataset.itemId === ticketId && !el.closest('.child-group')) card = el;
    });
    if (!card || card.offsetParent === null) {
      // absent, filtered out, or in a collapsed section — never touch the
      // user's filters/sections; pulse the section header if we can find it
      var sec = card ? card.closest('.bottom-section') : null;
      if (sec) {
        var h = sec.querySelector('h2, .bottom-section-header, summary');
        if (h) {
          h.classList.add('follow-section-pulse');
          setTimeout(function () { h.classList.remove('follow-section-pulse'); }, 2200);
        }
      }
      return;
    }
    card.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'center' });
    card.classList.remove('follow-spotlight');
    void card.offsetWidth;
    card.classList.add('follow-spotlight');
    setTimeout(function () { card.classList.remove('follow-spotlight'); }, 2600);
  }

  function depart(step, ev, maxId) {
    var target = ev.project_id;
    var cap = '→ ' + (nameByPid[target] || target) + ' · ' + captionFor(ev, step.events.length - 1);
    departOverlay.querySelector('.follow-depart-caption').textContent = cap;
    departOverlay.classList.add('visible');
    try {
      sessionStorage.setItem(ARRIVAL_KEY, JSON.stringify({
        pid: target,
        subjectType: ev.subject_type,
        subjectId: ev.subject_id,
        caption: captionFor(ev, step.events.length - 1),
        color: colorFor(ev),
        maxId: maxId,
        ts: Date.now()
      }));
    } catch (e) {}
    // Cursor invariant: cursor stays at its pre-step value here; the arrival
    // playback on the next page advances it. Lost handoff => replay, not loss.
    setTimeout(function () { location.href = '/' + encodeURIComponent(target) + '/kanban'; },
               reducedMotion ? 0 : DEPART_MS);
  }

  function checkArrival() {
    var raw = null;
    try {
      raw = sessionStorage.getItem(ARRIVAL_KEY);
      if (raw) sessionStorage.removeItem(ARRIVAL_KEY);
    } catch (e) {}
    if (!raw) return false;
    var a = null;
    try { a = JSON.parse(raw); } catch (e) { return false; }
    if (!a || a.pid !== pid || (Date.now() - (a.ts || 0)) > ARRIVAL_TTL_MS) return false;
    playing = true;
    showTicker(a.caption, a.color, a.subjectType === 'ticket' ? a.subjectId : '');
    if (a.subjectType === 'ticket') spotlight(a.subjectId);
    finishStep(a.maxId || cursor);
    return true;
  }

  chip.addEventListener('click', function () {
    enabled = !enabled;
    lsSet(ENABLED_KEY, enabled ? '1' : '0');
    setChipState();
    if (enabled) {
      queue = [];
      initCursor(function () { updateIdleTicker(); });  // enable ALWAYS resets to now
    } else {
      queue = [];
      playing = false;
    }
  });

  ticker.addEventListener('click', function () {
    var tid = tickerText.dataset.tid;
    if (tid) location.hash = '#ticket/' + tid;
  });

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && enabled) { queue = []; initCursor(null); }  // jump to now
  });

  setChipState();
  if (enabled) {
    if (!checkArrival() && !cursor) initCursor(null);
    updateIdleTicker();
  }
  setInterval(poll, POLL_MS);
})();
"""
    return (
        template.replace("__KIND_GROUPS__", json.dumps(EVENT_KIND_GROUPS))
        .replace("__GROUP_COLORS__", json.dumps(EVENT_GROUP_COLORS))
        .replace("__PRECEDENCE__", json.dumps(FOLLOW_KIND_PRECEDENCE))
    )
```

Check generate.py's imports: it must import `EVENT_KIND_GROUPS`, `EVENT_GROUP_COLORS`, `FOLLOW_KIND_PRECEDENCE` from `constants` (match the existing constants import style at the top of the file) and have `json` imported (it does).

- [ ] **Step 3: Wire the JS into the page.** In `generate_html` next to `_rail_js = build_nav_rail_js()` add `_follow_js = build_follow_mode_js()`, and emit `<script>{_follow_js}</script>` where `{_rail_js}` is emitted (after it — the engine needs the DOM present, and both are end-of-body).

- [ ] **Step 4: Regenerate + syntax-check the emitted page**

```bash
python3 src/generate.py
node --check <(python3 - <<'EOF'
import re, pathlib
html = pathlib.Path("docs/sdlc-dashboard.html").read_text()
for m in re.findall(r"<script>(.*?)</script>", html, re.S):
    print(m)
EOF
) 2>&1 | head   # any syntax error kills the whole block — must be clean
python3 -m pytest tests/test_tdd_*.py -q
```

If `node` is unavailable, open the page via the Task 3 throwaway server and check the browser console for `SyntaxError` instead.

- [ ] **Step 5: Commit**

```bash
git add src/generate.py
git commit -m "feat: follow-mode engine (poll/coalesce/play, cross-board navigation)"
```

---

### Task 7: E2E verification (deliberate; real DB, throwaway server on 8790)

**Files:** none (verification only). Uses Chrome DevTools MCP (house preference for manual E2E).

- [ ] **Step 1: Start a throwaway server on 8790 from src copies** (do NOT touch 8788):

```bash
lsof -nP -iTCP:8790 -sTCP:LISTEN   # must be empty
python3 src/serve.py --port 8790 &
```

- [ ] **Step 2: Baseline screenshots.** Navigate Chrome DevTools MCP to `http://localhost:8790/ticket-takeaway/kanban`. Screenshot the filter bar (chip off). Click `#followChip`; verify chip dot + ticker "following · live" appear; screenshot.

- [ ] **Step 3: Same-board action.** From a second shell:

```bash
python3 src/tickets-cli.py update ticket-takeaway <any-ticket-id> --status blocked
```

Within ~4s the ticker must caption the status change and the card must scroll+spotlight. Screenshot. Revert the status after.

- [ ] **Step 4: Cross-board action.** Pick another registered project `<p2>` and a ticket in it:

```bash
python3 src/tickets-cli.py move <p2> <ticket> wip
```

Expected within ~4s: departure overlay with "→ <P2 name> · …", navigation to `/<p2>/kanban`, arrival spotlight + ticker caption, Follow chip still on. Screenshot each stage (take_screenshot during the overlay may need quick timing — the console log sequence is acceptable evidence if the overlay frame is missed). Move the ticket back after.

- [ ] **Step 5: Guard + edge checks.**
  - Open a ticket overlay, fire another CLI move in `<p2>`: no navigation while open; it plays after closing.
  - Toggle Follow off/on: ticker resets to "following · live", no history replay.
  - `list_console_messages`: no errors.
- [ ] **Step 6: Cleanup + record.** Kill the 8790 server, close the browser (`browser_close`). Save screenshots to the scratchpad and send them to the user. Record verify on the ticket:

```bash
python3 src/tickets-cli.py verify ticket-takeaway <B-xx>
```

---

### Task 8: Deploy, restart production, ship

**Files:** runtime copies + git

- [ ] **Step 1: Full gate + lint**

```bash
python3 -m pytest tests/test_tdd_*.py -q
uvx ruff check src/
```

- [ ] **Step 2: Push branch + PR + merge** (house defaults: merge-commit, admin bypass if needed; fetch first and compare PR head vs origin branch tip before admin-merge):

```bash
git push -u origin follow-the-action
gh pr create --title "Follow the Action — global activity-follow mode" --body "Implements docs/superpowers/specs/2026-07-30-follow-the-action-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
git fetch origin && gh pr view --json headRefOid && git rev-parse origin/follow-the-action  # must match
gh pr merge --merge --admin
git checkout main && git pull
```

- [ ] **Step 3: Deploy runtime copies + regenerate + restart production (8788)**

```bash
cp src/generate.py src/serve.py src/actions.py src/constants.py src/tickets-cli.py ~/.claude/ticket-takeaway/
# regenerate EVERY project's board (generate.py edits are invisible without this)
python3 ~/.claude/ticket-takeaway/generate.py --help   # confirm per-project arg convention, then regenerate all registered projects
pkill -f "ticket-takeaway/serve.py"; sleep 1
python3 ~/.claude/ticket-takeaway/serve.py --port 8788 &   # match the flags the production instance was started with (check `ps aux | grep serve.py` output captured BEFORE pkill)
curl -s "http://localhost:8788/api/activity/feed" | python3 -m json.tool   # sanity
```

- [ ] **Step 4: Board bookkeeping** — move the ticket to review (never accept):

```bash
python3 ~/.claude/ticket-takeaway/tickets-cli.py move ticket-takeaway <B-xx> review
```

---

## Self-review notes (already applied)

- Spec coverage: endpoint (T2/T3), CLI parity (T4), taxonomy task (T1), chip/ticker/overlay (T5), engine incl. cursor invariant, enable-reset, coalesce key with subject_type, overflow, interaction guard, visibility, reduced-motion, `_kitchen` banner, projects-list preflight (T6 code), scroll ownership (T6 step 1), E2E + screenshots (T7), deploy gotchas (T8).
- Type consistency: `get_activity_feed(conn, since_id=, limit=, projects=)` used identically in T2/T3/T4; DOM ids `followChip/followTicker/followTickerText/followTickerQueue/followDepart` identical in T5/T6.
- Known judgment calls left to the executor, flagged inline: `runs` INSERT minimum columns (T2), `dashboard_server` fixture shape (T3), CLI dispatch attr naming (T4), generated-HTML commit convention (T5), production serve.py launch flags (T8).
