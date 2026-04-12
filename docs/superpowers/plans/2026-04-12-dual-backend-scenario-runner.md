# Dual-Backend Scenario Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow scenario manifests to run against either a Playwright-launched browser or an already-running browser via CDP connection, selected by a `--backend` CLI flag.

**Architecture:** Extract a `Backend` protocol from `scenario_runner.py`. `PlaywrightBackend` wraps the current launch-based flow. `CDPBackend` uses Playwright's `connect_over_cdp()` to attach to an existing browser at `http://localhost:9222`. Action handlers become thin dispatchers that call backend methods. Manifests are unchanged — same target types work with both backends.

**Tech Stack:** Python 3.10+, Playwright (sync API), pytest

**Spec:** `docs/superpowers/specs/2026-04-11-dual-backend-scenario-runner-design.md`

---

## File Structure

| File | Role |
|------|------|
| `tests/scenario_backend.py` | **New.** Backend protocol + PlaywrightBackend + CDPBackend. ~250 lines. Owns target resolution, settlement, screenshot capture. |
| `tests/scenario_runner.py` | **Modified.** Action handlers become dispatchers. `ScenarioContext` holds a backend factory instead of a raw browser. Target resolution and settlement move out. ~200 lines after. |
| `tests/conftest.py` | **Modified.** Add `--backend` pytest option. Browser fixture returns either a launched browser (playwright) or a connected browser (cdp) based on the flag. |
| `tests/test_scenarios.py` | **Modified.** Pass backend type through to `ScenarioContext`. |
| `tests/test_tdd_scenario_backend.py` | **New.** Unit tests for the Backend protocol and target resolution — no live browser. |

---

## Task 1: Create Backend protocol skeleton

**Files:**
- Create: `tests/scenario_backend.py`
- Create: `tests/test_tdd_scenario_backend.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tdd_scenario_backend.py`:

```python
"""TDD tests for scenario Backend protocol (no live browser required)."""
from __future__ import annotations

import pytest


def test_backend_protocol_defines_required_methods():
    """Backend protocol must define all action methods."""
    from scenario_backend import Backend

    required = [
        "navigate", "reload", "click", "dblclick", "fill", "select", "press",
        "wait_for_visible", "wait_for_hidden", "wait_for_text",
        "screenshot", "wait_for_settled", "evaluate", "get_text", "close",
    ]
    for name in required:
        assert hasattr(Backend, name), f"Backend missing method: {name}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scenario_backend'`

- [ ] **Step 3: Write minimal implementation**

Create `tests/scenario_backend.py`:

```python
"""Backend protocol and implementations for the scenario runner.

A Backend abstracts browser interaction so scenarios can run against either
a Playwright-launched browser (PlaywrightBackend) or an already-running
browser connected via CDP (CDPBackend).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Backend(Protocol):
    """Protocol every scenario backend must implement."""

    def navigate(self, url: str) -> None: ...
    def reload(self) -> None: ...
    def click(self, target: dict, seed_id_map: dict) -> None: ...
    def dblclick(self, target: dict, seed_id_map: dict) -> None: ...
    def fill(self, target: dict, value: str, seed_id_map: dict) -> None: ...
    def select(self, target: dict, value: str, seed_id_map: dict) -> None: ...
    def press(self, target: dict, key: str, seed_id_map: dict) -> None: ...
    def wait_for_visible(self, target: dict, timeout: int, seed_id_map: dict) -> None: ...
    def wait_for_hidden(self, target: dict, timeout: int, seed_id_map: dict) -> None: ...
    def wait_for_text(self, text: str, timeout: int) -> None: ...
    def screenshot(self, path: str, full_page: bool = False) -> str: ...
    def wait_for_settled(self, timeout: int = 5000) -> None: ...
    def evaluate(self, js: str) -> Any: ...
    def get_text(self, target: dict, seed_id_map: dict) -> str: ...
    def close(self) -> None: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/scenario_backend.py tests/test_tdd_scenario_backend.py
git commit -m "feat(scenarios): define Backend protocol skeleton"
```

---

## Task 2: Extract target resolution into backend module

**Files:**
- Modify: `tests/scenario_backend.py` — add `_resolve_target()` function
- Modify: `tests/test_tdd_scenario_backend.py` — add resolution tests

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tdd_scenario_backend.py`:

```python
def test_resolve_target_css():
    """CSS targets should route to page.locator()."""
    from scenario_backend import resolve_target

    class FakePage:
        def locator(self, selector):
            return ("locator", selector)

    page = FakePage()
    result = resolve_target(page, {"css": ".my-class"}, {})
    assert result == ("locator", ".my-class")


def test_resolve_target_testid():
    """testid targets should route to get_by_test_id()."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_test_id(self, tid):
            return ("testid", tid)

    result = resolve_target(FakePage(), {"testid": "submit"}, {})
    assert result == ("testid", "submit")


def test_resolve_target_title_via_seed_map():
    """title targets should look up ticket id in seed_id_map."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_test_id(self, tid):
            return ("testid", tid)

    seed_map = {"My Ticket": "B-42"}
    result = resolve_target(FakePage(), {"title": "My Ticket"}, seed_map)
    assert result == ("testid", "ticket-card-B-42")


def test_resolve_target_title_with_open_flag():
    """title + open:true should resolve to the card open button."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_test_id(self, tid):
            return ("testid", tid)

    seed_map = {"My Ticket": "B-42"}
    result = resolve_target(
        FakePage(), {"title": "My Ticket", "open": True}, seed_map
    )
    assert result == ("testid", "card-open-btn-B-42")


def test_resolve_target_seed_ref():
    """seed_ref ticket-0 should index into seed map values."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_test_id(self, tid):
            return ("testid", tid)

    seed_map = {"First": "B-01", "Second": "B-02"}
    result = resolve_target(FakePage(), {"seed_ref": "ticket-1"}, seed_map)
    assert result == ("testid", "ticket-card-B-02")


def test_resolve_target_text():
    """text targets should use get_by_text()."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_text(self, text, exact):
            return ("text", text, exact)

    result = resolve_target(FakePage(), {"text": "Save"}, {})
    assert result == ("text", "Save", False)


def test_resolve_target_role():
    """role targets should use get_by_role()."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_role(self, role, name):
            return ("role", role, name)

    result = resolve_target(
        FakePage(), {"role": "button", "name": "Cancel"}, {}
    )
    assert result == ("role", "button", "Cancel")


def test_resolve_target_unknown_raises():
    """Unknown target descriptors should raise ValueError."""
    from scenario_backend import resolve_target

    with pytest.raises(ValueError, match="Unrecognised target"):
        resolve_target(None, {"weird": "thing"}, {})


def test_resolve_target_title_missing_raises():
    """Missing title in seed map should raise ValueError."""
    from scenario_backend import resolve_target

    with pytest.raises(ValueError, match="not found in seed_id_map"):
        resolve_target(None, {"title": "Nope"}, {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: 8 FAILs with `ImportError: cannot import name 'resolve_target'`

- [ ] **Step 3: Write minimal implementation**

Append to `tests/scenario_backend.py`:

```python
def resolve_target(page: Any, target: dict, seed_id_map: dict) -> Any:
    """Return a Playwright Locator (or equivalent) for a target descriptor.

    Both PlaywrightBackend and CDPBackend use this since both wrap
    Playwright page objects. seed_id_map maps ticket titles (and
    "ticket-N" positional refs) to ticket IDs.

    Supported keys: testid, title, seed_ref, css, text, role.
    """
    if "testid" in target:
        return page.get_by_test_id(target["testid"])

    if "title" in target:
        title = target["title"]
        ticket_id = seed_id_map.get(title)
        if ticket_id is None:
            raise ValueError(
                f"Title {title!r} not found in seed_id_map. "
                f"Available: {list(seed_id_map.keys())}"
            )
        if target.get("open"):
            return page.get_by_test_id(f"card-open-btn-{ticket_id}")
        return page.get_by_test_id(f"ticket-card-{ticket_id}")

    if "seed_ref" in target:
        ref = target["seed_ref"]
        try:
            index = int(ref.split("-")[-1])
        except (ValueError, IndexError):
            raise ValueError(
                f"Invalid seed_ref format: {ref!r}. Expected 'ticket-N'."
            )
        ids = list(seed_id_map.values())
        if index >= len(ids):
            raise ValueError(
                f"seed_ref index {index} out of range "
                f"(have {len(ids)} seed tickets)"
            )
        return page.get_by_test_id(f"ticket-card-{ids[index]}")

    if "css" in target:
        return page.locator(target["css"])

    if "text" in target:
        return page.get_by_text(target["text"], exact=False)

    if "role" in target:
        return page.get_by_role(target["role"], name=target.get("name", ""))

    raise ValueError(f"Unrecognised target descriptor: {target!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/scenario_backend.py tests/test_tdd_scenario_backend.py
git commit -m "feat(scenarios): extract target resolution into scenario_backend"
```

---

## Task 3: Implement PlaywrightBackend

**Files:**
- Modify: `tests/scenario_backend.py` — add PlaywrightBackend class
- Modify: `tests/test_tdd_scenario_backend.py` — add instantiation test

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tdd_scenario_backend.py`:

```python
def test_playwright_backend_satisfies_protocol():
    """PlaywrightBackend must satisfy the Backend protocol."""
    from scenario_backend import Backend, PlaywrightBackend

    # Create a minimal fake page/context to instantiate
    class FakePage:
        def goto(self, url): pass
        def reload(self): pass
        def screenshot(self, path, full_page=False): pass
        def wait_for_function(self, *a, **kw): pass
        def wait_for_timeout(self, ms): pass

    class FakeCtx:
        def close(self): pass

    backend = PlaywrightBackend(page=FakePage(), context=FakeCtx())
    assert isinstance(backend, Backend)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py::test_playwright_backend_satisfies_protocol -v`
Expected: FAIL with `ImportError: cannot import name 'PlaywrightBackend'`

- [ ] **Step 3: Write minimal implementation**

Append to `tests/scenario_backend.py`:

```python
# ---------------------------------------------------------------------------
# PlaywrightBackend
# ---------------------------------------------------------------------------

# Settlement JS: wait for no DOM mutations for 300ms
_SETTLED_JS = """
() => {
    return new Promise((resolve) => {
        let timer = null;
        const observer = new MutationObserver(() => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                observer.disconnect();
                resolve(true);
            }, 300);
        });
        observer.observe(document.body, {
            childList: true, subtree: true,
            attributes: true, characterData: true
        });
        timer = setTimeout(() => {
            observer.disconnect();
            resolve(true);
        }, 300);
    });
}
"""

_LOADING_GONE_JS = (
    "document.querySelectorAll('.loading, [aria-busy=\"true\"]').length === 0"
)


class PlaywrightBackend:
    """Backend that drives a Playwright Page directly.

    Construction: pass an already-created Page and its BrowserContext.
    The caller (ScenarioContext.get_actor_backend) is responsible for
    creating the context and page.
    """

    def __init__(self, page: Any, context: Any) -> None:
        self.page = page
        self.context = context

    def navigate(self, url: str) -> None:
        self.page.goto(url)
        self.page.wait_for_load_state("domcontentloaded")

    def reload(self) -> None:
        self.page.reload()
        self.page.wait_for_load_state("domcontentloaded")

    def click(self, target: dict, seed_id_map: dict) -> None:
        resolve_target(self.page, target, seed_id_map).click()

    def dblclick(self, target: dict, seed_id_map: dict) -> None:
        resolve_target(self.page, target, seed_id_map).dblclick()

    def fill(self, target: dict, value: str, seed_id_map: dict) -> None:
        resolve_target(self.page, target, seed_id_map).fill(value)

    def select(self, target: dict, value: str, seed_id_map: dict) -> None:
        resolve_target(self.page, target, seed_id_map).select_option(value)

    def press(self, target: dict, key: str, seed_id_map: dict) -> None:
        resolve_target(self.page, target, seed_id_map).press(key)

    def wait_for_visible(
        self, target: dict, timeout: int, seed_id_map: dict
    ) -> None:
        resolve_target(self.page, target, seed_id_map).wait_for(
            state="visible", timeout=timeout
        )

    def wait_for_hidden(
        self, target: dict, timeout: int, seed_id_map: dict
    ) -> None:
        resolve_target(self.page, target, seed_id_map).wait_for(
            state="hidden", timeout=timeout
        )

    def wait_for_text(self, text: str, timeout: int) -> None:
        self.page.get_by_text(text, exact=False).wait_for(
            state="visible", timeout=timeout
        )

    def screenshot(self, path: str, full_page: bool = False) -> str:
        self.page.screenshot(path=path, full_page=full_page)
        return path

    def wait_for_settled(self, timeout: int = 5000) -> None:
        try:
            self.page.wait_for_function(_SETTLED_JS, timeout=timeout)
        except Exception:
            self.page.wait_for_timeout(500)
        try:
            self.page.wait_for_function(_LOADING_GONE_JS, timeout=2000)
        except Exception:
            pass

    def evaluate(self, js: str) -> Any:
        return self.page.evaluate(js)

    def get_text(self, target: dict, seed_id_map: dict) -> str:
        loc = resolve_target(self.page, target, seed_id_map)
        return loc.text_content() or ""

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/scenario_backend.py tests/test_tdd_scenario_backend.py
git commit -m "feat(scenarios): implement PlaywrightBackend"
```

---

## Task 4: Implement CDPBackend

**Files:**
- Modify: `tests/scenario_backend.py` — add CDPBackend class + factory
- Modify: `tests/test_tdd_scenario_backend.py` — add instantiation test

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tdd_scenario_backend.py`:

```python
def test_cdp_backend_satisfies_protocol():
    """CDPBackend must satisfy the Backend protocol."""
    from scenario_backend import Backend, CDPBackend

    class FakePage:
        def goto(self, url): pass
        def reload(self): pass
        def screenshot(self, path, full_page=False): pass

    class FakeCtx:
        def close(self): pass

    backend = CDPBackend(page=FakePage(), context=FakeCtx())
    assert isinstance(backend, Backend)


def test_connect_cdp_backend_raises_on_unreachable():
    """connect_cdp_backend should raise a clear error if no browser is listening."""
    from scenario_backend import connect_cdp_backend

    with pytest.raises(ConnectionError, match="9999"):
        connect_cdp_backend("http://localhost:9999", timeout_ms=500)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: 2 FAILs — `ImportError: cannot import name 'CDPBackend'`

- [ ] **Step 3: Write minimal implementation**

Append to `tests/scenario_backend.py`:

```python
# ---------------------------------------------------------------------------
# CDPBackend
# ---------------------------------------------------------------------------


class CDPBackend(PlaywrightBackend):
    """Backend that drives a browser via CDP connection.

    Behaviourally identical to PlaywrightBackend — the difference is in
    how the Page/BrowserContext are obtained (connect_over_cdp instead of
    launch). All scenario logic works identically.

    This subclass exists to make the distinction explicit in RunResult
    and to allow future divergence (e.g. CDP-specific error messages).
    """

    # No behavioural override needed — inherits everything from PlaywrightBackend.
    pass


def connect_cdp_backend(
    endpoint_url: str = "http://localhost:9222",
    timeout_ms: int = 5000,
) -> tuple[Any, Any]:
    """Connect to an already-running browser via CDP.

    Returns a (browser, playwright) tuple. The caller owns both and must
    call browser.close() + playwright.stop() on teardown.

    Raises ConnectionError with a clear message if no browser is listening
    on the given endpoint.
    """
    from playwright.sync_api import sync_playwright
    import urllib.error
    import urllib.request

    # Preflight: verify the CDP endpoint is reachable before calling playwright,
    # which gives a less friendly error on connection failure.
    try:
        with urllib.request.urlopen(
            f"{endpoint_url}/json/version", timeout=timeout_ms / 1000
        ) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConnectionError(
            f"Could not reach CDP endpoint at {endpoint_url}. "
            f"Start Chrome with --remote-debugging-port={endpoint_url.rsplit(':', 1)[-1]} "
            f"and try again. Original error: {exc}"
        )

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(endpoint_url)
    except Exception:
        pw.stop()
        raise
    return browser, pw
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: 12 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/scenario_backend.py tests/test_tdd_scenario_backend.py
git commit -m "feat(scenarios): implement CDPBackend with connect_over_cdp"
```

---

## Task 5: Refactor ScenarioContext to use backends

**Files:**
- Modify: `tests/scenario_runner.py` — update ScenarioContext
- Modify: `tests/test_tdd_scenario_backend.py` — add ScenarioContext tests

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tdd_scenario_backend.py`:

```python
def test_scenario_context_creates_playwright_backend():
    """ScenarioContext should create PlaywrightBackend when backend='playwright'."""
    from scenario_backend import PlaywrightBackend
    from scenario_runner import ScenarioContext

    class FakePage:
        pass

    class FakeBrowserCtx:
        def new_page(self):
            return FakePage()
        def close(self):
            pass

    class FakeBrowser:
        def new_context(self):
            return FakeBrowserCtx()

    ctx = ScenarioContext(
        base_url="http://localhost:8000",
        browser=FakeBrowser(),
        output_dir="/tmp",
        manifest={"id": "test"},
        backend_type="playwright",
    )
    backend = ctx.get_actor_backend("default")
    assert isinstance(backend, PlaywrightBackend)
    # Same actor should return same backend
    assert ctx.get_actor_backend("default") is backend
    # Different actor should create a new backend
    other = ctx.get_actor_backend("other")
    assert other is not backend


def test_scenario_context_close_all_closes_backends():
    """close_all() should close every actor backend."""
    from scenario_runner import ScenarioContext

    closed = []

    class FakePage:
        pass

    class FakeBrowserCtx:
        def new_page(self):
            return FakePage()
        def close(self):
            closed.append(True)

    class FakeBrowser:
        def new_context(self):
            return FakeBrowserCtx()

    ctx = ScenarioContext(
        base_url="http://localhost:8000",
        browser=FakeBrowser(),
        output_dir="/tmp",
        manifest={"id": "test"},
        backend_type="playwright",
    )
    ctx.get_actor_backend("a")
    ctx.get_actor_backend("b")
    ctx.close_all()
    assert len(closed) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: 2 FAILs — `TypeError: unexpected keyword 'backend_type'`

- [ ] **Step 3: Replace ScenarioContext in scenario_runner.py**

Replace `tests/scenario_runner.py` lines 1-71 with:

```python
"""Manifest-driven scenario runner with pluggable backends."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from scenario_backend import (
    Backend,
    CDPBackend,
    PlaywrightBackend,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    scenario_id: str
    status: str  # "passed" | "failed" | "error"
    duration_ms: int
    backend: str = "playwright"  # "playwright" | "cdp"
    failed_step: dict | None = None
    failed_step_index: int | None = None
    screenshots: list[str] = field(default_factory=list)
    error_message: str = ""


# ---------------------------------------------------------------------------
# ScenarioContext
# ---------------------------------------------------------------------------


class ScenarioContext:
    """Holds runtime state for a single scenario run.

    The ``browser`` argument is a Playwright Browser instance — either
    launched locally (backend_type="playwright") or obtained via
    connect_over_cdp() (backend_type="cdp"). Both produce compatible
    Page objects; the backend_type determines which Backend class
    wraps them.
    """

    def __init__(
        self,
        base_url: str,
        browser: Any,
        output_dir: str,
        manifest: dict[str, Any],
        seed_id_map: dict[str, str] | None = None,
        backend_type: str = "playwright",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.browser = browser
        self.output_dir = output_dir
        self.manifest = manifest
        self.backend_type = backend_type
        self.seed_id_map: dict[str, str] = seed_id_map or {}
        self._actor_backends: dict[str, Backend] = {}

    def get_actor_backend(self, actor_name: str) -> Backend:
        """Return the existing Backend for actor_name, or create a new one."""
        if actor_name not in self._actor_backends:
            context = self.browser.new_context()
            page = context.new_page()
            if self.backend_type == "cdp":
                backend = CDPBackend(page=page, context=context)
            else:
                backend = PlaywrightBackend(page=page, context=context)
            self._actor_backends[actor_name] = backend
        return self._actor_backends[actor_name]

    def close_all(self) -> None:
        """Close every actor backend."""
        for backend in self._actor_backends.values():
            backend.close()
        self._actor_backends.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: 14 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/scenario_runner.py tests/test_tdd_scenario_backend.py
git commit -m "feat(scenarios): refactor ScenarioContext to use backends"
```

---

## Task 6: Refactor action handlers to use backends

**Files:**
- Modify: `tests/scenario_runner.py` — replace handlers and execute loop

- [ ] **Step 1: Replace handler section**

Replace everything from `# Target resolution` (line 73) through the end of file with:

```python
# ---------------------------------------------------------------------------
# Post-step inline assertions
# ---------------------------------------------------------------------------


def _run_inline_assert(
    backend: Backend, assert_spec: dict[str, Any], ctx: ScenarioContext
) -> None:
    """Run an inline assert block attached to a step."""
    timeout = assert_spec.get("timeout", 5000)

    if "text_visible" in assert_spec:
        backend.wait_for_text(assert_spec["text_visible"], timeout=timeout)

    if "element_visible" in assert_spec:
        backend.wait_for_visible(
            assert_spec["element_visible"], timeout=timeout,
            seed_id_map=ctx.seed_id_map,
        )

    if "element_hidden" in assert_spec:
        backend.wait_for_hidden(
            assert_spec["element_hidden"], timeout=timeout,
            seed_id_map=ctx.seed_id_map,
        )


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _do_open(backend: Backend, step: dict, ctx: ScenarioContext) -> None:
    path = step.get("path", "/")
    backend.navigate(ctx.base_url + path)


def _do_reload(backend: Backend, step: dict, ctx: ScenarioContext) -> None:
    backend.reload()


def _do_click(backend: Backend, step: dict, ctx: ScenarioContext) -> None:
    backend.click(step["target"], ctx.seed_id_map)


def _do_double_click(backend: Backend, step: dict, ctx: ScenarioContext) -> None:
    backend.dblclick(step["target"], ctx.seed_id_map)


def _do_fill(backend: Backend, step: dict, ctx: ScenarioContext) -> None:
    backend.fill(step["target"], step["value"], ctx.seed_id_map)


def _do_select(backend: Backend, step: dict, ctx: ScenarioContext) -> None:
    backend.select(step["target"], step["value"], ctx.seed_id_map)


def _do_press(backend: Backend, step: dict, ctx: ScenarioContext) -> None:
    backend.press(step["target"], step["key"], ctx.seed_id_map)


def _do_wait_for(backend: Backend, step: dict, ctx: ScenarioContext) -> None:
    timeout = step.get("timeout", 10000)
    state = step.get("state", "visible")
    if state == "hidden":
        backend.wait_for_hidden(step["target"], timeout, ctx.seed_id_map)
    else:
        backend.wait_for_visible(step["target"], timeout, ctx.seed_id_map)


def _do_assert_visible(backend: Backend, step: dict, ctx: ScenarioContext) -> None:
    timeout = step.get("timeout", 10000)
    backend.wait_for_visible(step["target"], timeout, ctx.seed_id_map)


def _do_assert_text(backend: Backend, step: dict, ctx: ScenarioContext) -> None:
    timeout = step.get("timeout", 10000)
    backend.wait_for_text(step["text"], timeout=timeout)


def _do_capture(backend: Backend, step: dict, ctx: ScenarioContext) -> str:
    """Wait for settled UI, take a screenshot, save to output_dir.

    Returns the absolute path of the saved file.
    """
    cap = step.get("capture", step)
    backend.wait_for_settled(timeout=cap.get("settle_timeout", 5000))

    label = cap.get("name") or cap.get("label", "screenshot")
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    path = os.path.join(ctx.output_dir, f"{safe_label}.png")

    full_page = cap.get("full_page", False)
    return backend.screenshot(path=path, full_page=full_page)


# ---------------------------------------------------------------------------
# Action dispatch table
# ---------------------------------------------------------------------------

_ACTION_HANDLERS: dict[str, Any] = {
    "open": _do_open,
    "reload": _do_reload,
    "click": _do_click,
    "double_click": _do_double_click,
    "dblclick": _do_double_click,
    "fill": _do_fill,
    "select": _do_select,
    "press": _do_press,
    "wait_for": _do_wait_for,
    "assert_visible": _do_assert_visible,
    "assert_text": _do_assert_text,
    "capture": _do_capture,
}


# ---------------------------------------------------------------------------
# Main execution engine
# ---------------------------------------------------------------------------


def execute_scenario(context: ScenarioContext) -> RunResult:
    """Execute all steps in the manifest sequentially."""
    manifest = context.manifest
    scenario_id = manifest["id"]
    steps = manifest.get("steps", [])

    screenshots: list[str] = []
    start_ms = int(time.monotonic() * 1000)
    default_actor = manifest.get("actor", "default")

    for step_index, step in enumerate(steps):
        action = step.get("action")
        if not action:
            raise ValueError(f"Step {step_index} has no 'action' key: {step!r}")

        actor = step.get("actor", default_actor)
        backend = context.get_actor_backend(actor)

        handler = _ACTION_HANDLERS.get(action)
        if handler is None:
            raise ValueError(
                f"Unknown action {action!r} in step {step_index}. "
                f"Valid actions: {sorted(_ACTION_HANDLERS)}"
            )

        try:
            result = handler(backend, step, context)

            if action == "capture" and isinstance(result, str):
                screenshots.append(result)

            if "assert" in step:
                _run_inline_assert(backend, step["assert"], context)

            if "capture" in step and action != "capture":
                cap_spec = step["capture"]
                if isinstance(cap_spec, dict):
                    cap_path = _do_capture(backend, cap_spec, context)
                    screenshots.append(cap_path)

        except Exception as exc:
            failure_path = os.path.join(
                context.output_dir, f"FAILURE-step-{step_index:02d}.png"
            )
            try:
                backend.screenshot(path=failure_path)
                screenshots.append(failure_path)
            except Exception:
                pass

            end_ms = int(time.monotonic() * 1000)
            result = RunResult(
                scenario_id=scenario_id,
                status="failed",
                duration_ms=end_ms - start_ms,
                backend=context.backend_type,
                failed_step=step,
                failed_step_index=step_index,
                screenshots=screenshots,
                error_message=str(exc),
            )
            exc.__run_result__ = result  # type: ignore[attr-defined]
            raise

    end_ms = int(time.monotonic() * 1000)
    return RunResult(
        scenario_id=scenario_id,
        status="passed",
        duration_ms=end_ms - start_ms,
        backend=context.backend_type,
        screenshots=screenshots,
    )
```

- [ ] **Step 2: Verify module still imports**

Run: `cd ~/projects/ticket-takeaway && python3 -c "import sys; sys.path.insert(0, 'tests'); import scenario_runner; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Run existing TDD tests**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: 14 PASS

- [ ] **Step 4: Commit**

```bash
git add tests/scenario_runner.py
git commit -m "feat(scenarios): refactor handlers to use Backend interface"
```

---

## Task 7: Add --backend CLI flag to conftest

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add option registration**

In `tests/conftest.py`, replace the `pytest_addoption` function (lines 66-84) with:

```python
def pytest_addoption(parser):
    """Register custom CLI options for the scenario runner."""
    parser.addoption(
        "--scenario-id",
        action="store",
        default=None,
        metavar="ID",
        help="Run only the scenario with this exact ID (skips all others).",
    )
    parser.addoption(
        "--publish",
        action="store_true",
        default=False,
        help=(
            "After each scenario run, write a run-summary.json file "
            "alongside the captured screenshots (for gallery publishing)."
        ),
    )
    parser.addoption(
        "--backend",
        action="store",
        default="playwright",
        choices=["playwright", "cdp"],
        help=(
            "Which backend to run scenarios against. "
            "'playwright' launches a new browser (default). "
            "'cdp' connects to an already-running browser on "
            "http://localhost:9222 (start Chrome with "
            "--remote-debugging-port=9222)."
        ),
    )
    parser.addoption(
        "--cdp-endpoint",
        action="store",
        default="http://localhost:9222",
        help="CDP endpoint URL when --backend=cdp.",
    )
```

- [ ] **Step 2: Update the browser fixture**

Replace the `browser` fixture (lines 133-140) with:

```python
@pytest.fixture(scope="session")
def browser(request):
    """Session-scoped browser — either launched locally or CDP-connected.

    Controlled by --backend (default: playwright). When --backend=cdp,
    connects to an already-running browser at --cdp-endpoint.
    """
    backend_type = request.config.getoption("--backend", default="playwright")

    if backend_type == "cdp":
        from scenario_backend import connect_cdp_backend
        endpoint = request.config.getoption(
            "--cdp-endpoint", default="http://localhost:9222"
        )
        b, pw = connect_cdp_backend(endpoint)
        yield b
        b.close()
        pw.stop()
    else:
        pw = sync_playwright().start()
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()
        pw.stop()
```

- [ ] **Step 3: Verify options register cleanly**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest --help 2>&1 | grep -A1 backend`
Expected: Shows `--backend` and `--cdp-endpoint` options

- [ ] **Step 4: Verify Playwright backend still works (smoke test)**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_tdd_scenario_backend.py -v`
Expected: 14 PASS (TDD tests don't need a live browser)

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py
git commit -m "feat(scenarios): add --backend CLI flag"
```

---

## Task 8: Pass backend type through test_scenarios.py

**Files:**
- Modify: `tests/test_scenarios.py`

- [ ] **Step 1: Update ScenarioContext construction**

In `tests/test_scenarios.py`, replace the `ctx = ScenarioContext(...)` block (around line 118-124) with:

```python
    backend_type = request.config.getoption("--backend", default="playwright")

    ctx = ScenarioContext(
        base_url=dashboard_server,
        browser=browser,
        output_dir=screenshots_dir,
        manifest=manifest,
        seed_id_map=seed_id_map,
        backend_type=backend_type,
    )
```

- [ ] **Step 2: Update the summary dict**

Find the `summary = { ... }` block (around line 134-141) and add `"backend": result.backend`:

```python
    summary = {
        "scenario_id": result.scenario_id,
        "status": result.status,
        "backend": result.backend,
        "duration_ms": result.duration_ms,
        "screenshots": result.screenshots,
        "failed_step_index": result.failed_step_index,
        "error_message": result.error_message,
    }
```

- [ ] **Step 3: Run an existing scenario with default backend**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_scenarios.py --scenario-id=quick-edit-detail-overlay -v 2>&1 | tail -20`
Expected: PASS (regression check — existing Playwright path should work identically)

- [ ] **Step 4: Verify summary includes backend field**

Run: `cd ~/projects/ticket-takeaway && ls -t .artifacts/scenarios/ | head -1 | xargs -I{} cat .artifacts/scenarios/{}/summary.json`
Expected: Output shows `"backend": "playwright"`

- [ ] **Step 5: Commit**

```bash
git add tests/test_scenarios.py
git commit -m "feat(scenarios): pass backend type through test_scenarios"
```

---

## Task 9: Manual CDP smoke test

**Files:** None modified — this task verifies the full CDP path end-to-end.

- [ ] **Step 1: Start Chrome with remote debugging**

In a separate terminal the user runs (or ask the user to run):

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/cdp-profile &
# or on Mac:
# /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
#   --remote-debugging-port=9222 --user-data-dir=/tmp/cdp-profile
```

Verify the endpoint is reachable:

```bash
curl -s http://localhost:9222/json/version | head
```

Expected: JSON response with browser info.

- [ ] **Step 2: Run a scenario against CDP backend**

Run: `cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_scenarios.py --scenario-id=quick-edit-detail-overlay --backend=cdp -v 2>&1 | tail -30`

Expected: PASS. The scenario runs in the Chrome window the user opened.

- [ ] **Step 3: Verify summary shows cdp backend**

Run: `cd ~/projects/ticket-takeaway && ls -t .artifacts/scenarios/ | head -1 | xargs -I{} cat .artifacts/scenarios/{}/summary.json`
Expected: Output shows `"backend": "cdp"`

- [ ] **Step 4: Verify clear error when CDP unreachable**

Stop Chrome (close the window), then run:

```bash
cd ~/projects/ticket-takeaway && python3 -m pytest tests/test_scenarios.py --scenario-id=quick-edit-detail-overlay --backend=cdp -v 2>&1 | tail -10
```

Expected: FAIL with error message containing "Could not reach CDP endpoint at http://localhost:9222".

- [ ] **Step 5: Commit (nothing to commit — verification only)**

No commit needed. If any step fails, go back and fix before proceeding.

---

## Task 10: Update dashboard ticket and docs

**Files:**
- Modify: `CLAUDE.md` — note the `--backend` flag in the Scenario Runner section

- [ ] **Step 1: Add --backend note to CLAUDE.md**

Find the `## Scenario Runner` section in `CLAUDE.md` and add this below the existing examples:

```markdown
# Run against an already-running Chrome (CDP mode)
# First start Chrome with: google-chrome --remote-debugging-port=9222
python3 -m pytest tests/test_scenarios.py -v --backend=cdp
python3 -m pytest tests/test_scenarios.py -v --backend=cdp --cdp-endpoint=http://localhost:9333
```

- [ ] **Step 2: Move the ticket to Review**

Run: `python3 ~/.claude/ticket-takeaway/tickets-cli.py add ticket-takeaway "Dual-backend scenario runner (Playwright + CDP)" --section review --priority medium`

(Or if a ticket already exists, move it: `python3 ~/.claude/ticket-takeaway/tickets-cli.py move ticket-takeaway <ID> review`)

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md PRODUCT_BACKLOG.md
git commit -m "docs: document --backend flag for scenario runner"
```

---

## Self-Review

**Spec coverage:**
- Backend protocol → Task 1 ✓
- PlaywrightBackend → Task 3 ✓
- CDPBackend with connect_over_cdp → Task 4 ✓
- ScenarioContext backend factory → Task 5 ✓
- Action handler simplification → Task 6 ✓
- CLI flag → Task 7 ✓
- RunResult.backend field → Task 5 ✓
- Manifest format unchanged → Verified in Task 8/9 regression ✓
- Target translation unchanged → Task 2 (shared resolve_target) ✓
- Clear error on unreachable CDP → Task 4 + Task 9 ✓

**Placeholder scan:** No "TBD"/"TODO" in steps; every code change has full code inline.

**Type consistency:** Backend protocol methods consistent across tasks. `backend.click(target, seed_id_map)` signature matches in Task 1, 3, 4, 6. `ScenarioContext.get_actor_backend(name)` consistent in Tasks 5, 6, 8.

**Risks & mitigations:**
- **CDP preflight check uses urllib** — 500ms timeout prevents hangs when the endpoint is unreachable
- **CDP browser state persists across runs** — tests may see stale tabs; this is acceptable since it's a manual-use feature
- **Playwright regression** — Task 8 Step 3 runs an existing scenario through the refactored path to catch any breakage
