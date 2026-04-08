"""Manifest-driven scenario runner for Playwright-based UI scenarios."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Locator, Page


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    scenario_id: str
    status: str  # "passed" | "failed" | "error"
    duration_ms: int
    failed_step: dict | None = None
    failed_step_index: int | None = None
    screenshots: list[str] = field(default_factory=list)
    error_message: str = ""


# ---------------------------------------------------------------------------
# ScenarioContext
# ---------------------------------------------------------------------------


class ScenarioContext:
    """Holds runtime state for a single scenario run."""

    def __init__(
        self,
        base_url: str,
        browser: Browser,
        output_dir: str,
        manifest: dict[str, Any],
        seed_id_map: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.browser = browser
        self.output_dir = output_dir
        self.manifest = manifest
        # title -> ticket-id mapping (e.g. "My Ticket" -> "B-01")
        self.seed_id_map: dict[str, str] = seed_id_map or {}
        # actor name -> (BrowserContext, Page)
        self._actor_contexts: dict[str, tuple[BrowserContext, Page]] = {}

    def get_actor_page(self, actor_name: str) -> Page:
        """Return the existing Page for actor_name, or create a new one."""
        if actor_name not in self._actor_contexts:
            vp = self.manifest.get("viewport", {})
            ctx = self.browser.new_context(
                viewport={"width": vp.get("width", 1440), "height": vp.get("height", 1024)}
            )
            page = ctx.new_page()
            # Pre-set theme in localStorage before any navigation
            theme = self.manifest.get("theme")
            if theme:
                page.goto("about:blank")
                # We need to navigate to the actual origin first to set localStorage
                page.goto(self.base_url)
                page.evaluate(f"localStorage.setItem('tt-theme', '{theme}')")
                page.reload()
                page.wait_for_load_state("domcontentloaded")
            self._actor_contexts[actor_name] = (ctx, page)
        _, page = self._actor_contexts[actor_name]
        return page

    def close_all(self) -> None:
        """Close every actor BrowserContext (and its pages)."""
        for ctx, _ in self._actor_contexts.values():
            try:
                ctx.close()
            except Exception:
                pass
        self._actor_contexts.clear()


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _resolve_target(page: Page, target: dict[str, Any], ctx: ScenarioContext) -> Locator:
    """Return a Playwright Locator based on a target descriptor dict.

    Supported keys:
      testid     — page.get_by_test_id(value)
      title      — look up title in ctx.seed_id_map, then get card by ticket id
      seed_ref   — "ticket-N" positional reference into seed_id_map values
      css        — page.locator(value)
      text       — page.get_by_text(value, exact=False)
      role       — {"role": "button", "name": "Save"} -> get_by_role
    """
    if "testid" in target:
        return page.get_by_test_id(target["testid"])

    if "title" in target:
        title = target["title"]
        ticket_id = ctx.seed_id_map.get(title)
        if ticket_id is None:
            raise ValueError(
                f"Title {title!r} not found in seed_id_map. "
                f"Available: {list(ctx.seed_id_map.keys())}"
            )
        # When target includes "open": true, resolve to the open button
        # instead of the card root. Default to card root.
        if target.get("open"):
            return page.get_by_test_id(f"card-open-btn-{ticket_id}")
        return page.get_by_test_id(f"ticket-card-{ticket_id}")

    if "seed_ref" in target:
        ref = target["seed_ref"]  # e.g. "ticket-0"
        try:
            index = int(ref.split("-")[-1])
        except (ValueError, IndexError):
            raise ValueError(f"Invalid seed_ref format: {ref!r}. Expected 'ticket-N'.")
        ids = list(ctx.seed_id_map.values())
        if index >= len(ids):
            raise ValueError(
                f"seed_ref index {index} out of range (have {len(ids)} seed tickets)"
            )
        ticket_id = ids[index]
        return page.get_by_test_id(f"ticket-card-{ticket_id}")

    if "css" in target:
        return page.locator(target["css"])

    if "text" in target:
        return page.get_by_text(target["text"], exact=False)

    if "role" in target:
        name = target.get("name", "")
        return page.get_by_role(target["role"], name=name)

    raise ValueError(f"Unrecognised target descriptor: {target!r}")


# ---------------------------------------------------------------------------
# UI settling helper
# ---------------------------------------------------------------------------


def _wait_for_settled(page: Page, timeout: int = 5000) -> None:
    """Wait for the UI to stop mutating the DOM.

    Strategy: poll until no DOM mutations occur for 300 ms, with a generous
    outer timeout. Falls back to a simple 500 ms sleep on any JS error so the
    runner stays robust against pages that lack mutation-observer support.
    """
    try:
        page.wait_for_function(
            """
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
                    // Kick off the timer immediately too — if nothing mutates
                    // within 300 ms we're already settled.
                    timer = setTimeout(() => {
                        observer.disconnect();
                        resolve(true);
                    }, 300);
                });
            }
            """,
            timeout=timeout,
        )
    except Exception:
        # Fallback: if the page can't run the observer (e.g. error page), just wait.
        page.wait_for_timeout(500)

    # Also wait for any explicit loading indicators to clear.
    try:
        page.wait_for_function(
            "document.querySelectorAll('.loading, [aria-busy=\"true\"]').length === 0",
            timeout=2000,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Post-step inline assertions
# ---------------------------------------------------------------------------


def _run_inline_assert(page: Page, assert_spec: dict[str, Any], ctx: ScenarioContext) -> None:
    """Run an inline assert block attached to a step."""
    if "text_visible" in assert_spec:
        text = assert_spec["text_visible"]
        locator = page.get_by_text(text, exact=False)
        locator.wait_for(state="visible", timeout=assert_spec.get("timeout", 5000))

    if "element_visible" in assert_spec:
        target = assert_spec["element_visible"]
        loc = _resolve_target(page, target, ctx)
        loc.wait_for(state="visible", timeout=assert_spec.get("timeout", 5000))

    if "element_hidden" in assert_spec:
        target = assert_spec["element_hidden"]
        loc = _resolve_target(page, target, ctx)
        loc.wait_for(state="hidden", timeout=assert_spec.get("timeout", 5000))


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _do_open(page: Page, step: dict[str, Any], ctx: ScenarioContext) -> None:
    path = step.get("path", "/")
    page.goto(ctx.base_url + path)
    page.wait_for_load_state("domcontentloaded")


def _do_reload(page: Page, step: dict[str, Any], ctx: ScenarioContext) -> None:
    page.reload()
    page.wait_for_load_state("domcontentloaded")


def _do_click(page: Page, step: dict[str, Any], ctx: ScenarioContext) -> None:
    loc = _resolve_target(page, step["target"], ctx)
    loc.click()


def _do_double_click(page: Page, step: dict[str, Any], ctx: ScenarioContext) -> None:
    loc = _resolve_target(page, step["target"], ctx)
    loc.dblclick()


def _do_fill(page: Page, step: dict[str, Any], ctx: ScenarioContext) -> None:
    loc = _resolve_target(page, step["target"], ctx)
    loc.fill(step["value"])


def _do_select(page: Page, step: dict[str, Any], ctx: ScenarioContext) -> None:
    loc = _resolve_target(page, step["target"], ctx)
    loc.select_option(step["value"])


def _do_press(page: Page, step: dict[str, Any], ctx: ScenarioContext) -> None:
    loc = _resolve_target(page, step["target"], ctx)
    loc.press(step["key"])


def _do_wait_for(page: Page, step: dict[str, Any], ctx: ScenarioContext) -> None:
    timeout = step.get("timeout", 10000)
    state = step.get("state", "visible")
    loc = _resolve_target(page, step["target"], ctx)
    loc.wait_for(state=state, timeout=timeout)


def _do_assert_visible(page: Page, step: dict[str, Any], ctx: ScenarioContext) -> None:
    timeout = step.get("timeout", 10000)
    loc = _resolve_target(page, step["target"], ctx)
    loc.wait_for(state="visible", timeout=timeout)


def _do_assert_text(page: Page, step: dict[str, Any], ctx: ScenarioContext) -> None:
    text = step["text"]
    timeout = step.get("timeout", 10000)
    page.get_by_text(text, exact=False).wait_for(state="visible", timeout=timeout)


def _do_capture(
    page: Page, step: dict[str, Any], ctx: ScenarioContext
) -> str:
    """Wait for settled UI, take a screenshot, save to output_dir.

    Returns the absolute path of the saved file.

    ``step`` may be a full step dict (when action == "capture") or just
    the ``capture`` sub-dict (when invoked as an inline post-step capture).
    The capture metadata lives in step["capture"] for action steps, or
    directly on the dict for inline captures.
    """
    # Resolve capture metadata: either nested under "capture" key or top-level
    cap = step.get("capture", step)

    _wait_for_settled(page, timeout=cap.get("settle_timeout", 5000))

    label = cap.get("name") or cap.get("label", "screenshot")
    # Sanitise label for use as a filename component.
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    filename = f"{safe_label}.png"
    path = os.path.join(ctx.output_dir, filename)

    # Respect full_page flag from capture spec, defaulting to False.
    full_page = cap.get("full_page", False)
    page.screenshot(path=path, full_page=full_page)
    return path


# ---------------------------------------------------------------------------
# Action dispatch table
# ---------------------------------------------------------------------------

_ACTION_HANDLERS: dict[str, Any] = {
    "open": _do_open,
    "reload": _do_reload,
    "click": _do_click,
    "double_click": _do_double_click,
    "dblclick": _do_double_click,  # alias
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
    """Execute all steps in the manifest sequentially.

    On any step failure the runner:
    1. Captures a failure screenshot (if possible).
    2. Records which step failed.
    3. Raises the original exception so pytest captures the traceback.

    Returns a RunResult on success (status="passed").
    """
    manifest = context.manifest
    scenario_id = manifest["id"]
    steps = manifest.get("steps", [])

    screenshots: list[str] = []
    start_ms = int(time.monotonic() * 1000)

    # Determine the default actor name (falls back to "default").
    default_actor = manifest.get("actor", "default")

    for step_index, step in enumerate(steps):
        action = step.get("action")
        if not action:
            raise ValueError(f"Step {step_index} has no 'action' key: {step!r}")

        # Each step can override the actor.
        actor = step.get("actor", default_actor)
        page = context.get_actor_page(actor)

        handler = _ACTION_HANDLERS.get(action)
        if handler is None:
            raise ValueError(
                f"Unknown action {action!r} in step {step_index}. "
                f"Valid actions: {sorted(_ACTION_HANDLERS)}"
            )

        try:
            result = handler(page, step, context)

            # Collect screenshot paths returned by capture steps.
            if action == "capture" and isinstance(result, str):
                screenshots.append(result)

            # Post-step: run any inline assert block.
            if "assert" in step:
                _run_inline_assert(page, step["assert"], context)

            # Post-step: if step carries an inline capture dict, capture now.
            if "capture" in step and action != "capture":
                cap_spec = step["capture"]
                if isinstance(cap_spec, dict):
                    cap_path = _do_capture(page, cap_spec, context)
                    screenshots.append(cap_path)

        except Exception as exc:
            # Attempt a failure screenshot before propagating.
            failure_path = os.path.join(
                context.output_dir, f"FAILURE-step-{step_index:02d}.png"
            )
            try:
                page.screenshot(path=failure_path)
                screenshots.append(failure_path)
            except Exception:
                pass

            end_ms = int(time.monotonic() * 1000)
            result = RunResult(
                scenario_id=scenario_id,
                status="failed",
                duration_ms=end_ms - start_ms,
                failed_step=step,
                failed_step_index=step_index,
                screenshots=screenshots,
                error_message=str(exc),
            )
            # Attach result to the exception so callers can inspect it.
            exc.__run_result__ = result  # type: ignore[attr-defined]
            raise

    end_ms = int(time.monotonic() * 1000)
    return RunResult(
        scenario_id=scenario_id,
        status="passed",
        duration_ms=end_ms - start_ms,
        screenshots=screenshots,
    )
