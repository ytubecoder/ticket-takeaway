# Journey Path Builder — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the element-level journey editor with a screen-level path builder that auto-discovers interactive elements, and display screenshots inline after runs.

**Architecture:** New `page_scraper.py` module scrapes pages via Playwright to discover interactive elements. The journeys page gets a 3-panel path builder UI (screen picker → interaction picker → path list). Journey runner captures screenshots via existing scenario_runner infrastructure. Screenshots served and displayed inline.

**Tech Stack:** Python 3.12, Playwright (already installed), SQLite, inline HTML/JS/CSS (no framework)

---

## Parallelization Map

```
Task 1 (page_scraper.py)  ─┐
                            ├─→ Task 3 (path builder UI)  ─→ Task 5 (integration + polish)
Task 2 (screenshot runner) ─┘                                        ↓
                                                              Task 6 (verification)
Task 4 (steps converter) ──────────────────────────────────→ Task 5
```

**Can run in parallel:** Tasks 1, 2, 4 (independent modules with no shared files)
**Sequential after:** Task 3 depends on 1. Task 5 depends on 2, 3, 4. Task 6 depends on 5.

---

### Task 1: Page Scraper Module

**Files:**
- Create: `src/page_scraper.py`
- Create: `tests/test_tdd_page_scraper.py`

- [ ] **Step 1: Write failing tests for scraper data structures and element classification**

```python
# tests/test_tdd_page_scraper.py
"""TDD tests for page_scraper — element discovery and classification."""
import pytest


def test_page_element_fields():
    from page_scraper import PageElement
    el = PageElement(
        tag="button", testid="new-ticket-btn", text="+ New",
        role="button", element_type="button", name="New ticket button",
        css_selector="button#newTicketBtn", is_navigation=False,
    )
    assert el.testid == "new-ticket-btn"
    assert el.element_type == "button"
    assert el.is_navigation is False


def test_page_scan_fields():
    from page_scraper import PageScan, PageElement
    scan = PageScan(
        url="http://localhost:8787/test/",
        title="Test Board",
        screen_name="Board",
        elements=[],
        screenshot_base64="",
        scanned_at="2026-04-12T00:00:00",
    )
    assert scan.screen_name == "Board"


def test_classify_element_button():
    from page_scraper import classify_element
    result = classify_element(tag="button", input_type="", role="", href="")
    assert result == "button"


def test_classify_element_link():
    from page_scraper import classify_element
    result = classify_element(tag="a", input_type="", role="", href="/settings")
    assert result == "link"


def test_classify_element_text_input():
    from page_scraper import classify_element
    result = classify_element(tag="input", input_type="text", role="", href="")
    assert result == "text-input"


def test_classify_element_select():
    from page_scraper import classify_element
    result = classify_element(tag="select", input_type="", role="", href="")
    assert result == "select"


def test_classify_element_checkbox():
    from page_scraper import classify_element
    result = classify_element(tag="input", input_type="checkbox", role="", href="")
    assert result == "checkbox"


def test_classify_element_role_button():
    from page_scraper import classify_element
    result = classify_element(tag="div", input_type="", role="button", href="")
    assert result == "button"


def test_derive_screen_name_root():
    from page_scraper import derive_screen_name
    assert derive_screen_name("/test-proj/", "Test — Dashboard") == "Board"


def test_derive_screen_name_settings():
    from page_scraper import derive_screen_name
    assert derive_screen_name("/test-proj/settings", "Settings") == "Settings"


def test_derive_screen_name_journeys():
    from page_scraper import derive_screen_name
    assert derive_screen_name("/test-proj/journeys", "Journeys") == "Journeys"


def test_derive_screen_name_picker():
    from page_scraper import derive_screen_name
    assert derive_screen_name("/", "Ticket Takeaway") == "Project Picker"


def test_derive_screen_name_unknown():
    from page_scraper import derive_screen_name
    assert derive_screen_name("/test-proj/unknown", "Some Page") == "Some Page"


def test_derive_element_name_from_testid():
    from page_scraper import derive_element_name
    assert derive_element_name(testid="new-ticket-btn", text="", placeholder="", title="", aria_label="") == "New ticket btn"


def test_derive_element_name_from_text():
    from page_scraper import derive_element_name
    assert derive_element_name(testid="", text="Save Changes", placeholder="", title="", aria_label="") == "Save Changes"


def test_derive_element_name_from_placeholder():
    from page_scraper import derive_element_name
    assert derive_element_name(testid="", text="", placeholder="Search items...", title="", aria_label="") == "Search items..."


def test_infer_navigation_link_with_href():
    from page_scraper import infer_is_navigation
    assert infer_is_navigation(tag="a", href="/settings", text="", testid="") is True


def test_infer_navigation_back_button():
    from page_scraper import infer_is_navigation
    assert infer_is_navigation(tag="button", href="", text="Back", testid="") is True


def test_infer_navigation_settings_toggle():
    from page_scraper import infer_is_navigation
    assert infer_is_navigation(tag="button", href="", text="", testid="settings-toggle") is True


def test_infer_navigation_journeys_btn():
    from page_scraper import infer_is_navigation
    assert infer_is_navigation(tag="button", href="", text="", testid="journeys-btn") is True


def test_infer_navigation_regular_button():
    from page_scraper import infer_is_navigation
    assert infer_is_navigation(tag="button", href="", text="Submit", testid="submit-btn") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tdd_page_scraper.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'page_scraper'`

- [ ] **Step 3: Implement page_scraper.py — data structures and pure functions**

```python
# src/page_scraper.py
"""Page scraper — discovers interactive elements on dashboard pages via Playwright.

Pure functions (classify, derive, infer) are importable without Playwright.
The scrape_page() function requires a running Playwright browser.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class PageElement:
    tag: str
    testid: str
    text: str
    role: str
    element_type: str
    name: str
    css_selector: str
    is_navigation: bool


@dataclass
class PageScan:
    url: str
    title: str
    screen_name: str
    elements: list[PageElement]
    screenshot_base64: str
    scanned_at: str


# ---------------------------------------------------------------------------
# Pure classification helpers (no Playwright needed)
# ---------------------------------------------------------------------------

def classify_element(tag: str, input_type: str, role: str, href: str) -> str:
    """Classify an HTML element into a UI element type."""
    if role == "button" or tag == "button":
        return "button"
    if tag == "a" and href:
        return "link"
    if tag == "select":
        return "select"
    if tag == "textarea":
        return "text-input"
    if tag == "input":
        if input_type in ("checkbox",):
            return "checkbox"
        if input_type in ("radio",):
            return "radio"
        return "text-input"
    if role:
        return role
    return "button"


_SCREEN_PATTERNS = [
    (re.compile(r"/$|/index\.html$"), "Board"),
    (re.compile(r"/settings$"), "Settings"),
    (re.compile(r"/journeys$"), "Journeys"),
]


def derive_screen_name(path: str, page_title: str) -> str:
    """Derive a human-readable screen name from URL path and page title."""
    if path == "/":
        return "Project Picker"
    for pattern, name in _SCREEN_PATTERNS:
        if pattern.search(path):
            return name
    return page_title or "Unknown"


def derive_element_name(
    testid: str, text: str, placeholder: str, title: str, aria_label: str,
) -> str:
    """Derive a human-readable name for an element from its attributes."""
    if aria_label:
        return aria_label
    if text and len(text) < 60:
        return text.strip()
    if title:
        return title
    if placeholder:
        return placeholder
    if testid:
        return testid.replace("-", " ").replace("_", " ").capitalize()
    return "(unnamed)"


_NAV_TESTIDS = {"settings-toggle", "journeys-btn", "journeys-back", "settings-back", "detail-close"}
_NAV_TEXTS = {"back", "board", "settings", "journeys", "home"}


def infer_is_navigation(tag: str, href: str, text: str, testid: str) -> bool:
    """Infer whether clicking this element navigates to a different screen."""
    if tag == "a" and href:
        return True
    if testid in _NAV_TESTIDS:
        return True
    text_lower = text.lower().strip()
    for nav_word in _NAV_TEXTS:
        if nav_word in text_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Playwright-based scraping (requires a running browser)
# ---------------------------------------------------------------------------

_INTERACTIVE_SELECTOR = (
    "button, a[href], input, select, textarea, "
    "[role='button'], [onclick], [data-testid]"
)


def scrape_page(page: Any, url: str) -> PageScan:
    """Scrape a single page for interactive elements. Requires a Playwright Page object.

    The page is navigated to `url`, waited for load, then all interactive
    elements are extracted.
    """
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(500)  # let JS render

    title = page.title()
    path = page.url.split("//", 1)[-1].split("/", 1)[-1]
    path = "/" + path if not path.startswith("/") else path
    # Strip project prefix for screen name derivation
    parts = path.split("/", 2)
    if len(parts) >= 3:
        path_for_name = "/" + parts[2] if parts[2] else "/"
    else:
        path_for_name = path

    screen_name = derive_screen_name(path_for_name, title)

    # Extract elements
    raw_elements = page.evaluate("""() => {
        const sel = `""" + _INTERACTIVE_SELECTOR + """`;
        const els = document.querySelectorAll(sel);
        const results = [];
        for (const el of els) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) continue;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (parseFloat(style.opacity) === 0) continue;
            results.push({
                tag: el.tagName.toLowerCase(),
                testid: el.getAttribute('data-testid') || '',
                text: (el.textContent || '').trim().substring(0, 100),
                role: el.getAttribute('role') || '',
                href: el.getAttribute('href') || '',
                inputType: el.getAttribute('type') || '',
                placeholder: el.getAttribute('placeholder') || '',
                title: el.getAttribute('title') || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                cssSelector: _buildSelector(el),
            });
        }
        function _buildSelector(el) {
            if (el.id) return '#' + el.id;
            if (el.getAttribute('data-testid')) return '[data-testid="' + el.getAttribute('data-testid') + '"]';
            let s = el.tagName.toLowerCase();
            if (el.className && typeof el.className === 'string') s += '.' + el.className.trim().split(/\\s+/).join('.');
            return s;
        }
        return results;
    }""")

    elements = []
    for raw in raw_elements:
        el_type = classify_element(raw["tag"], raw["inputType"], raw["role"], raw["href"])
        name = derive_element_name(raw["testid"], raw["text"], raw["placeholder"], raw["title"], raw["ariaLabel"])
        is_nav = infer_is_navigation(raw["tag"], raw["href"], raw["text"], raw["testid"])
        elements.append(PageElement(
            tag=raw["tag"],
            testid=raw["testid"],
            text=raw["text"],
            role=raw["role"],
            element_type=el_type,
            name=name,
            css_selector=raw["cssSelector"],
            is_navigation=is_nav,
        ))

    # Take screenshot thumbnail
    screenshot_bytes = page.screenshot(full_page=False)
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

    return PageScan(
        url=url,
        title=title,
        screen_name=screen_name,
        elements=elements,
        screenshot_base64=screenshot_b64,
        scanned_at=datetime.now(timezone.utc).isoformat(),
    )


def scan_all_screens(base_url: str, browser: Any) -> list[PageScan]:
    """Scan all known screens of a project. Returns list of PageScan.

    Args:
        base_url: e.g. "http://localhost:8787/ticket-takeaway"
        browser: Playwright Browser instance
    """
    routes = ["/", "/settings"]  # journeys page excluded (it's the page we're on)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    scans = []

    for route in routes:
        url = base_url.rstrip("/") + route
        try:
            scan = scrape_page(page, url)
            scans.append(scan)
        except Exception as exc:
            print(f"[page_scraper] WARNING: failed to scan {url}: {exc}")

    # Special: Detail Overlay (open first card on Board)
    try:
        board_url = base_url.rstrip("/") + "/"
        page.goto(board_url, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        # Click first card to open detail overlay
        first_card = page.locator("[data-testid^='card-open-btn']").first
        if first_card.is_visible():
            first_card.click()
            page.wait_for_timeout(300)
            overlay_scan = PageScan(
                url=board_url + "#detail",
                title="Detail Overlay",
                screen_name="Detail Overlay",
                elements=[],
                screenshot_base64="",
                scanned_at=datetime.now(timezone.utc).isoformat(),
            )
            # Re-scrape with overlay open
            full_scan = scrape_page.__wrapped__(page) if hasattr(scrape_page, '__wrapped__') else None
            # Simpler: just evaluate elements again
            raw_elements = page.evaluate("""() => {
                const overlay = document.querySelector('[data-testid="detail-overlay"]');
                if (!overlay) return [];
                const sel = `button, a[href], input, select, textarea, [role='button'], [data-testid]`;
                const els = overlay.querySelectorAll(sel);
                const results = [];
                for (const el of els) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    results.push({
                        tag: el.tagName.toLowerCase(),
                        testid: el.getAttribute('data-testid') || '',
                        text: (el.textContent || '').trim().substring(0, 100),
                        role: el.getAttribute('role') || '',
                        href: el.getAttribute('href') || '',
                        inputType: el.getAttribute('type') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        title: el.getAttribute('title') || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        cssSelector: el.id ? '#' + el.id : (el.getAttribute('data-testid') ? '[data-testid="' + el.getAttribute('data-testid') + '"]' : el.tagName.toLowerCase()),
                    });
                }
                return results;
            }""")
            for raw in raw_elements:
                el_type = classify_element(raw["tag"], raw["inputType"], raw["role"], raw["href"])
                name = derive_element_name(raw["testid"], raw["text"], raw["placeholder"], raw["title"], raw["ariaLabel"])
                is_nav = infer_is_navigation(raw["tag"], raw["href"], raw["text"], raw["testid"])
                overlay_scan.elements.append(PageElement(
                    tag=raw["tag"], testid=raw["testid"], text=raw["text"],
                    role=raw["role"], element_type=el_type, name=name,
                    css_selector=raw["cssSelector"], is_navigation=is_nav,
                ))
            screenshot_bytes = page.screenshot(full_page=False)
            overlay_scan.screenshot_base64 = base64.b64encode(screenshot_bytes).decode("ascii")
            scans.append(overlay_scan)
    except Exception as exc:
        print(f"[page_scraper] WARNING: failed to scan Detail Overlay: {exc}")

    ctx.close()
    return scans


def scans_to_json(scans: list[PageScan]) -> list[dict]:
    """Convert PageScan list to JSON-serializable dicts."""
    result = []
    for scan in scans:
        result.append({
            "id": scan.screen_name.lower().replace(" ", "-"),
            "name": scan.screen_name,
            "url": scan.url,
            "elements": [
                {
                    "name": el.name,
                    "testid": el.testid,
                    "type": el.element_type,
                    "is_navigation": el.is_navigation,
                    "tag": el.tag,
                    "css": el.css_selector,
                    "text": el.text[:50] if el.text else "",
                }
                for el in scan.elements
            ],
            "thumbnail": "data:image/png;base64," + scan.screenshot_base64 if scan.screenshot_base64 else "",
            "scanned_at": scan.scanned_at,
        })
    return result
```

- [ ] **Step 4: Run TDD tests to verify they pass**

Run: `python3 -m pytest tests/test_tdd_page_scraper.py -v`
Expected: All PASS

- [ ] **Step 5: Deploy and commit**

```bash
cp src/page_scraper.py ~/.claude/ticket-takeaway/page_scraper.py
git add src/page_scraper.py tests/test_tdd_page_scraper.py
git commit -m "feat: page scraper module — discovers interactive elements via Playwright"
```

---

### Task 2: Screenshot Serving + Storage for Journey Runs

**Files:**
- Modify: `src/serve.py` (add screenshot serving endpoint, ~20 lines)
- Modify: `src/journeys.py` (add screenshot_dir helper, ~10 lines)

- [ ] **Step 1: Add screenshot directory helper to journeys.py**

Add at end of `src/journeys.py`:

```python
def screenshot_dir_for_run(project_path: str, journey_id: str, run_id: str) -> str:
    """Return the directory path for storing run screenshots."""
    from pathlib import Path
    d = Path(project_path) / ".artifacts" / "journeys" / journey_id / run_id
    d.mkdir(parents=True, exist_ok=True)
    return str(d)
```

- [ ] **Step 2: Add screenshot serving endpoint to serve.py**

In `do_GET`, after the journey run details handler (the `GET /api/journeys/{id}/runs/{run_id}` block), add:

```python
        # Journey API: serve run screenshot
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/runs/([A-Za-z0-9_-]+)/screenshots/(.+\.png)$", remainder)
        if m:
            journey_id, run_id, filename = m.group(1), m.group(2), m.group(3)
            # Security: reject path traversal
            if "/" in filename or "\\" in filename or ".." in filename:
                self._send_json({"error": "Invalid filename"}, 400)
                return
            project_path = proj.get("path", "")
            screenshot_path = os.path.join(project_path, ".artifacts", "journeys", journey_id, run_id, filename)
            if os.path.isfile(screenshot_path):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                with open(screenshot_path, "rb") as f:
                    data = f.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_json({"error": "Screenshot not found"}, 404)
            return
```

- [ ] **Step 3: Deploy and commit**

```bash
cp src/serve.py ~/.claude/ticket-takeaway/serve.py
cp src/journeys.py ~/.claude/ticket-takeaway/journeys.py
git add src/serve.py src/journeys.py
git commit -m "feat: screenshot serving endpoint for journey runs"
```

---

### Task 3: Screens API Endpoint + Caching

**Files:**
- Modify: `src/serve.py` (add `/api/screens` endpoint + scan cache, ~60 lines)

**Depends on:** Task 1 (page_scraper.py)

- [ ] **Step 1: Add import for page_scraper to serve.py**

After the existing `from journeys import ...` line:

```python
from page_scraper import scan_all_screens, scans_to_json
```

- [ ] **Step 2: Add scan cache and API endpoint**

Add near the top of serve.py, after `_scenario_runs_lock`:

```python
# Page scan cache — populated on demand, invalidated manually
_page_scan_cache: dict[str, list[dict]] = {}  # project_id -> scans JSON
_page_scan_lock = threading.Lock()
```

In `do_POST`, before the journey create handler, add:

```python
        # Screens API: scan pages for interactive elements
        if remainder == "/api/screens/scan":
            project_id = proj["id"]
            base_url = f"http://localhost:{SERVER_PORT}/{project_id}"
            def _do_scan():
                try:
                    from playwright.sync_api import sync_playwright
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True)
                        scans = scan_all_screens(base_url, browser)
                        browser.close()
                    result = scans_to_json(scans)
                    with _page_scan_lock:
                        _page_scan_cache[project_id] = result
                    return result
                except Exception as exc:
                    return {"error": str(exc)}
            # Run synchronously (takes ~2-3 seconds)
            result = _do_scan()
            if isinstance(result, dict) and "error" in result:
                self._send_json(result, 500)
            else:
                self._send_json({"screens": result})
            return
```

In `do_GET`, after the journeys run details handler, add:

```python
        # Screens API: get cached scan results
        if remainder == "/api/screens":
            project_id = proj["id"]
            with _page_scan_lock:
                cached = _page_scan_cache.get(project_id)
            if cached:
                self._send_json({"screens": cached})
            else:
                self._send_json({"screens": [], "hint": "No scan yet. POST /api/screens/scan to discover pages."})
            return
```

- [ ] **Step 3: Deploy and commit**

```bash
cp src/serve.py ~/.claude/ticket-takeaway/serve.py
cp src/page_scraper.py ~/.claude/ticket-takeaway/page_scraper.py
git add src/serve.py src/page_scraper.py
git commit -m "feat: /api/screens endpoint — scan pages for interactive elements"
```

---

### Task 4: Path-to-Steps Converter

**Files:**
- Modify: `src/journeys.py` (add `build_steps_from_path()`, ~80 lines)
- Create: `tests/test_tdd_path_builder.py`

**Can run in parallel with Tasks 1-3**

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tdd_path_builder.py
"""TDD tests for path-to-steps conversion."""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def test_navigate_to_board_produces_open_step():
    from journeys import build_steps_from_path
    path = [{"screen": "Board", "interaction": None}]
    steps = build_steps_from_path(path, "user")
    assert len(steps) >= 1
    assert steps[0]["action"] == "open"
    assert steps[0]["value"] == ""


def test_navigate_to_settings_produces_open_step():
    from journeys import build_steps_from_path
    path = [{"screen": "Settings", "interaction": None}]
    steps = build_steps_from_path(path, "user")
    assert steps[0]["action"] == "open"
    assert steps[0]["value"] == "/settings"


def test_click_interaction_produces_click_step():
    from journeys import build_steps_from_path
    path = [
        {"screen": "Board", "interaction": None},
        {"screen": "Board", "interaction": {"type": "button", "testid": "new-ticket-btn", "name": "New"}},
    ]
    steps = build_steps_from_path(path, "user")
    click_steps = [s for s in steps if s["action"] == "click"]
    assert len(click_steps) == 1
    assert click_steps[0]["target"]["testid"] == "new-ticket-btn"


def test_fill_interaction_produces_fill_step():
    from journeys import build_steps_from_path
    path = [
        {"screen": "Board", "interaction": None},
        {"screen": "Board", "interaction": {"type": "text-input", "testid": "search-input", "name": "Search", "fill_value": "test query"}},
    ]
    steps = build_steps_from_path(path, "user")
    fill_steps = [s for s in steps if s["action"] == "fill"]
    assert len(fill_steps) == 1
    assert fill_steps[0]["target"]["testid"] == "search-input"
    assert fill_steps[0]["value"] == "test query"


def test_screen_change_inserts_auto_capture():
    from journeys import build_steps_from_path
    path = [
        {"screen": "Board", "interaction": None},
        {"screen": "Settings", "interaction": None},
    ]
    steps = build_steps_from_path(path, "user")
    captures = [s for s in steps if s["action"] == "capture"]
    assert len(captures) >= 1  # at least one auto-capture


def test_explicit_screenshot_step():
    from journeys import build_steps_from_path
    path = [
        {"screen": "Board", "interaction": None},
        {"screen": "Board", "interaction": {"type": "screenshot", "name": "Board overview"}},
    ]
    steps = build_steps_from_path(path, "user")
    captures = [s for s in steps if s["action"] == "capture"]
    assert len(captures) >= 1


def test_navigation_click_opens_target_screen():
    from journeys import build_steps_from_path
    path = [
        {"screen": "Board", "interaction": None},
        {"screen": "Board", "interaction": {"type": "button", "testid": "settings-toggle", "name": "Settings", "navigates_to": "Settings"}},
    ]
    steps = build_steps_from_path(path, "user")
    # Should produce: open board, click settings-toggle — no separate "open /settings"
    actions = [s["action"] for s in steps]
    assert "click" in actions
    assert actions.count("open") == 1  # only the initial board open


def test_all_steps_have_actor():
    from journeys import build_steps_from_path
    path = [
        {"screen": "Board", "interaction": None},
        {"screen": "Board", "interaction": {"type": "button", "testid": "new-ticket-btn", "name": "New"}},
    ]
    steps = build_steps_from_path(path, "reviewer")
    for step in steps:
        assert step["actor"] == "reviewer"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tdd_path_builder.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_steps_from_path'`

- [ ] **Step 3: Implement build_steps_from_path in journeys.py**

Add at end of `src/journeys.py`, before the export/import section:

```python
# ---------------------------------------------------------------------------
# Path Builder: screen-level path → element-level steps
# ---------------------------------------------------------------------------

_SCREEN_ROUTES = {
    "Board": "",
    "Settings": "/settings",
    "Journeys": "/journeys",
    "Project Picker": "/",
    "Detail Overlay": "",  # opened via click, not navigation
}


def build_steps_from_path(
    path: list[dict],
    actor: str = "user",
) -> list[dict]:
    """Convert a screen-level path into element-level step dicts.

    Each path entry is: {"screen": "Board", "interaction": {...} or None}
    Interaction dict: {"type": "button"|"text-input"|"screenshot", "testid": "...",
                        "name": "...", "fill_value": "...", "navigates_to": "..."}

    Returns list of step field dicts ready for add_step().
    """
    steps: list[dict] = []
    current_screen: str | None = None

    for entry in path:
        screen = entry["screen"]
        interaction = entry.get("interaction")

        # Screen change: emit open step (unless reached via navigation click)
        if screen != current_screen and interaction is None:
            route = _SCREEN_ROUTES.get(screen, "")
            steps.append({
                "actor": actor,
                "action": "open",
                "label": f"Go to {screen}",
                "value": route,
                "target": None,
                "key": "",
                "capture": None,
                "assertion": None,
            })
            # Auto-capture on screen arrival
            steps.append({
                "actor": actor,
                "action": "capture",
                "label": f"Screenshot: {screen}",
                "value": "",
                "target": None,
                "key": "",
                "capture": {"name": screen.lower().replace(" ", "-")},
                "assertion": None,
            })
            current_screen = screen
            continue

        if interaction is None:
            continue

        itype = interaction.get("type", "button")
        testid = interaction.get("testid", "")
        name = interaction.get("name", "")

        if itype == "screenshot":
            steps.append({
                "actor": actor,
                "action": "capture",
                "label": f"Screenshot: {name or current_screen}",
                "value": "",
                "target": None,
                "key": "",
                "capture": {"name": (name or current_screen or "capture").lower().replace(" ", "-")},
                "assertion": None,
            })
        elif itype == "text-input":
            fill_value = interaction.get("fill_value", "")
            target = {"testid": testid} if testid else None
            steps.append({
                "actor": actor,
                "action": "fill",
                "label": f"Fill: {name}",
                "value": fill_value,
                "target": target,
                "key": "",
                "capture": None,
                "assertion": None,
            })
        elif itype == "select":
            fill_value = interaction.get("fill_value", "")
            target = {"testid": testid} if testid else None
            steps.append({
                "actor": actor,
                "action": "select",
                "label": f"Select: {name}",
                "value": fill_value,
                "target": target,
                "key": "",
                "capture": None,
                "assertion": None,
            })
        else:
            # button, link — emit click
            target = {"testid": testid} if testid else None
            steps.append({
                "actor": actor,
                "action": "click",
                "label": f"Click: {name}",
                "value": "",
                "target": target,
                "key": "",
                "capture": None,
                "assertion": None,
            })

        # If navigation, update current screen and auto-capture
        navigates_to = interaction.get("navigates_to")
        if navigates_to:
            current_screen = navigates_to
            steps.append({
                "actor": actor,
                "action": "capture",
                "label": f"Screenshot: {navigates_to}",
                "value": "",
                "target": None,
                "key": "",
                "capture": {"name": navigates_to.lower().replace(" ", "-")},
                "assertion": None,
            })

    return steps
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tdd_path_builder.py -v`
Expected: All PASS

- [ ] **Step 5: Deploy and commit**

```bash
cp src/journeys.py ~/.claude/ticket-takeaway/journeys.py
git add src/journeys.py tests/test_tdd_path_builder.py
git commit -m "feat: build_steps_from_path — converts screen-level path to element-level steps"
```

---

### Task 5: Path Builder UI in Journeys Page

**Files:**
- Modify: `src/serve.py` — `_render_journeys_page()` (add path builder HTML/CSS/JS, ~300 lines)

**Depends on:** Tasks 1, 2, 3, 4

- [ ] **Step 1: Add path builder CSS to the journeys page style block**

In `_render_journeys_page`, after the `.graph-legend-swatch` CSS rule, add:

```css
.path-builder {{ display: grid; grid-template-columns: 200px 280px 1fr; gap: 16px; margin: 20px 0; min-height: 300px; }}
.pb-panel {{ background: var(--bg-card); border: 1px solid var(--border-default); border-radius: 8px; padding: 12px; }}
.pb-panel h4 {{ font-size: 11px; font-weight: 600; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; }}
.pb-screen {{ display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 6px; cursor: pointer; font-size: 13px; color: var(--text-secondary); transition: background 0.15s; }}
.pb-screen:hover {{ background: var(--bg-hover); }}
.pb-screen.selected {{ background: rgba(59,130,246,0.12); color: var(--accent); }}
.pb-screen .thumb {{ width: 32px; height: 20px; border-radius: 3px; border: 1px solid var(--border-subtle); object-fit: cover; }}
.pb-interaction {{ display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; color: var(--text-primary); transition: background 0.15s; border: 1px solid transparent; }}
.pb-interaction:hover {{ background: var(--bg-hover); border-color: var(--border-default); }}
.pb-interaction .type-icon {{ font-size: 11px; color: var(--text-tertiary); width: 16px; text-align: center; }}
.pb-interaction.nav {{ color: var(--accent); }}
.pb-fill-input {{ width: 100%; margin-top: 4px; padding: 4px 8px; font-size: 11px; background: var(--bg-page); border: 1px solid var(--border-default); border-radius: 4px; color: var(--text-primary); font-family: inherit; }}
.pb-path-item {{ display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 6px; font-size: 12px; border-bottom: 1px solid var(--border-subtle); }}
.pb-path-item .step-icon {{ font-size: 14px; width: 20px; text-align: center; }}
.pb-path-item .step-label {{ flex: 1; color: var(--text-primary); }}
.pb-path-item .step-screen {{ font-size: 10px; color: var(--text-tertiary); }}
.pb-path-item .remove-btn {{ opacity: 0; font-size: 11px; cursor: pointer; color: var(--red); background: none; border: none; padding: 2px 6px; }}
.pb-path-item:hover .remove-btn {{ opacity: 1; }}
.pb-scan-btn {{ width: 100%; margin-top: 8px; }}
.pb-group-label {{ font-size: 10px; font-weight: 600; color: var(--text-tertiary); text-transform: uppercase; margin: 8px 0 4px; padding: 0 10px; }}
```

- [ ] **Step 2: Add path builder HTML container**

In the detail view, after the graph-view div and before the run-results div, add:

```html
    <div id="path-builder-view" data-testid="path-builder-view" style="display:none;margin:20px 0;">
      <div class="path-builder">
        <div class="pb-panel">
          <h4>Screen</h4>
          <div id="pb-screens" data-testid="pb-screens"></div>
          <button class="btn btn-ghost btn-sm pb-scan-btn" onclick="scanPages()" data-testid="pb-scan-btn">Scan Pages</button>
        </div>
        <div class="pb-panel">
          <h4>Interaction</h4>
          <div id="pb-interactions" data-testid="pb-interactions">
            <p style="color:var(--text-tertiary);font-size:12px;">Select a screen first</p>
          </div>
        </div>
        <div class="pb-panel">
          <h4>Your Path</h4>
          <div id="pb-path" data-testid="pb-path"></div>
          <div style="margin-top:12px;display:flex;gap:8px;">
            <button class="btn btn-success btn-sm" onclick="savePath()" data-testid="pb-save" style="flex:1;">Save Journey</button>
          </div>
        </div>
      </div>
    </div>
```

- [ ] **Step 3: Update the view toggle to include Path Builder as the default**

Replace the view toggle HTML:

```html
      <div class="view-toggle" style="margin-left:12px;" data-testid="view-toggle">
        <button class="active" onclick="setView('build')" data-testid="view-build">Build</button>
        <button onclick="setView('steps')" data-testid="view-steps">Steps</button>
        <button onclick="setView('graph')" data-testid="view-graph">Graph</button>
      </div>
```

- [ ] **Step 4: Add path builder JavaScript**

In the IIFE, before the `/* ── Init ──` section, add the path builder JS:

```javascript
  /* ── Path Builder ──────────────────────────────────────── */
  var pbScreens = [];       // [{id, name, url, elements, thumbnail}]
  var pbSelectedScreen = null;
  var pbPath = [];           // [{screen, interaction}]
  var pbScanning = false;

  window.scanPages = function() {{
    if (pbScanning) return;
    pbScanning = true;
    var btn = document.querySelector('[data-testid="pb-scan-btn"]');
    if (btn) btn.textContent = 'Scanning...';
    apiPost('/screens/scan', {{}}).then(function(r) {{
      pbScanning = false;
      if (btn) btn.textContent = 'Scan Pages';
      if (r.data.error) {{ toast(r.data.error, 'error'); return; }}
      pbScreens = r.data.screens || [];
      // Add a "Screenshot" pseudo-interaction to every screen
      renderPbScreens();
      if (pbScreens.length > 0) selectPbScreen(pbScreens[0].id);
      toast('Found ' + pbScreens.length + ' screens');
    }});
  }};

  // Also try loading cached screens on init
  function loadCachedScreens() {{
    apiGet('/screens').then(function(data) {{
      if (data.screens && data.screens.length > 0) {{
        pbScreens = data.screens;
        renderPbScreens();
      }}
    }});
  }}

  function renderPbScreens() {{
    var container = document.getElementById('pb-screens');
    container.textContent = '';
    pbScreens.forEach(function(screen) {{
      var el = document.createElement('div');
      el.className = 'pb-screen' + (pbSelectedScreen === screen.id ? ' selected' : '');
      el.setAttribute('data-testid', 'pb-screen-' + screen.id);
      if (screen.thumbnail) {{
        var img = document.createElement('img');
        img.className = 'thumb';
        img.src = screen.thumbnail;
        el.appendChild(img);
      }}
      var name = document.createElement('span');
      name.textContent = screen.name;
      el.appendChild(name);
      el.onclick = function() {{ selectPbScreen(screen.id); }};
      container.appendChild(el);
    }});
  }}

  function selectPbScreen(screenId) {{
    pbSelectedScreen = screenId;
    renderPbScreens();
    renderPbInteractions();
  }}

  function renderPbInteractions() {{
    var container = document.getElementById('pb-interactions');
    container.textContent = '';
    var screen = pbScreens.find(function(s) {{ return s.id === pbSelectedScreen; }});
    if (!screen) return;

    // Group by type
    var navEls = screen.elements.filter(function(e) {{ return e.is_navigation; }});
    var clickEls = screen.elements.filter(function(e) {{ return !e.is_navigation && (e.type === 'button' || e.type === 'link'); }});
    var inputEls = screen.elements.filter(function(e) {{ return e.type === 'text-input' || e.type === 'select'; }});

    function addGroup(label, elements, isNav) {{
      if (elements.length === 0) return;
      var groupLabel = document.createElement('div');
      groupLabel.className = 'pb-group-label';
      groupLabel.textContent = label;
      container.appendChild(groupLabel);
      elements.forEach(function(el) {{
        var row = document.createElement('div');
        row.className = 'pb-interaction' + (isNav ? ' nav' : '');
        var icon = document.createElement('span');
        icon.className = 'type-icon';
        icon.textContent = el.type === 'text-input' ? '\u270D' : el.is_navigation ? '\u2192' : '\u25CF';
        row.appendChild(icon);
        var nameSpan = document.createElement('span');
        nameSpan.textContent = el.name;
        nameSpan.style.flex = '1';
        row.appendChild(nameSpan);
        var addBtn = document.createElement('button');
        addBtn.className = 'btn btn-ghost btn-sm';
        addBtn.textContent = '+ Add';
        addBtn.style.cssText = 'padding:2px 8px;font-size:10px;';

        if (el.type === 'text-input' || el.type === 'select') {{
          // Show fill input
          var wrapper = document.createElement('div');
          wrapper.style.width = '100%';
          wrapper.appendChild(row);
          var fillInput = document.createElement('input');
          fillInput.className = 'pb-fill-input';
          fillInput.placeholder = el.type === 'select' ? 'Select value...' : 'Enter text...';
          fillInput.setAttribute('data-testid', 'pb-fill-' + (el.testid || 'input'));
          addBtn.onclick = function() {{
            addToPath(screen, {{
              type: el.type, testid: el.testid, name: el.name,
              fill_value: fillInput.value,
            }});
            fillInput.value = '';
          }};
          wrapper.appendChild(fillInput);
          row.appendChild(addBtn);
          container.appendChild(wrapper);
        }} else {{
          addBtn.onclick = function() {{
            addToPath(screen, {{
              type: el.type, testid: el.testid, name: el.name,
              navigates_to: isNav ? _guessTargetScreen(el) : undefined,
            }});
          }};
          row.appendChild(addBtn);
          container.appendChild(row);
        }}
      }});
    }}

    // Screenshot option (always first)
    var ssRow = document.createElement('div');
    ssRow.className = 'pb-interaction';
    var ssIcon = document.createElement('span');
    ssIcon.className = 'type-icon';
    ssIcon.textContent = String.fromCodePoint(0x1F4F7);
    ssRow.appendChild(ssIcon);
    var ssName = document.createElement('span');
    ssName.textContent = 'Take Screenshot';
    ssName.style.flex = '1';
    ssRow.appendChild(ssName);
    var ssBtn = document.createElement('button');
    ssBtn.className = 'btn btn-ghost btn-sm';
    ssBtn.textContent = '+ Add';
    ssBtn.style.cssText = 'padding:2px 8px;font-size:10px;';
    ssBtn.onclick = function() {{
      addToPath(screen, {{ type: 'screenshot', name: screen.name + ' screenshot' }});
    }};
    ssRow.appendChild(ssBtn);
    container.appendChild(ssRow);

    addGroup('Navigate', navEls, true);
    addGroup('Actions', clickEls, false);
    addGroup('Inputs', inputEls, false);
  }}

  function _guessTargetScreen(el) {{
    var t = (el.testid || '').toLowerCase();
    var n = (el.name || '').toLowerCase();
    if (t.indexOf('settings') >= 0 || n.indexOf('settings') >= 0) return 'Settings';
    if (t.indexOf('journey') >= 0 || n.indexOf('journey') >= 0) return 'Journeys';
    if (t.indexOf('back') >= 0 || n.indexOf('board') >= 0) return 'Board';
    if (t.indexOf('detail-close') >= 0) return 'Board';
    return null;
  }}

  function addToPath(screen, interaction) {{
    // If changing screens, add a screen entry first
    var lastScreen = pbPath.length > 0 ? pbPath[pbPath.length - 1].screen : null;
    if (lastScreen !== screen.name) {{
      pbPath.push({{ screen: screen.name, interaction: null }});
    }}
    if (interaction) {{
      pbPath.push({{ screen: screen.name, interaction: interaction }});
    }}
    // Auto-advance to target screen if navigation
    if (interaction && interaction.navigates_to) {{
      var target = pbScreens.find(function(s) {{ return s.name === interaction.navigates_to; }});
      if (target) selectPbScreen(target.id);
    }}
    renderPbPath();
  }}

  function renderPbPath() {{
    var container = document.getElementById('pb-path');
    container.textContent = '';
    if (pbPath.length === 0) {{
      var empty = document.createElement('p');
      empty.style.cssText = 'color:var(--text-tertiary);font-size:12px;';
      empty.textContent = 'Pick a screen and add interactions';
      container.appendChild(empty);
      return;
    }}
    pbPath.forEach(function(entry, idx) {{
      var row = document.createElement('div');
      row.className = 'pb-path-item';
      var icon = document.createElement('span');
      icon.className = 'step-icon';
      if (!entry.interaction) {{
        icon.textContent = '\u{1F4BB}';
      }} else if (entry.interaction.type === 'screenshot') {{
        icon.textContent = String.fromCodePoint(0x1F4F7);
      }} else if (entry.interaction.type === 'text-input' || entry.interaction.type === 'select') {{
        icon.textContent = '\u270D';
      }} else {{
        icon.textContent = '\u{1F449}';
      }}
      row.appendChild(icon);
      var label = document.createElement('span');
      label.className = 'step-label';
      if (!entry.interaction) {{
        label.textContent = 'Go to ' + entry.screen;
      }} else {{
        label.textContent = entry.interaction.name || entry.interaction.type;
        if (entry.interaction.fill_value) {{
          label.textContent += ': "' + entry.interaction.fill_value + '"';
        }}
      }}
      row.appendChild(label);
      var screenTag = document.createElement('span');
      screenTag.className = 'step-screen';
      screenTag.textContent = entry.screen;
      row.appendChild(screenTag);
      var removeBtn = document.createElement('button');
      removeBtn.className = 'remove-btn';
      removeBtn.textContent = '\u00D7';
      (function(i) {{
        removeBtn.onclick = function() {{
          pbPath.splice(i, 1);
          renderPbPath();
        }};
      }})(idx);
      row.appendChild(removeBtn);
      container.appendChild(row);
    }});
  }}

  window.savePath = function() {{
    if (pbPath.length === 0) {{ toast('Add some steps first', 'error'); return; }}
    if (!currentJourney) {{ toast('No journey selected', 'error'); return; }}
    // Convert path to steps and save via API
    apiPost('/journeys/' + currentJourney.id + '/build-path', {{
      path: pbPath,
      actor: 'user',
    }}).then(function(r) {{
      if (r.data.error) {{ toast(r.data.error, 'error'); return; }}
      toast('Path saved (' + (r.data.steps_created || 0) + ' steps)');
      openJourney(currentJourney.id);
    }});
  }};
```

- [ ] **Step 5: Update setView to handle 'build' mode**

Update the existing `setView` function:

```javascript
  window.setView = function(mode) {{
    var stepsEl = document.getElementById('steps-view');
    var graphEl = document.getElementById('graph-view');
    var buildEl = document.getElementById('path-builder-view');
    var btns = document.querySelectorAll('.view-toggle button');
    stepsEl.style.display = 'none';
    graphEl.style.display = 'none';
    buildEl.style.display = 'none';
    btns.forEach(function(b) {{ b.classList.remove('active'); }});
    if (mode === 'graph') {{
      graphEl.style.display = 'block';
      btns[2].classList.add('active');
      renderGraph();
    }} else if (mode === 'steps') {{
      stepsEl.style.display = 'block';
      btns[1].classList.add('active');
    }} else {{
      buildEl.style.display = 'block';
      btns[0].classList.add('active');
      loadCachedScreens();
    }}
  }};
```

- [ ] **Step 6: Add build-path POST endpoint to serve.py**

In `do_POST`, after the journey link handler:

```python
        # Journey API: build journey from screen-level path
        m = re.match(r"^/api/journeys/([A-Za-z0-9_-]+)/build-path$", remainder)
        if m:
            journey_id = m.group(1)
            try:
                body = self._read_body()
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            path_entries = body.get("path", [])
            actor = body.get("actor", "user")
            if not path_entries:
                self._send_json({"error": "path is required"}, 400)
                return
            try:
                from journeys import build_steps_from_path
                step_dicts = build_steps_from_path(path_entries, actor)
                with _db_lock:
                    conn = get_db()
                    init_db(conn)
                    # Clear existing steps
                    conn.execute(
                        "DELETE FROM journey_steps WHERE journey_id = ? AND project_id = ?",
                        (journey_id, proj["id"]),
                    )
                    # Insert new steps
                    for sd in step_dicts:
                        add_step(conn, journey_id, proj["id"], **sd)
                    conn.commit()
                    conn.close()
                _auto_export_journey(proj["id"], journey_id, proj.get("path", ""))
                self._send_json({"ok": True, "steps_created": len(step_dicts)})
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            return
```

- [ ] **Step 7: Deploy and commit**

```bash
cp src/serve.py ~/.claude/ticket-takeaway/serve.py
git add src/serve.py
git commit -m "feat: path builder UI — screen-level journey authoring with auto-discovery"
```

---

### Task 6: Integration, Screenshot Display + Verification

**Files:**
- Modify: `src/serve.py` (screenshot display in run results, ~40 lines)

**Depends on:** Task 5

- [ ] **Step 1: Enhance run results display to show screenshot thumbnails**

In the `loadRunResults` JS function, modify the timeline rendering to show screenshots. After the `step.appendChild(l)` line in the `stepResults.forEach`, add:

```javascript
        if (sr.screenshot_path) {{
          var img = document.createElement('img');
          img.src = API + '/journeys/' + journey.id + '/runs/' + latest.id + '/screenshots/' + sr.screenshot_path.split('/').pop();
          img.style.cssText = 'width:60px;height:40px;object-fit:cover;border-radius:4px;border:1px solid var(--border-default);margin-top:4px;cursor:pointer;';
          img.onclick = function(e) {{
            e.stopPropagation();
            window.open(img.src, '_blank');
          }};
          step.appendChild(img);
        }}
```

- [ ] **Step 2: Make path builder the default view when opening a journey**

In the `openJourney` function, after `dv.className = 'journey-detail active';`, add:

```javascript
      setView('build');
```

- [ ] **Step 3: Deploy, restart server, and verify end-to-end**

```bash
cp src/serve.py ~/.claude/ticket-takeaway/serve.py
pkill -f serve.py; sleep 1
python3 ~/.claude/ticket-takeaway/serve.py &
```

Verification checklist:
1. Open dashboard → click Journeys button → see journeys list
2. Click "New Journey" → enters detail with path builder as default view
3. Click "Scan Pages" → see Board, Settings, Detail Overlay appear
4. Click "Board" → see interactive elements (Navigate, Actions, Inputs)
5. Click "+ Add" on "Settings" navigation → auto-advances to Settings screen
6. Click "Take Screenshot" → appears in path list
7. Click "Save Journey" → saves steps
8. Toggle to "Steps" → see generated element-level steps
9. Toggle to "Graph" → see screen topology
10. Click "Run" → after completion, see screenshot thumbnails in timeline

- [ ] **Step 4: Run all TDD tests**

```bash
python3 -m pytest tests/test_tdd_*.py -v
```
Expected: All pass (179 existing + new page_scraper + path_builder tests)

- [ ] **Step 5: Commit**

```bash
git add src/serve.py
git commit -m "feat: screenshot display in run results + path builder as default view"
```
