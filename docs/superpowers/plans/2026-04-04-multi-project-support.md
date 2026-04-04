# Multi-Project Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve all registered projects from one `serve.py` instance, each at `/{project-id}`, with project picker, settings pages, and header dropdown switcher.

**Architecture:** Replace the global `SERVER_PROJECT_ID` with per-request URL-based project resolution. Add project-scoped API routes under `/{pid}/api/`, global project management at `/api/projects`, server-rendered picker/settings pages. Two constant background threads iterate all projects.

**Tech Stack:** Python 3.10+ stdlib only (http.server, threading, json, re, html, pathlib). Vanilla JS for the dropdown.

**Spec:** `docs/superpowers/specs/2026-04-04-multi-project-support-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/serve.py` | Modify | Routing refactor, new pages, project APIs, background threads |
| `src/generate.py` | Modify | Per-project HTML generation (stop aggregating) |
| `src/tickets-cli.py` | Modify | `register` / `unregister` subcommands |
| `tests/test_tdd_routing.py` | Create | Unit tests for URL routing and validation |
| `tests/test_smoke_multiproject.py` | Create | API smoke tests for multi-project endpoints |

Files NOT modified: `src/constants.py`, `src/db.py`, `src/actions.py`.

---

## Security Requirements (from red-team review)

These MUST be implemented in the relevant tasks below:

1. **Path validation on project registration:** `path` must resolve to a real directory under `$HOME`, not `$HOME` itself, not under `~/.claude`
2. **Reserved ID blocklist:** Project IDs `api`, `settings`, `static`, `health`, `favicon.ico`, `index.html` are forbidden
3. **HTML escaping:** All project name/id values interpolated into HTML must use `html.escape(..., quote=True)`
4. **CORS:** Keep `http://localhost:{PORT}` as the allowed origin (don't change to `*`)

---

### Task 1: Per-Project HTML Generation (`generate.py`)

**Files:**
- Modify: `src/generate.py:484-493` (function signature + body)
- Modify: `src/generate.py:4172-4182` (main loop)
- Test: `tests/test_tdd_routing.py`

- [ ] **Step 1: Write failing test for single-project generation**

Create `tests/test_tdd_routing.py`:

```python
"""TDD tests for multi-project support."""
import sys
from pathlib import Path

# Add src/ to path so we can import generate module
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_generate_html_single_project():
    """generate_html() should accept a single Project (not a list)."""
    from generate import generate_html, Project

    proj = Project(id="test-proj", name="Test Project", path="", description="", active=True)
    proj.tickets = []
    html = generate_html(proj)
    assert "Test Project" in html
    assert "<html" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/user/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_routing.py::test_generate_html_single_project -v`

Expected: FAIL — `generate_html` currently expects a list, not a single Project.

- [ ] **Step 3: Change `generate_html` signature and body**

In `src/generate.py`, change lines 484-493:

```python
# Before:
def generate_html(projects: list[Project]) -> str:
    """Generate the full self-contained HTML dashboard."""
    primary = projects[0] if projects else None
    all_tickets: list[Ticket] = []
    for proj in projects:
        all_tickets.extend(proj.tickets)

# After:
def generate_html(project: Project) -> str:
    """Generate the full self-contained HTML dashboard for a single project."""
    primary = project
    all_tickets: list[Ticket] = list(project.tickets)
```

- [ ] **Step 4: Update `main()` generation loop**

In `src/generate.py`, change lines 4172-4182:

```python
# Before:
    html = generate_html(projects)
    output_paths = []
    for proj in projects:
        if proj.path:
            docs_dir = Path(proj.path) / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            out_path = docs_dir / "sdlc-dashboard.html"
            out_path.write_text(html, encoding="utf-8")
            output_paths.append(out_path)

# After:
    output_paths = []
    for proj in projects:
        html = generate_html(proj)
        if proj.path:
            docs_dir = Path(proj.path) / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            out_path = docs_dir / "sdlc-dashboard.html"
            out_path.write_text(html, encoding="utf-8")
            output_paths.append(out_path)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tdd_routing.py::test_generate_html_single_project -v`

Expected: PASS

- [ ] **Step 6: Run existing tests to verify no regression**

Run: `python3 -m pytest tests/test_tdd_*.py -v`

Expected: All existing TDD tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/generate.py tests/test_tdd_routing.py
git commit -m "refactor: generate_html takes single Project, not list"
```

---

### Task 2: Registry Cache + URL Resolver (`serve.py`)

**Files:**
- Modify: `src/serve.py:58-74` (replace SERVER_PROJECT_ID and _get_project)
- Test: `tests/test_tdd_routing.py`

- [ ] **Step 1: Write failing tests for `_resolve_project_from_path`**

Append to `tests/test_tdd_routing.py`:

```python
def test_resolve_project_known_prefix():
    """Known project ID in URL prefix returns (project, remainder)."""
    from serve import _resolve_project_from_path, _PROJECTS_CACHE, _PROJECTS_CACHE_LOCK
    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
        _PROJECTS_CACHE["goodform"] = {"id": "goodform", "name": "GoodForm", "path": "/tmp"}
    proj, remainder = _resolve_project_from_path("/goodform/api/tickets")
    assert proj["id"] == "goodform"
    assert remainder == "/api/tickets"


def test_resolve_project_root_path():
    """Root path returns (None, '/')."""
    from serve import _resolve_project_from_path
    proj, remainder = _resolve_project_from_path("/")
    assert proj is None
    assert remainder == "/"


def test_resolve_project_unknown_prefix():
    """Unknown prefix returns (None, original_path)."""
    from serve import _resolve_project_from_path, _PROJECTS_CACHE, _PROJECTS_CACHE_LOCK
    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
    proj, remainder = _resolve_project_from_path("/unknown/api/tickets")
    assert proj is None
    assert remainder == "/unknown/api/tickets"


def test_resolve_project_global_routes_not_captured():
    """Global routes like /api/projects should not match a project named 'api'."""
    from serve import _resolve_project_from_path, _PROJECTS_CACHE, _PROJECTS_CACHE_LOCK
    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
        _PROJECTS_CACHE["api"] = {"id": "api", "name": "bad", "path": "/tmp"}
    proj, remainder = _resolve_project_from_path("/api/projects")
    assert proj is None  # global routes take precedence


def test_resolve_project_bare_id():
    """/goodform (no trailing slash) should resolve correctly."""
    from serve import _resolve_project_from_path, _PROJECTS_CACHE, _PROJECTS_CACHE_LOCK
    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
        _PROJECTS_CACHE["goodform"] = {"id": "goodform", "name": "GoodForm", "path": "/tmp"}
    proj, remainder = _resolve_project_from_path("/goodform")
    assert proj["id"] == "goodform"
    assert remainder == "/"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tdd_routing.py -k "test_resolve" -v`

Expected: FAIL — `_resolve_project_from_path` doesn't exist yet.

- [ ] **Step 3: Implement cache and resolver in `serve.py`**

Replace lines 58-74 of `src/serve.py` (the `SERVER_PROJECT_ID`, `SERVER_PORT`, and `_get_project` block) with:

```python
# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

import html as _html

_LEGACY_PROJECT_ID = None  # Set from --project arg for backward compat
SERVER_PORT = 8787

# Lock for DB operations (sqlite3 connections aren't thread-safe)
_db_lock = threading.RLock()

# Registry cache — populated at startup, refreshed on /api/projects mutations
_PROJECTS_CACHE: dict[str, dict] = {}
_PROJECTS_CACHE_LOCK = threading.Lock()

# Global route prefixes that must never be captured as project IDs
_GLOBAL_PREFIXES = frozenset({"api", "settings", "static", "health", "favicon.ico", "index.html", ""})

# Reserved project IDs that cannot be registered
_RESERVED_IDS = frozenset({"api", "settings", "static", "health", "favicon.ico", "index.html"})


def _refresh_projects_cache() -> None:
    """Reload registry.json into the module-level cache. Thread-safe."""
    if not REGISTRY_PATH.exists():
        return
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return
    projects = {p["id"]: p for p in data.get("projects", []) if p.get("active", True)}
    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()
        _PROJECTS_CACHE.update(projects)


def _resolve_project_from_path(path: str) -> tuple[dict | None, str]:
    """Extract project from URL prefix. Returns (project_dict, remaining_path).

    /goodform/api/tickets  →  (goodform_project, "/api/tickets")
    /api/projects          →  (None, "/api/projects")
    /settings              →  (None, "/settings")
    /                      →  (None, "/")
    """
    parts = path.split("/", 2)  # ["", "segment", "rest..."]
    if len(parts) >= 2:
        candidate = parts[1]
        # Global routes take precedence — never capture these as project IDs
        if candidate in _GLOBAL_PREFIXES:
            return None, path
        with _PROJECTS_CACHE_LOCK:
            proj = _PROJECTS_CACHE.get(candidate)
        if proj is not None:
            remainder = "/" + parts[2] if len(parts) > 2 else "/"
            return proj, remainder
    return None, path


def _safe_attr(s: str) -> str:
    """Escape string for HTML attribute context."""
    return _html.escape(str(s), quote=True)
```

Keep `_get_project()` for now (it will be removed in Task 4 after all operation functions are updated).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tdd_routing.py -k "test_resolve" -v`

Expected: All 5 resolver tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve.py tests/test_tdd_routing.py
git commit -m "feat: add registry cache and URL-to-project resolver"
```

---

### Task 3: Thread `proj` Dict Through Operation Functions (`serve.py`)

**Files:**
- Modify: `src/serve.py:77-816` (all operation functions)

This is a pure refactor — every operation function changes from calling `_get_project()` internally to receiving `proj` as a parameter. No behavioral change.

- [ ] **Step 1: Refactor all 16 operation functions**

For each function listed below, apply this pattern:

**Before** (example: `_update_ticket_field` at line 77):
```python
def _update_ticket_field(project_id: str, ticket_id: str, field: str, value) -> bool:
    ...
    with _db_lock:
        conn = get_db()
        init_db(conn)
        proj = _get_project()       # ← remove this
        cli.ingest_markdown(conn, proj)
```

**After:**
```python
def _update_ticket_field(proj: dict, ticket_id: str, field: str, value) -> bool:
    project_id = proj["id"]
    ...
    with _db_lock:
        conn = get_db()
        init_db(conn)
        # proj is now a parameter — no _get_project() call
        cli.ingest_markdown(conn, proj)
```

Apply to all 16 functions (each one replaces `project_id: str` first arg with `proj: dict`, adds `project_id = proj["id"]` at the top, removes internal `proj = _get_project()` call):

| Function | Line | Has `_get_project()` call |
|----------|------|--------------------------|
| `_update_ticket_field` | 77 | Yes (line 89) |
| `_move_ticket` | 113 | Yes (line 130) |
| `_toggle_criterion` | 147 | Yes (line ~160) |
| `_update_criterion_text` | 189 | Yes (line ~202) |
| `_remove_criterion` | 224 | Yes (line ~237) |
| `_add_criterion` | 267 | Yes (line ~280) |
| `_update_depends` | 302 | Yes (line ~315) |
| `_create_ticket` | 336 | Yes (line ~349) |
| `_delete_ticket` | 368 | Yes (line 373) |
| `_accept_ticket` | 395 | Yes (line 400) |
| `_run_gate_check` | 474 | Yes (uses `_get_project().get("path")`) |
| `_run_category_assess` | 575 | Yes (uses `_get_project().get("path")`) |
| `_run_enrich` | 696 | Yes (uses `_get_project().get("path")`) |
| `_toggle_readiness` | 734 | Yes |
| `_update_readiness_content` | 776 | Yes |
| `_get_ticket_json` | 817 | No — keep `project_id: str` (called from many contexts) |

- [ ] **Step 2: Update all call sites in handlers**

In `do_GET`, `do_PUT`, `do_POST`, `do_DELETE`, every call like:
```python
proj = _get_project()
_update_ticket_field(proj["id"], ticket_id, field, value)
```
becomes:
```python
proj = _get_project()
_update_ticket_field(proj, ticket_id, field, value)
```

This is a temporary state — handlers still call `_get_project()`, but pass the result to operations instead of letting operations call it internally. Task 4 will eliminate `_get_project()` entirely.

- [ ] **Step 3: Run existing tests**

Run: `python3 -m pytest tests/test_tdd_*.py -v`

Expected: All pass (refactor is behavioral no-op).

- [ ] **Step 4: Commit**

```bash
git add src/serve.py
git commit -m "refactor: thread proj dict through all operation functions"
```

---

### Task 4: Switch Route Handlers to URL-Based Resolution (`serve.py`)

**Files:**
- Modify: `src/serve.py` — `do_GET`, `do_PUT`, `do_POST`, `do_DELETE` methods

This is the core routing change. Each handler switches from `_get_project()` to `_resolve_project_from_path()`.

- [ ] **Step 1: Refactor `do_GET`**

Replace the top of `do_GET` (line 925):

```python
def do_GET(self):
    path = urlparse(self.path).path

    proj, remainder = _resolve_project_from_path(path)

    # ── Global routes (proj is None) ────────────────────────────
    if proj is None:
        # Legacy backward compat: --project flag redirects bare /api/ routes
        if _LEGACY_PROJECT_ID and remainder.startswith("/api/"):
            self.send_response(301)
            self.send_header("Location", f"/{_LEGACY_PROJECT_ID}{remainder}")
            self.end_headers()
            return

        # Root: project picker (handled in Task 6)
        if remainder == "/" or remainder == "":
            # Temporary: redirect to first project or 404
            with _PROJECTS_CACHE_LOCK:
                first = next(iter(_PROJECTS_CACHE.values()), None)
            if first:
                self.send_response(302)
                self.send_header("Location", f"/{first['id']}")
                self.end_headers()
            else:
                self._send_json({"error": "No projects registered"}, 404)
            return

        self._send_json({"error": "Not found"}, 404)
        return

    # ── Project-scoped routes ────────────────────────────────────

    # Serve dashboard HTML
    if remainder == "/" or remainder == "/index.html":
        html_path = Path(os.path.expanduser(proj.get("path", ""))) / "docs" / "sdlc-dashboard.html"
        if html_path.exists():
            html = html_path.read_text(encoding="utf-8")
            # Inject edit-api meta tag — now project-scoped
            if '<meta name="edit-api"' not in html:
                idx = html.find('<meta name="gen-ts"')
                if idx != -1:
                    html = html[:idx] + f'<meta name="edit-api" content="http://localhost:{SERVER_PORT}/{proj["id"]}/api">\n' + html[idx:]
            self._send_html(html)
        else:
            self._send_json({"error": "Dashboard not generated yet. Run generate.py first."}, 404)
        return

    # JSON tickets API
    if remainder == "/api/tickets":
        project_id = proj["id"]
        conn = get_db()
        init_db(conn)
        rows = conn.execute(
            "SELECT id FROM tickets WHERE project_id = ? ORDER BY sort_order ASC",
            (project_id,)
        ).fetchall()
        tickets = []
        for r in rows:
            t = _get_ticket_json(project_id, r["id"])
            if t:
                tickets.append(t)
        conn.close()
        self._send_json({"project_id": project_id, "tickets": tickets})
        return

    # Single ticket
    m = re.match(r"^/api/tickets/([A-Za-z0-9_-]+)$", remainder)
    if m:
        t = _get_ticket_json(proj["id"], m.group(1))
        if t:
            self._send_json(t)
        else:
            self._send_json({"error": "Ticket not found"}, 404)
        return

    self._send_json({"error": "Not found"}, 404)
```

- [ ] **Step 2: Refactor `do_PUT`**

Same pattern — add `proj, remainder = _resolve_project_from_path(path)` at top. If `proj is None`, return 404. Replace all `path` in regex matches with `remainder`. Replace `_get_project()` calls with `proj`.

- [ ] **Step 3: Refactor `do_POST`**

Same pattern. Additionally handle the legacy redirect for `_LEGACY_PROJECT_ID` on POST routes.

- [ ] **Step 4: Refactor `do_DELETE`**

Same pattern.

- [ ] **Step 5: Update `main()` — replace `SERVER_PROJECT_ID` with `_LEGACY_PROJECT_ID`**

In `main()` (line 1369), change:

```python
def main():
    global _LEGACY_PROJECT_ID, SERVER_PORT

    args = sys.argv[1:]
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            SERVER_PORT = int(args[idx + 1])
    if "--project" in args:
        idx = args.index("--project")
        if idx + 1 < len(args):
            _LEGACY_PROJECT_ID = args[idx + 1]

    # Populate registry cache
    _refresh_projects_cache()

    if not _PROJECTS_CACHE:
        print("No active projects in registry. Register a project first.", file=sys.stderr)
        sys.exit(1)

    project_names = [p.get("name", p["id"]) for p in _PROJECTS_CACHE.values()]
    print(f"Serving {len(project_names)} project(s): {', '.join(project_names)}")
```

- [ ] **Step 6: Delete `_get_project()`**

Remove the now-unused `_get_project()` function (lines 69-74).

- [ ] **Step 7: Run all tests**

Run: `python3 -m pytest tests/test_tdd_*.py -v`

Expected: All pass.

- [ ] **Step 8: Manual smoke test**

```bash
cd /home/user/projects/ticket-takeaway
python3 src/serve.py &
# Open http://localhost:8787/ticket-takeaway — should show dashboard
# Open http://localhost:8787/ticket-takeaway/api/tickets — should return JSON
kill %1
```

- [ ] **Step 9: Commit**

```bash
git add src/serve.py
git commit -m "feat: project-scoped URL routing — /{project-id}/api/..."
```

---

### Task 5: Multi-Project Background Threads (`serve.py`)

**Files:**
- Modify: `src/serve.py:1283-1366` (watcher and poller functions)

- [ ] **Step 1: Refactor `_start_external_edit_watcher` to iterate all projects**

```python
def _start_external_edit_watcher(interval: float = 5.0):
    """Daemon thread polling for external PRODUCT_BACKLOG.md edits across all projects."""
    import time

    def _poll():
        while True:
            try:
                time.sleep(interval)
                with _PROJECTS_CACHE_LOCK:
                    snapshot = list(_PROJECTS_CACHE.values())
                for project in snapshot:
                    try:
                        with _db_lock:
                            conn = get_db()
                            init_db(conn)
                            changed = cli.detect_external_edits(conn, project)
                            if changed:
                                cli.regenerate_dashboard(project)
                                print(f"[watcher] External edits absorbed for {project.get('id', '?')}")
                            conn.close()
                    except Exception as exc:
                        print(f"[watcher] Error for {project.get('id', '?')}: {exc}")
            except Exception:
                import traceback
                traceback.print_exc()

    t = threading.Thread(target=_poll, daemon=True, name="md-edit-watcher")
    t.start()
```

- [ ] **Step 2: Refactor `_start_scheduled_event_poller` to handle all projects**

```python
def _start_scheduled_event_poller(interval: float = 30.0):
    """Daemon thread executing scheduled events across all projects."""
    import time

    def _poll():
        while True:
            try:
                time.sleep(interval)
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    now = datetime.now().isoformat()
                    due = conn.execute(
                        "SELECT * FROM scheduled_events WHERE fired = 0 AND fire_at <= ? "
                        "ORDER BY fire_at ASC",
                        (now,),
                    ).fetchall()
                    # Group syncs by project to avoid redundant regen
                    projects_to_sync = set()
                    for event in due:
                        try:
                            execute_scheduled_event(conn, event)
                            conn.execute(
                                "UPDATE scheduled_events SET fired = 1 WHERE id = ?",
                                (event["id"],),
                            )
                            projects_to_sync.add(event["project_id"])
                        except Exception:
                            conn.execute(
                                "UPDATE scheduled_events SET fired = 1 WHERE id = ?",
                                (event["id"],),
                            )
                            import traceback
                            traceback.print_exc()
                    if projects_to_sync:
                        conn.commit()
                        with _PROJECTS_CACHE_LOCK:
                            cache_snap = dict(_PROJECTS_CACHE)
                        for pid in projects_to_sync:
                            proj = cache_snap.get(pid)
                            if proj:
                                cli.sync_to_markdown(conn, proj)
                                cli.regenerate_dashboard(proj)
                    conn.close()
            except Exception:
                import traceback
                traceback.print_exc()

    t = threading.Thread(target=_poll, daemon=True, name="scheduled-event-poller")
    t.start()
```

- [ ] **Step 3: Update `main()` to call without project arg**

Change the startup calls in `main()`:

```python
# Before:
_start_external_edit_watcher(proj)
_start_scheduled_event_poller(proj)

# After:
_start_external_edit_watcher()
_start_scheduled_event_poller()
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_tdd_*.py -v`

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add src/serve.py
git commit -m "feat: background threads iterate all projects"
```

---

### Task 6: Project Picker Page (`/`)

**Files:**
- Modify: `src/serve.py` — add `_render_project_picker()` and wire into `do_GET`

- [ ] **Step 1: Implement `_render_project_picker`**

Add this function above the `DashboardHandler` class in `src/serve.py`:

```python
def _render_project_picker(port: int) -> str:
    """Render the project picker page as self-contained HTML."""
    conn = get_db()
    init_db(conn)

    # Get ticket summary counts per project
    counts_by_project = {}
    rows = conn.execute(
        "SELECT project_id, section, COUNT(*) as cnt FROM tickets GROUP BY project_id, section"
    ).fetchall()
    for r in rows:
        pid = r["project_id"]
        if pid not in counts_by_project:
            counts_by_project[pid] = {}
        counts_by_project[pid][r["section"]] = r["cnt"]
    conn.close()

    with _PROJECTS_CACHE_LOCK:
        projects = list(_PROJECTS_CACHE.values())

    cards_html = ""
    for proj in projects:
        pid = proj["id"]
        name = _safe_attr(proj.get("name", pid))
        raw_path = proj.get("path", "")
        display_path = raw_path.replace(str(Path.home()), "~")
        counts = counts_by_project.get(pid, {})
        wip = counts.get("WIP", 0)
        backlog = counts.get("Backlog", 0)
        review = counts.get("For Review", 0)

        path_exists = Path(os.path.expanduser(raw_path)).is_dir() if raw_path else False
        opacity = "1" if path_exists else "0.5"

        cards_html += f'''
        <a href="/{_safe_attr(pid)}" class="proj-card" style="opacity:{opacity}">
          <div class="proj-card-name">{name}</div>
          <div class="proj-card-path">{_safe_attr(display_path)}</div>
          <div class="proj-card-counts">
            <span class="count-wip">{wip} WIP</span>
            <span class="count-backlog">{backlog} Backlog</span>
            <span class="count-review">{review} Review</span>
          </div>
          {'' if path_exists else '<div class="proj-card-warn">Path not found</div>'}
        </a>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ticket Takeaway</title>
<style>
:root {{
  --bg-page: #0a0a0b; --bg-surface: #141417; --bg-card: #1a1a1f; --bg-hover: #222228;
  --border-subtle: #1e1e24; --border-default: #2a2a32; --text-primary: #ededef;
  --text-secondary: #a0a0ab; --text-tertiary: #6b6b76; --accent: #3b82f6;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: var(--bg-page); color: var(--text-primary); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 32px; }}
.header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 32px; padding-bottom: 16px; border-bottom: 1px solid var(--border-default); }}
.header h1 {{ font-size: 20px; font-weight: 600; }}
.header .count {{ color: var(--text-tertiary); font-size: 13px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; max-width: 900px; }}
.proj-card {{ display: block; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 10px; padding: 20px; text-decoration: none; color: inherit; transition: border-color 0.15s, background 0.15s; }}
.proj-card:hover {{ border-color: var(--accent); background: var(--bg-hover); }}
.proj-card-name {{ font-size: 16px; font-weight: 600; color: var(--text-primary); margin-bottom: 6px; }}
.proj-card-path {{ font-size: 12px; color: var(--text-tertiary); font-family: monospace; margin-bottom: 12px; }}
.proj-card-counts {{ display: flex; gap: 12px; font-size: 12px; }}
.count-wip {{ color: #f59e0b; }}
.count-backlog {{ color: #3b82f6; }}
.count-review {{ color: #ec4899; }}
.proj-card-warn {{ color: #ef4444; font-size: 11px; margin-top: 8px; }}
.add-card {{ display: flex; align-items: center; justify-content: center; background: transparent; border: 2px dashed var(--border-default); border-radius: 10px; padding: 20px; min-height: 110px; cursor: pointer; transition: border-color 0.15s; color: var(--text-tertiary); text-decoration: none; }}
.add-card:hover {{ border-color: var(--accent); }}
.add-card-inner {{ text-align: center; }}
.add-card-plus {{ font-size: 24px; color: var(--text-tertiary); }}
.add-card-label {{ font-size: 13px; margin-top: 4px; }}
.add-form {{ display: none; background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 10px; padding: 20px; margin-top: 16px; max-width: 500px; }}
.add-form.visible {{ display: block; }}
.add-form label {{ display: block; color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; margin-top: 14px; }}
.add-form label:first-child {{ margin-top: 0; }}
.add-form input {{ width: 100%; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 6px; padding: 8px 12px; color: var(--text-primary); font-size: 14px; }}
.add-form input:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }}
.add-form .btn {{ display: inline-block; margin-top: 16px; padding: 8px 20px; background: rgba(59,130,246,0.15); border: 1px solid rgba(59,130,246,0.3); color: var(--accent); border-radius: 6px; cursor: pointer; font-size: 13px; }}
.add-form .btn:hover {{ background: rgba(59,130,246,0.25); }}
.add-form .error {{ color: #ef4444; font-size: 12px; margin-top: 8px; display: none; }}
</style>
</head>
<body>
<div class="header">
  <h1>Ticket Takeaway</h1>
  <span class="count">{len(projects)} project{"s" if len(projects) != 1 else ""} registered</span>
</div>
<div class="grid">
  {cards_html}
  <div class="add-card" onclick="document.getElementById('add-form').classList.toggle('visible')" data-testid="add-project-card">
    <div class="add-card-inner">
      <div class="add-card-plus">+</div>
      <div class="add-card-label">Add Project</div>
    </div>
  </div>
</div>
<form id="add-form" class="add-form" data-testid="add-project-form">
  <label>Project Name</label>
  <input name="name" placeholder="My Project" required data-testid="add-project-name">
  <label>Project Path</label>
  <input name="path" placeholder="~/projects/my-project" required data-testid="add-project-path">
  <label>Project ID <span style="color:var(--text-tertiary)">(auto-generated from name)</span></label>
  <input name="id" placeholder="my-project" data-testid="add-project-id">
  <label>Description <span style="color:var(--text-tertiary)">(optional)</span></label>
  <input name="description" placeholder="Brief description">
  <button type="submit" class="btn">Add Project</button>
  <div class="error" id="add-error"></div>
</form>
<script>
(function() {{
  var nameInput = document.querySelector('[name="name"]');
  var idInput = document.querySelector('[name="id"]');
  if (nameInput && idInput) {{
    nameInput.addEventListener('input', function() {{
      if (!idInput.dataset.manual) {{
        idInput.value = nameInput.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      }}
    }});
    idInput.addEventListener('input', function() {{ idInput.dataset.manual = '1'; }});
  }}
  var form = document.getElementById('add-form');
  var errorDiv = document.getElementById('add-error');
  if (form) {{
    form.addEventListener('submit', function(e) {{
      e.preventDefault();
      errorDiv.style.display = 'none';
      var data = {{
        id: form.elements.id.value || form.elements.name.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''),
        name: form.elements.name.value,
        path: form.elements.path.value,
        description: form.elements.description.value
      }};
      fetch('/api/projects', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(data)
      }}).then(function(r) {{ return r.json().then(function(j) {{ return {{ok: r.ok, data: j}}; }}); }})
      .then(function(res) {{
        if (res.ok) {{ window.location.href = '/' + res.data.id; }}
        else {{ errorDiv.textContent = res.data.error || 'Failed'; errorDiv.style.display = 'block'; }}
      }}).catch(function(err) {{ errorDiv.textContent = err.message; errorDiv.style.display = 'block'; }});
    }});
  }}
}})();
</script>
</body>
</html>'''
```

- [ ] **Step 2: Wire into `do_GET`**

In the `do_GET` global routes section (from Task 4), replace the temporary redirect with:

```python
if remainder == "/" or remainder == "":
    html = _render_project_picker(SERVER_PORT)
    self._send_html(html)
    return
```

- [ ] **Step 3: Manual test**

Start server, open `http://localhost:8787/` — should show project cards.

- [ ] **Step 4: Commit**

```bash
git add src/serve.py
git commit -m "feat: project picker page at root URL"
```

---

### Task 7: Project Management API (`/api/projects`)

**Files:**
- Modify: `src/serve.py` — add global API routes
- Test: `tests/test_smoke_multiproject.py`

- [ ] **Step 1: Add validation helper**

Add to `src/serve.py` near the other helpers:

```python
import re as _re

_SLUG_RE = _re.compile(r'^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$')


def _validate_project_registration(body: dict) -> str | None:
    """Validate project registration fields. Returns error string or None."""
    pid = body.get("id", "")
    if not _SLUG_RE.match(pid):
        return "id must be 2-40 chars, lowercase alphanumeric and hyphens"
    if pid in _RESERVED_IDS:
        return f"'{pid}' is a reserved name and cannot be used as a project ID"

    path = body.get("path", "")
    if not path:
        return "path is required"

    resolved = Path(os.path.realpath(os.path.expanduser(path)))
    if not resolved.is_dir():
        return "path does not exist or is not a directory"

    home = Path.home().resolve()
    try:
        resolved.relative_to(home)
    except ValueError:
        return "path must be within the user's home directory"

    if resolved == home:
        return "path cannot be the home directory itself"
    claude_dir = (home / ".claude").resolve()
    if resolved == claude_dir or str(resolved).startswith(str(claude_dir) + os.sep):
        return "path cannot be inside ~/.claude"

    # Check for duplicate ID
    with _PROJECTS_CACHE_LOCK:
        if pid in _PROJECTS_CACHE:
            return f"project '{pid}' already exists"

    return None
```

- [ ] **Step 2: Add `GET /api/projects` handler**

In `do_GET`, in the global routes section (where `proj is None`), add before the 404 fallback:

```python
    if remainder == "/api/projects":
        with _PROJECTS_CACHE_LOCK:
            projects_list = list(_PROJECTS_CACHE.values())
        # Enrich with summary counts
        conn = get_db()
        init_db(conn)
        counts_rows = conn.execute(
            "SELECT project_id, section, COUNT(*) as cnt FROM tickets GROUP BY project_id, section"
        ).fetchall()
        conn.close()
        counts_map = {}
        for r in counts_rows:
            counts_map.setdefault(r["project_id"], {})[r["section"]] = r["cnt"]
        result = []
        for p in projects_list:
            c = counts_map.get(p["id"], {})
            result.append({
                "id": p["id"], "name": p.get("name", p["id"]),
                "path": p.get("path", ""), "description": p.get("description", ""),
                "active": p.get("active", True),
                "ticket_counts": {"wip": c.get("WIP", 0), "backlog": c.get("Backlog", 0), "review": c.get("For Review", 0)}
            })
        self._send_json({"projects": result})
        return
```

- [ ] **Step 3: Add `POST /api/projects` handler**

In `do_POST`, in the global routes section (where `proj is None`), add:

```python
    if proj is None and remainder == "/api/projects":
        try:
            body = self._read_body()
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        error = _validate_project_registration(body)
        if error:
            self._send_json({"error": error}, 400)
            return

        new_project = {
            "id": body["id"],
            "name": body.get("name", body["id"]),
            "path": body["path"],
            "description": body.get("description", ""),
            "active": True,
        }

        # Write to registry.json
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                registry = json.load(f)
        except (json.JSONDecodeError, IOError):
            registry = {"projects": []}

        registry["projects"].append(new_project)
        with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2)

        _refresh_projects_cache()

        # Init DB tables if needed
        conn = get_db()
        init_db(conn)
        conn.close()

        self._send_json(new_project, 201)
        return
```

- [ ] **Step 4: Add `PUT /api/projects/{pid}` handler**

In `do_PUT`, add a global route check at the top:

```python
    if proj is None:
        m = re.match(r"^/api/projects/([a-z0-9][a-z0-9-]*[a-z0-9])$", remainder)
        if m:
            pid = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            try:
                with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                    registry = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._send_json({"error": "Registry not found"}, 500)
                return

            found = False
            for entry in registry["projects"]:
                if entry["id"] == pid:
                    for field in ("name", "path", "description", "active"):
                        if field in body:
                            entry[field] = body[field]
                    found = True
                    break

            if not found:
                self._send_json({"error": "Project not found"}, 404)
                return

            with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                json.dump(registry, f, indent=2)
            _refresh_projects_cache()
            self._send_json(entry)
            return

        self._send_json({"error": "Not found"}, 404)
        return
```

- [ ] **Step 5: Add `DELETE /api/projects/{pid}` handler**

In `do_DELETE`, add a global route check at the top:

```python
    if proj is None:
        m = re.match(r"^/api/projects/([a-z0-9][a-z0-9-]*[a-z0-9])$", remainder)
        if m:
            pid = m.group(1)
            try:
                with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                    registry = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._send_json({"error": "Registry not found"}, 500)
                return

            found = False
            for entry in registry["projects"]:
                if entry["id"] == pid:
                    entry["active"] = False
                    found = True
                    break

            if not found:
                self._send_json({"error": "Project not found"}, 404)
                return

            with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                json.dump(registry, f, indent=2)
            _refresh_projects_cache()
            self._send_json({"ok": True, "deactivated": pid})
            return

        self._send_json({"error": "Not found"}, 404)
        return
```

- [ ] **Step 6: Write smoke test**

Create `tests/test_smoke_multiproject.py`:

```python
"""Smoke tests for multi-project API endpoints."""
import json
import urllib.request


def test_get_projects(dashboard_server):
    """GET /api/projects returns project list."""
    url = f"http://localhost:{dashboard_server}/api/projects"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    assert "projects" in data
    assert isinstance(data["projects"], list)
    assert len(data["projects"]) >= 1


def test_project_scoped_tickets(dashboard_server):
    """GET /{project-id}/api/tickets returns scoped tickets."""
    # First get the project list to find a valid project ID
    url = f"http://localhost:{dashboard_server}/api/projects"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    pid = data["projects"][0]["id"]

    tickets_url = f"http://localhost:{dashboard_server}/{pid}/api/tickets"
    with urllib.request.urlopen(tickets_url) as resp:
        tickets_data = json.loads(resp.read())
    assert "tickets" in tickets_data
    assert tickets_data["project_id"] == pid
```

- [ ] **Step 7: Run smoke tests**

Run: `python3 -m pytest tests/test_smoke_multiproject.py -v` (requires serve.py running)

- [ ] **Step 8: Commit**

```bash
git add src/serve.py tests/test_smoke_multiproject.py
git commit -m "feat: project management API — GET/POST/PUT/DELETE /api/projects"
```

---

### Task 8: Project Settings Page (`/{pid}/settings`)

**Files:**
- Modify: `src/serve.py` — add `_render_project_settings()` and wire into `do_GET`

- [ ] **Step 1: Implement `_render_project_settings`**

Add to `src/serve.py`:

```python
def _render_project_settings(proj: dict, port: int) -> str:
    """Render the settings page for a single project."""
    pid = _safe_attr(proj["id"])
    name = _safe_attr(proj.get("name", proj["id"]))
    path = _safe_attr(proj.get("path", ""))
    description = _safe_attr(proj.get("description", ""))
    active = proj.get("active", True)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} — Settings</title>
<style>
:root {{
  --bg-page: #0a0a0b; --bg-surface: #141417; --bg-card: #1a1a1f; --bg-hover: #222228;
  --border-subtle: #1e1e24; --border-default: #2a2a32; --text-primary: #ededef;
  --text-secondary: #a0a0ab; --text-tertiary: #6b6b76; --accent: #3b82f6;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: var(--bg-page); color: var(--text-primary); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 32px; max-width: 600px; }}
.back {{ color: var(--text-tertiary); text-decoration: none; font-size: 13px; }}
.back:hover {{ color: var(--text-secondary); }}
h1 {{ font-size: 18px; font-weight: 600; margin: 16px 0 24px; }}
label {{ display: block; color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; margin-top: 20px; }}
input, textarea {{ width: 100%; background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 6px; padding: 8px 12px; color: var(--text-primary); font-size: 14px; font-family: inherit; }}
input:focus, textarea:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }}
input[readonly] {{ background: var(--bg-page); color: var(--text-tertiary); border-color: var(--border-subtle); }}
textarea {{ min-height: 60px; resize: vertical; }}
.toggle-wrap {{ display: flex; align-items: center; gap: 10px; margin-top: 20px; }}
.toggle {{ width: 36px; height: 20px; border-radius: 10px; cursor: pointer; position: relative; transition: background 0.15s; border: none; }}
.toggle.on {{ background: #22c55e; }}
.toggle.off {{ background: var(--border-default); }}
.toggle::after {{ content: ''; position: absolute; width: 16px; height: 16px; background: white; border-radius: 50%; top: 2px; transition: left 0.15s; }}
.toggle.on::after {{ left: 18px; }}
.toggle.off::after {{ left: 2px; }}
.btn {{ display: inline-block; margin-top: 24px; padding: 8px 20px; background: rgba(59,130,246,0.15); border: 1px solid rgba(59,130,246,0.3); color: var(--accent); border-radius: 6px; cursor: pointer; font-size: 13px; }}
.btn:hover {{ background: rgba(59,130,246,0.25); }}
.danger {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--border-default); }}
.danger h3 {{ color: #ef4444; font-size: 12px; font-weight: 600; margin-bottom: 12px; }}
.danger .btn {{ background: rgba(239,68,68,0.15); border-color: rgba(239,68,68,0.3); color: #ef4444; margin-top: 0; }}
.danger .btn:hover {{ background: rgba(239,68,68,0.25); }}
.danger p {{ color: var(--text-tertiary); font-size: 11px; margin-top: 6px; }}
.msg {{ font-size: 12px; margin-top: 8px; display: none; }}
.msg.ok {{ color: #22c55e; }}
.msg.err {{ color: #ef4444; }}
</style>
</head>
<body>
<a href="/{pid}" class="back">&larr; Back to board</a>
<h1>{name} Settings</h1>
<form id="settings-form" data-testid="project-settings-form">
  <label>Project Name</label>
  <input name="name" value="{name}" data-testid="settings-name">
  <label>Project Path</label>
  <input name="path" value="{path}" style="font-family:monospace" data-testid="settings-path">
  <label>Description</label>
  <textarea name="description" data-testid="settings-description">{description}</textarea>
  <label>Project ID <span style="color:var(--text-tertiary)">(read-only)</span></label>
  <input name="id" value="{pid}" readonly data-testid="settings-id">
  <div class="toggle-wrap">
    <button type="button" class="toggle {'on' if active else 'off'}" id="active-toggle" data-testid="settings-active-toggle"></button>
    <span style="font-size:13px">Active</span>
  </div>
  <button type="submit" class="btn" data-testid="settings-save">Save Changes</button>
  <div class="msg" id="save-msg"></div>
</form>
<div class="danger">
  <h3>Danger Zone</h3>
  <button class="btn" id="remove-btn" data-testid="settings-remove">Remove Project</button>
  <p>Removes from registry only. Does not delete files, tickets, or database entries.</p>
</div>
<script>
(function() {{
  var activeOn = {'true' if active else 'false'};
  var toggle = document.getElementById('active-toggle');
  toggle.addEventListener('click', function() {{
    activeOn = !activeOn;
    toggle.className = 'toggle ' + (activeOn ? 'on' : 'off');
  }});

  var form = document.getElementById('settings-form');
  var msg = document.getElementById('save-msg');
  form.addEventListener('submit', function(e) {{
    e.preventDefault();
    msg.style.display = 'none';
    fetch('/api/projects/{pid}', {{
      method: 'PUT',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        name: form.elements.name.value,
        path: form.elements.path.value,
        description: form.elements.description.value,
        active: activeOn
      }})
    }}).then(function(r) {{ return r.json().then(function(j) {{ return {{ok: r.ok, data: j}}; }}); }})
    .then(function(res) {{
      if (res.ok) {{ msg.textContent = 'Saved!'; msg.className = 'msg ok'; }}
      else {{ msg.textContent = res.data.error || 'Failed'; msg.className = 'msg err'; }}
      msg.style.display = 'block';
    }});
  }});

  document.getElementById('remove-btn').addEventListener('click', function() {{
    if (!confirm('Remove this project from the registry? Tickets and files will not be deleted.')) return;
    fetch('/api/projects/{pid}', {{ method: 'DELETE' }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.ok) window.location.href = '/';
      else alert(data.error || 'Failed to remove');
    }});
  }});
}})();
</script>
</body>
</html>'''
```

- [ ] **Step 2: Wire into `do_GET`**

In the project-scoped section of `do_GET`, add before the dashboard serving block:

```python
    # Project settings page
    if remainder == "/settings":
        html = _render_project_settings(proj, SERVER_PORT)
        self._send_html(html)
        return
```

- [ ] **Step 3: Manual test**

Open `http://localhost:8787/ticket-takeaway/settings` — should show form.

- [ ] **Step 4: Commit**

```bash
git add src/serve.py
git commit -m "feat: project settings page at /{project-id}/settings"
```

---

### Task 9: Project Switcher Dropdown (`generate.py`)

**Files:**
- Modify: `src/generate.py:628` (CSS after `.project-name`)
- Modify: `src/generate.py` (JS block after line ~2636)
- Modify: `src/serve.py` (meta tag injection)

- [ ] **Step 1: Add meta tag injection for projects list**

In `src/serve.py`, update the meta tag injection in `do_GET` where the dashboard HTML is served. Replace the `edit-api` injection block:

```python
    if '<meta name="edit-api"' not in html:
        idx = html.find('<meta name="gen-ts"')
        if idx != -1:
            with _PROJECTS_CACHE_LOCK:
                proj_list = [{"id": p["id"], "name": p.get("name", p["id"])} for p in _PROJECTS_CACHE.values()]
            projects_json = json.dumps(proj_list)
            injection = (
                f'<meta name="edit-api" content="http://localhost:{SERVER_PORT}/{_safe_attr(proj["id"])}/api">\n'
                f'<meta name="current-project" content="{_safe_attr(proj["id"])}">\n'
                f"<meta name=\"projects-list\" content='{_safe_attr(projects_json)}'>\n"
            )
            html = html[:idx] + injection + html[idx:]
```

- [ ] **Step 2: Add switcher CSS to `generate.py`**

In `src/generate.py`, after line 628 (`.project-name` rule), add:

```css
.proj-switcher {{ position: relative; display: inline-flex; align-items: center; }}
.proj-switcher-btn {{
  display: inline-flex; align-items: center; gap: 5px; font-size: 13px; font-weight: 600;
  color: var(--text-primary); background: none; border: none; padding: 2px 4px;
  border-radius: 4px; cursor: pointer; transition: background 0.15s;
  font-family: var(--font-sans); line-height: 1.4;
}}
.proj-switcher-btn:hover, .proj-switcher-btn[aria-expanded="true"] {{ background: var(--bg-hover); }}
.proj-switcher-btn:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.proj-switcher-chevron {{ width: 10px; height: 10px; opacity: 0.5; flex-shrink: 0; transition: transform 0.15s; }}
.proj-switcher-btn[aria-expanded="true"] .proj-switcher-chevron {{ transform: rotate(180deg); }}
.proj-switcher-menu {{
  position: absolute; top: calc(100% + 6px); left: 0; z-index: 500; min-width: 200px;
  background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.5); padding: 4px 0; display: none;
}}
.proj-switcher-menu.open {{ display: block; }}
.proj-switcher-item {{
  display: block; padding: 7px 14px; font-size: 13px; color: var(--text-primary);
  text-decoration: none; white-space: nowrap; cursor: pointer; transition: background 0.1s;
}}
.proj-switcher-item:hover, .proj-switcher-item:focus-visible {{ background: var(--bg-hover); outline: none; }}
.proj-switcher-item.current {{ color: var(--accent); font-weight: 600; }}
.proj-switcher-item.current::after {{ content: " \\2713"; font-size: 11px; }}
.proj-switcher-divider {{ height: 1px; background: var(--border-subtle); margin: 4px 0; }}
.proj-switcher-footer-item {{
  display: block; padding: 6px 14px; font-size: 11px; color: var(--text-tertiary);
  text-decoration: none; white-space: nowrap; transition: background 0.1s, color 0.1s;
}}
.proj-switcher-footer-item:hover, .proj-switcher-footer-item:focus-visible {{ background: var(--bg-hover); color: var(--text-secondary); outline: none; }}
```

- [ ] **Step 3: Add switcher JS to `generate.py`**

Add the complete project-switcher `<script>` block from the frontend architect's output (the IIFE that reads `projects-list` and `current-project` meta tags, replaces `.project-name` with a dropdown, handles keyboard navigation, outside-click closing, and `data-testid` attributes). Insert it as a new `<script>` tag in the HTML template string — find the right insertion point after the existing live-update polling script.

The JS is a progressive enhancement — when meta tags are absent (static file), nothing happens.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_tdd_*.py -v`

Expected: All pass.

- [ ] **Step 5: Regenerate dashboard and manual test**

```bash
python3 src/generate.py --project ticket-takeaway
python3 src/serve.py &
# Open http://localhost:8787/ticket-takeaway
# Click project name in header → dropdown should appear
# Verify Ctrl+click opens in new tab
# Verify Escape closes dropdown
kill %1
```

- [ ] **Step 6: Commit**

```bash
git add src/serve.py src/generate.py
git commit -m "feat: project switcher dropdown in dashboard header"
```

---

### Task 10: CLI `register` / `unregister` Commands (`tickets-cli.py`)

**Files:**
- Modify: `src/tickets-cli.py` — add subcommands and command handlers

- [ ] **Step 1: Write failing test**

Append to `tests/test_tdd_routing.py`:

```python
import tempfile
import shutil


def test_validate_project_id_reserved():
    """Reserved IDs like 'api' and 'settings' should be rejected."""
    from serve import _RESERVED_IDS
    assert "api" in _RESERVED_IDS
    assert "settings" in _RESERVED_IDS


def test_validate_project_registration_bad_path():
    """Registration with non-existent path should fail."""
    from serve import _validate_project_registration
    error = _validate_project_registration({"id": "test-proj", "path": "/nonexistent/path"})
    assert error is not None
    assert "does not exist" in error


def test_validate_project_registration_good():
    """Registration with valid ID and path should succeed."""
    from serve import _validate_project_registration, _PROJECTS_CACHE, _PROJECTS_CACHE_LOCK
    with _PROJECTS_CACHE_LOCK:
        _PROJECTS_CACHE.clear()  # ensure no dupe
    tmp = tempfile.mkdtemp(dir=str(Path.home()))
    try:
        error = _validate_project_registration({"id": "test-proj", "path": tmp})
        assert error is None
    finally:
        shutil.rmtree(tmp)
```

- [ ] **Step 2: Run tests to verify they pass** (validation was added in Task 7)

Run: `python3 -m pytest tests/test_tdd_routing.py -k "test_validate" -v`

Expected: PASS (the validation function already exists from Task 7).

- [ ] **Step 3: Add CLI subcommands**

In `src/tickets-cli.py`, add command handlers:

```python
def cmd_register(args):
    """Register a new project in the registry."""
    import re as _re
    _SLUG_RE = _re.compile(r'^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$')

    pid = args.id
    if not _SLUG_RE.match(pid):
        print("Error: ID must be 2-40 chars, lowercase alphanumeric and hyphens", file=sys.stderr)
        sys.exit(1)

    path = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(path):
        print(f"Error: Path does not exist: {path}", file=sys.stderr)
        sys.exit(1)

    # Load registry
    if REGISTRY_PATH.exists():
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            registry = json.load(f)
    else:
        registry = {"projects": []}

    # Check for duplicate
    for p in registry["projects"]:
        if p["id"] == pid:
            print(f"Error: Project '{pid}' already exists in registry", file=sys.stderr)
            sys.exit(1)

    new_project = {
        "id": pid,
        "name": args.name or pid,
        "path": path,
        "description": args.description or "",
        "active": True,
    }
    registry["projects"].append(new_project)

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)

    # Init DB
    conn = get_db()
    init_db(conn)
    conn.close()

    print(f"Registered: {pid} → {path}")

    # Offer to seed if PRODUCT_BACKLOG.md exists
    backlog = Path(path) / "PRODUCT_BACKLOG.md"
    if backlog.exists():
        print(f"Found {backlog}. Run 'tickets-cli.py seed --project {pid}' to import existing tickets.")


def cmd_unregister(args):
    """Deactivate a project in the registry."""
    if not REGISTRY_PATH.exists():
        print("Registry not found.", file=sys.stderr)
        sys.exit(1)

    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        registry = json.load(f)

    found = False
    for p in registry["projects"]:
        if p["id"] == args.id:
            p["active"] = False
            found = True
            break

    if not found:
        print(f"Project '{args.id}' not found in registry.", file=sys.stderr)
        sys.exit(1)

    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)

    print(f"Deactivated: {args.id}")

    if args.delete_tickets:
        conn = get_db()
        init_db(conn)
        conn.execute("DELETE FROM acceptance_criteria WHERE project_id = ?", (args.id,))
        conn.execute("DELETE FROM depends WHERE project_id = ?", (args.id,))
        conn.execute("DELETE FROM readiness_flags WHERE project_id = ?", (args.id,))
        conn.execute("DELETE FROM scheduled_events WHERE project_id = ?", (args.id,))
        conn.execute("DELETE FROM tickets WHERE project_id = ?", (args.id,))
        conn.execute("DELETE FROM _sync_state WHERE project_id = ?", (args.id,))
        conn.commit()
        conn.close()
        print(f"Deleted all tickets for {args.id} from database.")
```

- [ ] **Step 4: Register subparsers**

In the argparse section (around line 1227), add:

```python
    p_reg = sub.add_parser("register", help="Register a new project")
    p_reg.add_argument("--id", required=True, help="Project ID (lowercase, hyphens OK)")
    p_reg.add_argument("--name", help="Display name (default: same as ID)")
    p_reg.add_argument("--path", required=True, help="Path to project root")
    p_reg.add_argument("--description", help="Project description")

    p_unreg = sub.add_parser("unregister", help="Deactivate a project")
    p_unreg.add_argument("id", help="Project ID to deactivate")
    p_unreg.add_argument("--delete-tickets", action="store_true", help="Also delete tickets from DB (destructive)")
```

And add to the commands dict:

```python
    commands = {
        ...
        "register": cmd_register,
        "unregister": cmd_unregister,
    }
```

- [ ] **Step 5: Test CLI**

```bash
mkdir -p /tmp/test-project
python3 src/tickets-cli.py register --id test-cli --name "Test CLI" --path /tmp/test-project
python3 src/tickets-cli.py list --project test-cli
python3 src/tickets-cli.py unregister test-cli
rm -rf /tmp/test-project
```

- [ ] **Step 6: Commit**

```bash
git add src/tickets-cli.py
git commit -m "feat: register/unregister CLI commands for project management"
```

---

### Task 11: Deploy and End-to-End Verification

**Files:**
- Deploy from `src/` to `~/.claude/ticket-takeaway/`

- [ ] **Step 1: Deploy updated files**

```bash
cp src/serve.py ~/.claude/ticket-takeaway/serve.py
cp src/generate.py ~/.claude/ticket-takeaway/generate.py
cp src/tickets-cli.py ~/.claude/ticket-takeaway/tickets-cli.py
cp src/constants.py ~/.claude/ticket-takeaway/constants.py
cp src/db.py ~/.claude/ticket-takeaway/db.py
cp src/actions.py ~/.claude/ticket-takeaway/actions.py
```

- [ ] **Step 2: Regenerate dashboards for all projects**

```bash
python3 ~/.claude/ticket-takeaway/generate.py --all
```

- [ ] **Step 3: Start server and run full E2E verification**

```bash
python3 ~/.claude/ticket-takeaway/serve.py --port 8787 &
SERVER_PID=$!
```

Manual checklist:
1. Open `http://localhost:8787/` — project picker shows all projects with ticket counts
2. Click a project card → navigates to `/{project-id}` with kanban
3. Ctrl+click another project → opens in new tab, independent dashboard
4. Click project name in header → dropdown with all projects, "All Projects", "Settings"
5. Navigate to `/{project-id}/settings` → form with name, path, description
6. Verify API scoping: `curl http://localhost:8787/ticket-takeaway/api/tickets`
7. Verify global API: `curl http://localhost:8787/api/projects`
8. Test backward compat: `python3 ~/.claude/ticket-takeaway/serve.py --project ticket-takeaway &` then `curl -v http://localhost:8787/api/tickets` → 301 redirect

```bash
kill $SERVER_PID
```

- [ ] **Step 4: Run all automated tests**

```bash
python3 -m pytest tests/test_tdd_*.py tests/test_smoke_multiproject.py -v
```

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: multi-project support — picker, settings, scoped APIs, dropdown switcher"
```
