# Dual-Backend Scenario Runner

**Date:** 2026-04-11
**Status:** Approved

## Problem

The scenario runner is tightly coupled to Playwright. Playwright sometimes fails (screen share dialogs, browser launch issues, environment quirks). Chrome DevTools MCP is available in every Claude Code session and connects to an already-running browser, making it a reliable fallback. Supporting both backends means the same manifest can be run against either, and if one fails the other may still produce results.

## Design

### Backend Protocol

A `Backend` protocol defines the interface both implementations share. Lives in a new file `tests/scenario_backend.py`.

```python
class Backend(Protocol):
    def navigate(self, url: str) -> None: ...
    def reload(self) -> None: ...
    def click(self, target: dict, ctx: ScenarioContext) -> None: ...
    def dblclick(self, target: dict, ctx: ScenarioContext) -> None: ...
    def fill(self, target: dict, value: str, ctx: ScenarioContext) -> None: ...
    def select(self, target: dict, value: str, ctx: ScenarioContext) -> None: ...
    def press(self, target: dict, key: str, ctx: ScenarioContext) -> None: ...
    def wait_for_visible(self, target: dict, timeout: int, ctx: ScenarioContext) -> None: ...
    def wait_for_hidden(self, target: dict, timeout: int, ctx: ScenarioContext) -> None: ...
    def wait_for_text(self, text: str, timeout: int) -> None: ...
    def screenshot(self, path: str, full_page: bool = False) -> str: ...
    def wait_for_settled(self) -> None: ...
    def evaluate(self, js: str) -> Any: ...
    def get_text(self, target: dict, ctx: ScenarioContext) -> str: ...
```

### PlaywrightBackend

Wraps existing Playwright Page/Locator logic. Extracted from current `_do_*` handlers and `_resolve_target()` in `scenario_runner.py`. All current behavior preserved — this is a refactor, not a rewrite.

- Target resolution: `css`, `text`, `testid`, `role`, `title`, `seed_ref` — all existing strategies
- Auto-wait: Playwright's built-in actionability checks
- Settlement: Existing mutation-observer JS + loading indicator checks
- Screenshots: `page.screenshot(path=..., full_page=...)`

### CDPBackend

Connects to an **already-running browser** via Playwright's `browser_type.connect_over_cdp(endpoint_url)`. This reuses Playwright's full API (locators, screenshots, auto-wait) but skips browser launch entirely — no flags, no headless mode, no launch failures.

**How it works:** Chrome DevTools MCP is already connected to a browser via CDP. The CDPBackend connects to that same browser's CDP endpoint (typically `http://localhost:9222`). Playwright's CDP connection gives us the same `Page`, `Locator`, and screenshot APIs as the Playwright backend — the difference is purely in how the browser is obtained.

**Why this is better than raw MCP tool calls:** MCP tools are invoked by Claude Code, not by Python scripts. Calling them from pytest would require shelling out or building an MCP client. Using Playwright's CDP connection gives us the same reliability while keeping the implementation simple and testable.

**Target resolution:** Same strategies as PlaywrightBackend (`css`, `text`, `testid`, `role`, `title`, `seed_ref`). Since we're using Playwright's locator API in both cases, all target types work identically.

**Key differences from PlaywrightBackend:**
- No browser launch (connects to existing)
- No custom Chrome flags
- Browser state persists between runs (tabs, cookies, etc.)
- The browser must already be running with `--remote-debugging-port=9222`

**Multi-actor:** Each actor gets a new BrowserContext on the connected browser, same as PlaywrightBackend.

### ScenarioContext Changes

Current:
```python
class ScenarioContext:
    browser: Browser
    def get_actor_page(self, actor: str) -> Page
```

Refactored:
```python
class ScenarioContext:
    backend_type: str  # "playwright" | "cdp"
    # browser only populated for playwright
    browser: Browser | None
    def get_actor_backend(self, actor: str) -> Backend
```

- For Playwright: creates BrowserContext + Page per actor, wraps in `PlaywrightBackend`
- For CDP: connects to existing browser via `connect_over_cdp()`, creates BrowserContext + Page, wraps in `CDPBackend`

### Action Handler Simplification

Current handlers call Playwright directly:
```python
def _do_click(page, step, ctx):
    target = _resolve_target(page, step["target"], ctx)
    target.click()
```

Refactored handlers call the backend:
```python
def _do_click(backend, step, ctx):
    backend.click(step["target"], ctx)
```

The `_ACTION_HANDLERS` dispatch table stays. The execute loop changes from passing `page` to passing `backend`.

### CLI Flag

- `--backend=playwright` (default) or `--backend=cdp`
- Added as a pytest custom option in `conftest.py`
- Passed through to `ScenarioContext` construction

### RunResult

```python
@dataclass
class RunResult:
    status: str           # "passed" | "failed"
    backend: str          # "playwright" | "cdp"
    screenshots: list[str]
    error: str | None
    duration_ms: int
```

The `backend` field enables the gallery/summary to distinguish which backend produced each set of screenshots. When both backends run against the same manifest, results can be displayed side by side.

### Manifest Format

**No changes.** Existing manifests work with both backends. Target types (`css`, `text`, `testid`, `role`, `title`, `seed_ref`) are translated by each backend internally.

## Files

| File | Action |
|------|--------|
| `tests/scenario_backend.py` | **Create** — Backend protocol + PlaywrightBackend + CDPBackend |
| `tests/scenario_runner.py` | **Modify** — Refactor handlers to use Backend, update execute loop |
| `tests/conftest.py` | **Modify** — Add `--backend` CLI option, pass to context |
| `tests/test_scenarios.py` | **Modify** — Pass backend type through to ScenarioContext |

## Verification

1. Run existing scenarios with `--backend=playwright` — all should pass identically to current behavior (regression check)
2. Start Chrome with `--remote-debugging-port=9222`, run a scenario with `--backend=cdp` — verify it connects and produces screenshots
3. Run the same scenario with both backends and verify both produce screenshots
4. Verify all target types work identically across both backends
5. Test error handling — a failing step on one backend shouldn't affect the other's results
6. Test that `--backend=cdp` fails gracefully with a clear message if no browser is running on port 9222
