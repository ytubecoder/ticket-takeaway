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
            assert_spec["element_visible"],
            timeout=timeout,
            seed_id_map=ctx.seed_id_map,
        )

    if "element_hidden" in assert_spec:
        backend.wait_for_hidden(
            assert_spec["element_hidden"],
            timeout=timeout,
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
