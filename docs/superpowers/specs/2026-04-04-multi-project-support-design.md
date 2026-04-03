# Multi-Project Support — Design Spec

## Context

Ticket Takeaway currently locks `serve.py` to a single project per server instance via a global `SERVER_PROJECT_ID`. The DB and registry already support multiple projects (`project_id` columns, `registry.json`), but the HTTP server, dashboard HTML, and background threads are all single-project. Users want to have multiple project dashboards open in separate browser tabs simultaneously, served by one server process.

**Goals:**
- One `serve.py` instance serves all registered projects
- Each project lives at `/{project-id}` with fully scoped API URLs
- Multiple browser tabs work independently (no shared state)
- Lightweight enough to run several projects without performance issues
- Global functions (project management) are not scoped under a project
- New users can discover and register projects via a settings UI

---

## URL Architecture

### Project-scoped routes (under `/{project-id}/`)

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/{pid}` | Kanban dashboard HTML for project |
| GET | `/{pid}/settings` | Project settings page |
| GET | `/{pid}/api/tickets` | List all tickets |
| GET | `/{pid}/api/tickets/{tid}` | Single ticket JSON |
| PUT | `/{pid}/api/tickets/{tid}` | Update ticket fields / criteria |
| PUT | `/{pid}/api/tickets/{tid}/readiness/{flag}` | Update readiness content |
| POST | `/{pid}/api/tickets/{tid}/move` | Move ticket between sections |
| POST | `/{pid}/api/tickets/{tid}/enrich` | AI field enrichment |
| POST | `/{pid}/api/tickets/{tid}/gate-check` | Pre-move validation |
| POST | `/{pid}/api/tickets/{tid}/assess/{cat}` | Per-category assessment |
| POST | `/{pid}/api/tickets/{tid}/readiness/{flag}` | Toggle readiness flag |
| POST | `/{pid}/api/tickets/{tid}/accept` | Accept ticket |
| POST | `/{pid}/api/tickets` | Create ticket |
| DELETE | `/{pid}/api/tickets/{tid}` | Delete ticket |

### Global routes (not project-scoped)

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/` | Project picker page |
| GET | `/api/projects` | List all registered projects (with ticket summary counts) |
| POST | `/api/projects` | Register a new project |
| PUT | `/api/projects/{pid}` | Update project settings (name, path, description, active) |
| DELETE | `/api/projects/{pid}` | Deactivate/remove project from registry |
| OPTIONS | `*` | CORS preflight (unchanged) |

### Backward compatibility

When `--project <id>` is passed to `serve.py`, legacy routes without a project prefix (`/api/tickets`, `/api/tickets/{id}`, etc.) are rewritten to `/{project-id}/api/...` via 301 redirect. This preserves old bookmarks and any external integrations. The `--project` flag is optional — without it, all projects are served.

---

## Changes by File

### 1. `src/serve.py` — Core routing refactor

**Remove global state:**
- Delete `SERVER_PROJECT_ID` global variable
- Delete `_get_project()` helper
- Add `_resolve_project_from_path(url_path) -> (project_id, remainder)` that strips the `/{project-id}` prefix and returns both the project dict and the remaining path

**URL parsing — new helper:**
```python
def _resolve_project_from_path(path: str) -> tuple[dict | None, str]:
    """Extract project from URL prefix. Returns (project_dict, remaining_path).
    
    /goodform/api/tickets  →  (goodform_project, "/api/tickets")
    /settings              →  (None, "/settings")
    /api/projects          →  (None, "/api/projects")
    /                      →  (None, "/")
    """
```

Logic: load registry once at startup into a module-level dict (`_PROJECTS_CACHE`). On each request, check if the first path segment matches a known project ID. If yes, resolve and return remainder. If no, return `(None, original_path)` for global routes.

**Registry cache:**
- `_PROJECTS_CACHE: dict[str, dict] = {}` — populated at startup, refreshed on registry writes (POST/PUT/DELETE to `/api/projects`)
- `_refresh_projects_cache()` — re-reads `registry.json`, updates the dict
- This avoids reading the registry file on every HTTP request

**Route handler refactor (`do_GET`, `do_PUT`, `do_POST`, `do_DELETE`):**

Each handler starts with:
```python
path = urlparse(self.path).path
proj, remainder = _resolve_project_from_path(path)
```

Then routes on `remainder` instead of `path`. The `proj` dict (or `proj["id"]`) replaces every `_get_project()` call. If `proj` is `None`, the request is a global route.

**Pattern change for route matching:**
- Before: `re.match(r"^/api/tickets/([A-Za-z0-9_-]+)$", path)`
- After: `re.match(r"^/api/tickets/([A-Za-z0-9_-]+)$", remainder)` (same regex, applied to `remainder`)

This means the regex patterns themselves don't change — only the input string does. Minimal diff.

**New `do_GET` routes:**

- `remainder == "/"` and `proj is None` → serve project picker HTML (inline-rendered, see UI section)
- `remainder == "/"` or `remainder == ""` and `proj is not None` → serve project dashboard HTML (existing logic, but reads from `proj["path"]/docs/sdlc-dashboard.html`)
- `remainder == "/settings"` and `proj is not None` → serve project settings HTML (inline-rendered)
- `remainder == "/api/projects"` and `proj is None` → return JSON list of all projects with ticket summary counts

**New `do_POST`/`do_PUT`/`do_DELETE` routes for `/api/projects`:**

- `POST /api/projects` — body: `{id, name, path, description?}`. Validates `id` is slug-safe (`[a-z0-9-]+`), `path` exists on disk. Appends to `registry.json`, refreshes cache, returns created project.
- `PUT /api/projects/{pid}` — body: partial update of `{name?, path?, description?, active?}`. Updates `registry.json`, refreshes cache.
- `DELETE /api/projects/{pid}` — sets `active: false` in registry (soft delete). Does NOT delete tickets from DB or files from disk.

**Meta tag injection change:**
- Before: `content="http://localhost:{PORT}/api"`
- After: `content="http://localhost:{PORT}/{project_id}/api"`
- This single change makes all frontend JS fetch calls project-scoped automatically (the JS already uses `EDIT_API + '/tickets/...'`)

**Project list injection (new):**
Inject a second meta tag alongside `edit-api`:
```html
<meta name="projects-list" content='[{"id":"goodform","name":"GoodForm"},{"id":"ticket-takeaway","name":"Ticket Takeaway"}]'>
```
The dashboard JS reads this to build the project switcher dropdown. Lightweight — just id+name pairs, no ticket data.

**Background threads — multi-project:**

Currently `_start_external_edit_watcher(project)` and `_start_scheduled_event_poller(project)` each take a single project and spin up one thread.

Change: start one thread of each type that iterates over ALL active projects per poll cycle:

```python
def _start_external_edit_watcher(interval: float = 5.0):
    def _poll():
        while True:
            time.sleep(interval)
            for proj in _PROJECTS_CACHE.values():
                try:
                    with _db_lock:
                        conn = get_db()
                        init_db(conn)
                        changed = cli.detect_external_edits(conn, proj)
                        if changed:
                            cli.regenerate_dashboard(proj)
                        conn.close()
                except Exception as exc:
                    print(f"[watcher] Error for {proj.get('id')}: {exc}")
    threading.Thread(target=_poll, daemon=True, name="md-edit-watcher").start()
```

Same pattern for scheduled event poller — iterate all projects, query events filtered by each `project_id`. One thread, N projects. Keeps thread count constant regardless of project count.

**`--project` backward compatibility:**
When `--project <id>` is passed:
- Server still serves all projects at their `/{id}` URLs
- Legacy un-prefixed routes (`/api/tickets`, etc.) redirect to `/{id}/api/tickets` via 301
- Root `/` redirects to `/{id}` instead of showing the picker
- This lets existing scripts and bookmarks work without changes

**CORS update:**
Change `Access-Control-Allow-Origin` from `http://localhost:{PORT}` to `*` for same-origin requests (all routes are on the same host:port, so the browser won't send CORS headers anyway for same-origin fetches). Alternatively, keep the current origin but it already covers all project routes since they share the same origin.

### 2. `src/generate.py` — Per-project isolation

**Stop aggregating tickets across projects.**

Change `generate_html(projects: list[Project])` to `generate_html(project: Project)` (singular). The function already uses `primary = projects[0]` for everything meaningful — the aggregation across projects was unused in practice since serve.py always passed a single project.

In `main()`, change the generation loop:
```python
# Before:
html = generate_html(projects)
for proj in projects:
    write html to proj.path/docs/sdlc-dashboard.html

# After:
for proj in projects:
    html = generate_html(proj)
    write html to proj.path/docs/sdlc-dashboard.html
```

Each project gets its own isolated HTML file with only its own tickets. No cross-project data leakage.

### 3. `src/tickets-cli.py` — New subcommands

**`register` subcommand:**
```bash
python3 tickets-cli.py register --id goodform --name "GoodForm" --path ~/projects/GoodForm [--description "..."]
```
- Validates: `id` is slug-safe, `path` exists, `id` not already in registry
- Appends to `registry.json` with `"active": true`
- Runs `init_db()` to ensure tables exist
- Auto-syncs: if `PRODUCT_BACKLOG.md` exists at the path, offers to seed

**`unregister` subcommand:**
```bash
python3 tickets-cli.py unregister goodform [--delete-tickets]
```
- Sets `active: false` in registry (default)
- With `--delete-tickets`: also removes rows from DB where `project_id = ?` (destructive, requires confirmation)

### 4. Dashboard HTML — Project switcher dropdown

**Injected by serve.py, built by JS.**

The dashboard JS (in `generate.py`'s inline `<script>`) gains a small block that:

1. Reads `<meta name="projects-list">` content
2. Reads `<meta name="current-project">` content (also injected by serve.py)
3. Replaces the static `.project-name` span in the header with a clickable dropdown
4. Each dropdown item is an `<a href="/{project-id}">` — standard links, Ctrl+click opens new tab naturally
5. Dropdown footer has "All Projects" (`<a href="/">`) and "Settings" (`<a href="/{current}/settings">`) links

**No SPA routing.** Clicking a project navigates the full page. Each tab is completely independent — no shared JS state, no coordination between tabs.

### 5. Project picker page (`/`)

**Server-rendered by serve.py** — a self-contained HTML string built in Python, same dark theme as the dashboard. Not generated by `generate.py`.

Content:
- Header: "Ticket Takeaway" title
- Grid of project cards, each showing: name, path (abbreviated with `~`), ticket summary counts (WIP/Backlog/Review — fetched from DB via a quick `SELECT section, COUNT(*) ... GROUP BY section` query)
- Each card is an `<a href="/{project-id}">` — clickable, Ctrl+click for new tab
- "Add Project" dashed card — opens an inline form (name, path, ID auto-generated from name) that POSTs to `/api/projects`
- Minimal JS — just the add-project form toggle and submit. No polling, no live updates.

**Performance:** The project picker queries the DB once on load for summary counts. No generate.py call, no file I/O beyond reading `registry.json` (cached) and one DB query. Sub-millisecond for typical registry sizes (<20 projects).

### 6. Project settings page (`/{project-id}/settings`)

**Server-rendered by serve.py** — same approach as the picker page.

Content:
- Back link to `/{project-id}`
- Form fields: Name, Path, Description (editable), ID (read-only), Active toggle
- Save button → `PUT /api/projects/{id}`
- Danger zone: "Remove Project" button → `DELETE /api/projects/{id}` with confirmation dialog
- Removal explanation: "Removes from registry only. Does not delete files, tickets, or database entries."

**No project dropdown in settings header** — just a back arrow + project name. Keeps it simple.

---

## Data Flow

### Request lifecycle (project-scoped)

```
Browser tab: http://localhost:8787/goodform
    ↓
serve.py: _resolve_project_from_path("/goodform") → (goodform_proj, "/")
    ↓
Read /home/user/projects/GoodForm/docs/sdlc-dashboard.html
    ↓
Inject <meta name="edit-api" content="http://localhost:8787/goodform/api">
Inject <meta name="projects-list" content='[...]'>
Inject <meta name="current-project" content="goodform">
    ↓
Browser JS: EDIT_API = "http://localhost:8787/goodform/api"
    ↓
fetch(EDIT_API + '/tickets') → GET /goodform/api/tickets
    ↓
serve.py: _resolve_project_from_path("/goodform/api/tickets") → (goodform_proj, "/api/tickets")
    ↓
DB query: SELECT ... WHERE project_id = 'goodform'
```

### Background thread lifecycle

```
One watcher thread, one poller thread (total: 2 threads, regardless of project count)
    ↓
Every 5s (watcher) / 30s (poller):
    for project in _PROJECTS_CACHE.values():
        check for changes / execute scheduled events
        sync markdown + regen dashboard if needed
```

---

## Performance & Resilience

**Stateless request handling:** No global mutable state. Project resolved from URL on every request. Thread-safe by design — the `_db_lock` is already reentrant and scoped to individual operations.

**Registry cache:** Read once at startup, refreshed only on writes to `/api/projects`. Avoids file I/O on every request. Cache is a simple dict — O(1) project lookups by ID.

**Constant thread count:** Two background threads total, regardless of project count. Each iterates the project list. A project error is caught and logged without affecting other projects.

**Tab independence:** Each browser tab holds its own `EDIT_API` URL pointing to its project. No shared state between tabs. Opening 5 projects = 5 independent dashboard instances, each polling their own `/{pid}/api/tickets` endpoint.

**HTML generation:** `generate.py` already generates per-project files. The only change is stopping the cross-project aggregation, which actually reduces work per generation.

**Graceful degradation:** If a project's path becomes invalid (directory deleted), the dashboard returns 404 with a helpful message. Other projects continue working. The picker page shows the project but grays it out with a "path not found" indicator.

---

## Files to Modify

| File | Change Scope |
|------|-------------|
| `src/serve.py` | Major — routing refactor, new pages, new API endpoints, background thread changes |
| `src/generate.py` | Minor — change `generate_html` from multi-project aggregation to single-project |
| `src/tickets-cli.py` | Minor — add `register` and `unregister` subcommands |
| `src/constants.py` | None expected |
| `src/db.py` | None expected |
| `src/actions.py` | None expected |

---

## Verification Plan

### Manual testing

1. **Start server:** `python3 serve.py` (no `--project` flag)
2. **Project picker:** Open `http://localhost:8787/` — should show all registered projects as cards with ticket counts
3. **Project dashboard:** Click a project card → navigates to `/{project-id}` showing that project's kanban
4. **Multi-tab:** Ctrl+click a different project → opens in new tab. Both tabs work independently.
5. **Project switcher dropdown:** In the dashboard header, click project name → dropdown shows all projects. Click another → navigates.
6. **API scoping:** In browser devtools, verify all fetch calls go to `/{project-id}/api/...`
7. **Settings page:** Navigate to `/{project-id}/settings` → edit name → save → verify change in picker and dropdown
8. **Add project:** On picker page, click "+ Add Project" → fill form → submit → new project appears
9. **Remove project:** On settings page, click "Remove Project" → confirm → project disappears from picker
10. **Backward compat:** Start with `--project goodform` → verify `/api/tickets` redirects to `/goodform/api/tickets`

### Automated testing

- **TDD tests:** Add tests for `_resolve_project_from_path()` — various URL patterns, edge cases (unknown project, empty path, trailing slashes)
- **Smoke tests:** API endpoints with project prefix return correct data. Global endpoints return project list.
- **E2E tests:** Project picker → click → dashboard loads. Switcher dropdown → navigate → correct project shown.

### CLI testing

```bash
python3 tickets-cli.py register --id test-proj --name "Test" --path /tmp/test-proj
python3 tickets-cli.py list --project test-proj
python3 tickets-cli.py unregister test-proj
```
