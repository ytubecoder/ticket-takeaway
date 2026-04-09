# Seek Feature — Technical Implementation Spec

## 0. Pre-Seek Fixes (required before Seek can work correctly)

These are bugs/gaps in existing code that Seek depends on. Fix them first.

### 0a. Exclude drafts from `sync_to_markdown()` — `src/tickets-cli.py:473`

**Problem:** `sync_to_markdown()` writes ALL tickets to PRODUCT_BACKLOG.md, including drafts. Drafts should NOT appear in the markdown file — they're unconfirmed noise. The user confirmed: "draft tickets don't go into md files."

**Fix:** Add `AND draft = 0` to the query:
```python
# Line 473 in sync_to_markdown()
tickets = conn.execute(
    "SELECT * FROM tickets WHERE project_id = ? AND section = ? AND draft = 0 ORDER BY sort_order ASC",
    (project_id, section)
).fetchall()
```

### 0b. Fix draft banner copy — `src/generate.py:4384`

**Problem:** Draft banner says "This ticket was auto-generated from a feedback session" — hardcoded to feedbacks. Seek drafts would show misleading copy.

**Fix:** Make the message dynamic based on description's `Source:` prefix:
```javascript
// Line 4384
var desc = (card && card.dataset.desc) || '';
if (desc.startsWith('Source: ')) {
  var srcType = desc.split(' ')[1];
  var labels = {code_todo:'a code comment',md_task:'a markdown task',readme_todo:'a README item',changelog:'a changelog entry',github_issue:'a GitHub issue'};
  msg.textContent = 'This ticket was auto-generated from ' + (labels[srcType] || 'project files') + '.';
} else {
  msg.textContent = 'This ticket was auto-generated from a feedback session.';
}
```

### 0c. Fix draft confirm to use `confirm_ticket()` — `src/serve.py`

**Problem:** The confirm button sends `PUT /api/tickets/{id}` with `{draft: false}`, which goes through the generic update path. The `confirm_ticket()` helper in `actions.py:392` also clears `source_attachment_id` and is the canonical path.

**Fix:** Add a dedicated route `POST /{project}/api/tickets/{id}/confirm` that calls `confirm_ticket()`, or modify the PUT handler to detect `draft: false` and route to `confirm_ticket()`.

### 0d. Fix drafts toggle to persist via localStorage — `src/generate.py:4322`

**Problem:** `showDrafts` is always initialized to `false`. The Seek button sets `localStorage('tt-show-drafts', '1')` before reload, but the toggle script never reads it.

**Fix:**
```javascript
// Line 4322
var showDrafts = localStorage.getItem('tt-show-drafts') === '1';
if (showDrafts && draftsBtn) draftsBtn.classList.add('active');
```
And update the toggle click handler to persist:
```javascript
draftsBtn.addEventListener('click', function() {
  showDrafts = !showDrafts;
  localStorage.setItem('tt-show-drafts', showDrafts ? '1' : '0');
  // ... existing visibility logic
});
```

---

## 1. Core Module: `src/seek.py`

### 1.1 Data Model

```python
@dataclass
class DiscoveredItem:
    title: str           # Cleaned text, max ~120 chars
    source_type: str     # "md_task" | "readme_todo" | "code_todo" | "changelog" | "github_issue"
    source_file: str     # Relative path from project root
    source_line: int     # Line number (or issue number for GitHub)
    raw_text: str        # Full original text → becomes ticket description
    priority: str        # "medium" default, "high" for FIXME/HACK
    section: str         # Always "Ideas" for v1
```

### 1.2 Scanner Functions

Each scanner is a pure function: `(project_path: str) -> list[DiscoveredItem]`

#### `scan_md_tasks(project_path)`
- Glob `*.md` in project root (non-recursive to avoid node_modules etc.)
- Skip `README.md` (has its own scanner), `PRODUCT_BACKLOG.md`, `PRODUCT_SPECIFICATION.md`, `CHANGELOG.md`
- Match: `^\s*- \[ \]\s+(.+)$` (unchecked markdown tasks only)
- Skip checked items `- [x]`
- Title = captured group, stripped

#### `scan_readme_todos(project_path)`
- Read `README.md` if exists
- Find headers (any `#` level) containing: `TODO`, `Roadmap`, `Planned`, `Future` (case-insensitive)
- Extract bullet items (`- ...` or `* ...`) until next same-or-higher-level header
- Title = bullet text (strip leading `- ` or `* `)

#### `scan_code_todos(project_path)`
- Walk project directory, include: `.py .js .ts .jsx .tsx .go .rs .rb .java .c .cpp .h .css .lua`
- Skip dirs: `node_modules/`, `.git/`, `__pycache__/`, `venv/`, `.venv/`, `dist/`, `build/`, `.next/`, `.artifacts/`
- Match: `(?:TODO|FIXME|HACK)\s*[:—-]\s*(.+)` (case-insensitive)
- Strip comment prefixes (`#`, `//`, `/*`, `--`, `;;`)
- FIXME/HACK → `priority="high"`
- TODO → `priority="medium"`

#### `scan_changelog_unreleased(project_path)`
- Read `CHANGELOG.md` if exists
- Find header containing "Unreleased" (case-insensitive)
- Extract bullet items until next header of same or higher level
- Title = bullet text

#### `scan_github_issues(project_path)`
- Run: `subprocess.run(["gh", "issue", "list", "--json", "number,title,body,labels", "--limit", "50", "--state", "open"], capture_output=True, timeout=10, cwd=project_path)`
- On any failure (FileNotFoundError, timeout, non-zero exit) → return `[]` silently
- Parse JSON output, create DiscoveredItem per issue
- `source_line` = issue number, `source_file` = "github"
- `raw_text` = issue body (truncated to 500 chars)

### 1.3 Orchestrator

```python
SCANNERS = {
    "md_task": scan_md_tasks,
    "readme_todo": scan_readme_todos,
    "code_todo": scan_code_todos,
    "changelog": scan_changelog_unreleased,
    "github_issue": scan_github_issues,
}

def discover(project_path: str, sources: list[str] | None = None) -> list[DiscoveredItem]:
    """Run scanners and return combined results."""
    to_run = {k: v for k, v in SCANNERS.items() if sources is None or k in sources}
    results = []
    for name, scanner in to_run.items():
        try:
            results.extend(scanner(project_path))
        except Exception:
            pass  # Individual scanner failures don't block others
    return results
```

### 1.4 Deduplication

```python
def _normalize(title: str) -> str:
    return title.strip().lower()

def _parse_source_from_desc(description: str) -> tuple[str, str, int] | None:
    """Extract (source_type, source_file, source_line) from 'Source: type @ file:line' prefix."""
    m = re.match(r'^Source:\s+(\S+)\s+@\s+(.+):(\d+)', description)
    if m:
        return (m.group(1), m.group(2), int(m.group(3)))
    return None

def deduplicate(
    items: list[DiscoveredItem],
    existing_titles: list[str],
    existing_draft_descriptions: list[str],
) -> list[DiscoveredItem]:
    """Remove items that match existing tickets or previous seek drafts."""
    existing_norm = {_normalize(t) for t in existing_titles}
    
    # Parse source keys from existing draft descriptions for source-level dedup
    existing_sources = set()
    for desc in existing_draft_descriptions:
        parsed = _parse_source_from_desc(desc)
        if parsed:
            existing_sources.add(parsed)
    
    seen_titles = set()
    result = []
    for item in items:
        norm = _normalize(item.title)
        source_key = (item.source_type, item.source_file, item.source_line)
        
        if norm in existing_norm:
            continue  # Matches existing real ticket
        if norm in seen_titles:
            continue  # Duplicate within this scan
        if source_key in existing_sources:
            continue  # Same source already has a draft
        
        seen_titles.add(norm)
        result.append(item)
    return result
```

### 1.5 Ingestion

```python
def ingest(conn, project_id: str, items: list[DiscoveredItem]) -> list[str]:
    """Create draft tickets from discovered items. Returns list of new ticket IDs."""
    created = []
    for item in items:
        description = f"Source: {item.source_type} @ {item.source_file}:{item.source_line}\n\n{item.raw_text}"
        tid = add_ticket(
            conn, project_id,
            title=item.title[:200],  # Truncate long titles
            section=item.section,
            priority=item.priority,
            draft=True,
        )
        # Update description separately (add_ticket has limited params)
        conn.execute(
            "UPDATE tickets SET description = ? WHERE id = ? AND project_id = ?",
            (description, tid, project_id)
        )
        created.append(tid)
    conn.commit()
    return created
```

### 1.6 Top-Level Entry Point

```python
def run_seek(
    conn,
    project_id: str,
    project_path: str,
    sources: list[str] | None = None,
) -> dict:
    """Discover ticket-like items and create drafts. Returns summary dict."""
    items = discover(project_path, sources=sources)
    
    # Get existing ticket titles for dedup
    rows = conn.execute(
        "SELECT title FROM tickets WHERE project_id = ?", (project_id,)
    ).fetchall()
    existing_titles = [r["title"] for r in rows]
    
    # Get existing draft descriptions for source-level dedup
    draft_rows = conn.execute(
        "SELECT description FROM tickets WHERE project_id = ? AND draft = 1", (project_id,)
    ).fetchall()
    existing_draft_descs = [r["description"] for r in draft_rows]
    
    unique = deduplicate(items, existing_titles, existing_draft_descs)
    created_ids = ingest(conn, project_id, unique) if unique else []
    
    return {
        "discovered": len(items),
        "created": len(created_ids),
        "skipped_duplicates": len(items) - len(unique),
        "tickets": created_ids,
    }
```

---

## 2. CLI: `src/tickets-cli.py`

### Subcommand Parser (add near line 1566)

```python
p_seek = sub.add_parser("seek", help="Discover ticket-like items in project files and create drafts")
p_seek.add_argument("project", help="Project ID")
p_seek.add_argument("--sources", help="Comma-separated source types: md_task,readme_todo,code_todo,changelog,github_issue")
```

### Dispatch Table (add to `commands` dict at line 1571)

```python
commands = {
    ...
    "seek": cmd_seek,   # <-- ADD THIS LINE
}
```

**Note:** tickets-cli.py dispatches via `commands[args.command](args)`, NOT `args.func`. The parser entry alone is not enough — the command dict entry is required.

### Handler

```python
def cmd_seek(args):
    projects = load_registry()
    target = resolve_project_id(projects, args.project)
    if len(target) != 1:
        print("Error: seek requires a single project", file=sys.stderr)
        sys.exit(1)
    proj = target[0]
    project_path = os.path.expanduser(proj.get("path", ""))
    sources = args.sources.split(",") if args.sources else None
    
    conn = get_db()
    init_db(conn)
    
    # Ingest any pending markdown edits BEFORE mutating DB
    ingest_markdown(conn, proj)
    
    from seek import run_seek
    result = run_seek(conn, proj["id"], project_path, sources=sources)
    
    sync_to_markdown(conn, proj)
    regenerate_dashboard(proj)
    conn.close()
    
    print(f"Discovered: {result['discovered']} items")
    print(f"Created: {result['created']} draft ticket(s)")
    print(f"Skipped: {result['skipped_duplicates']} duplicate(s)")
    if result['tickets']:
        print("New drafts:")
        for tid in result['tickets']:
            print(f"  {tid}")
```

---

## 3. API: `src/serve.py`

### Route: `POST /{project}/api/seek`

Add in project-scoped POST handler (after ticket creation routes):

```python
if remainder == "/api/seek":
    try:
        body = self._read_body()
    except (json.JSONDecodeError, ValueError):
        body = {}
    sources = body.get("sources", None)
    project_path = os.path.expanduser(proj.get("path", ""))
    
    from seek import run_seek
    with _db_lock:
        conn = get_db()
        init_db(conn)
        # Ingest pending markdown edits BEFORE mutating DB (prevents drift/duplicates)
        cli.ingest_markdown(conn, proj)
        result = run_seek(conn, proj["id"], project_path, sources=sources)
        cli.sync_to_markdown(conn, proj)
        conn.close()
    cli.regenerate_dashboard(proj)
    self._send_json(result)
    return
```

---

## 4. Dashboard UI: `src/generate.py`

### Button HTML (filter bar, after drafts toggle ~line 1822)

```html
<button class="filter-btn" id="seekBtn" data-testid="seek-btn" 
        title="Scan project files for ticket-like items">Seek</button>
```

### JavaScript (near drafts toggle handler ~line 4320)

```javascript
(function() {
  var seekBtn = document.getElementById('seekBtn');
  if (!seekBtn || !EDIT_API) return;
  
  seekBtn.addEventListener('click', function() {
    seekBtn.disabled = true;
    var origText = seekBtn.textContent;
    seekBtn.textContent = 'Seeking\u2026';
    
    fetch(EDIT_API + '/seek', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    })
    .then(function(r) { return r.json(); })
    .then(function(result) {
      seekBtn.disabled = false;
      seekBtn.textContent = origText;
      
      if (result.created > 0) {
        showAppToast(result.created + ' draft(s) created', 'success');
        // Auto-show drafts and reload to display new cards
        localStorage.setItem('tt-show-drafts', '1');
        location.reload();
      } else if (result.discovered > 0) {
        showAppToast('All ' + result.discovered + ' items already tracked', 'success');
      } else {
        showAppToast('No ticket-like items found', 'success');
      }
    })
    .catch(function() {
      seekBtn.disabled = false;
      seekBtn.textContent = origText;
      showAppToast('Seek failed', 'error');
    });
  });
})();
```

---

## 5. Tests

### `tests/test_tdd_seek.py` — Unit Tests

```
test_scan_md_tasks_finds_unchecked        — creates .md with - [ ] items, verifies extraction
test_scan_md_tasks_skips_checked          — - [x] items are ignored
test_scan_md_tasks_skips_backlog          — PRODUCT_BACKLOG.md is excluded
test_scan_readme_todos_extracts_section   — README with ## TODO header, bullets extracted
test_scan_readme_todos_no_section         — README without TODO header → empty list
test_scan_code_todos_python               — .py file with # TODO: comment
test_scan_code_todos_javascript           — .js file with // FIXME: gets high priority
test_scan_code_todos_skips_node_modules   — files in node_modules/ ignored
test_scan_changelog_unreleased            — CHANGELOG with [Unreleased] section
test_scan_changelog_no_unreleased         — no unreleased section → empty list
test_scan_github_issues_success           — mock subprocess, verify items
test_scan_github_issues_no_gh             — gh not found → empty list, no error
test_deduplicate_exact_match              — existing title blocks duplicate
test_deduplicate_case_insensitive         — "Add Auth" blocks "add auth"
test_deduplicate_source_key               — same file:line in draft desc → blocked
test_deduplicate_within_batch             — duplicate titles within scan → only first kept
test_run_seek_creates_drafts              — in-memory DB, verify draft=1 tickets created
test_run_seek_idempotent                  — run twice, second creates 0
test_run_seek_source_in_description       — verify "Source: type @ file:line" prefix
```

### `tests/test_e2e_seek.py` — Integration Tests

```
test_seek_api_returns_results             — POST /api/seek, verify response shape
test_seek_api_idempotent                  — POST twice, second returns created=0
test_seek_api_with_source_filter          — POST with sources=["md_task"], only scans .md files
```

---

## 6. File Walk Exclusions

Hardcoded skip list for `scan_code_todos`:

```python
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv",
    "dist", "build", ".next", ".artifacts", ".feedbacks",
    "docs", ".cache", ".mypy_cache", ".pytest_cache",
    "vendor", "target",  # Go/Rust
}

SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs",
    ".rb", ".java", ".c", ".cpp", ".h", ".css", ".lua",
    ".sh", ".bash", ".zsh",
}
```

---

## 7. Edge Cases

| Case | Behavior |
|------|----------|
| Empty project (no files) | Returns `{discovered: 0, created: 0, ...}` |
| Binary files | Skipped by extension filter |
| Very long TODO text | Title truncated to 200 chars, full text in description |
| Encoding errors | `open(f, errors='replace')` to handle non-UTF8 files |
| `gh` not installed | GitHub scanner silently returns empty list |
| `gh` auth expired | Subprocess returns non-zero exit → empty list |
| Thousands of TODOs | All created as drafts (user can bulk-reject via filter) |
| Re-run after confirming a draft | Confirmed ticket's title is now in `existing_titles`, so it won't be re-created |
| Re-run after rejecting a draft | Draft is deleted, so both title and source key are gone → re-discovered |

## 8. Implementation Sequencing

1. **Pre-seek fixes (Section 0):** Exclude drafts from markdown sync, fix banner copy, fix confirm path, fix drafts toggle persistence
2. TDD tests for scanners (pure functions, no DB)
3. Implement `src/seek.py` scanners + dedup
4. `run_seek()` integration test with in-memory DB
5. Wire CLI (dispatch table + handler) + API endpoint
6. Wire dashboard UI (Seek button + JS)
7. E2E tests
8. Deploy to `~/.claude/ticket-takeaway/`

## 9. Future (v2, not in this plan)

- Fuzzy title matching (Levenshtein distance)
- AI-powered triage: classify discovered items by priority/section
- Scan git blame for TODO age
- Import from Jira/Linear/GitHub Projects via API
- Bulk confirm/reject UI
