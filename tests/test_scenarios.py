"""Manifest-driven scenario tests.

Discovers scenario manifests from tests/scenarios/ and runs each
as a parametrized pytest test.

Usage
-----
Run all discovered scenarios:
    pytest tests/test_scenarios.py -v

Run a single scenario by ID:
    pytest tests/test_scenarios.py -v --scenario-id=my-scenario-id

With gallery publishing (writes a run summary JSON alongside screenshots):
    pytest tests/test_scenarios.py -v --publish
"""

import importlib.util
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Ensure src/ is importable (for scenarios.py and any shared modules).
# ---------------------------------------------------------------------------

_src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# scenarios.py lives in src/ but has no package structure; load via importlib.
_scenarios_path = os.path.join(_src_dir, "scenarios.py")
_scenarios_spec = importlib.util.spec_from_file_location("scenarios", _scenarios_path)
if _scenarios_spec is None or _scenarios_spec.loader is None:
    raise ImportError(
        f"Could not load src/scenarios.py from {_scenarios_path}. "
        "Ensure the file exists and is valid Python."
    )
_scenarios_mod = importlib.util.module_from_spec(_scenarios_spec)
sys.modules.setdefault("scenarios", _scenarios_mod)
_scenarios_spec.loader.exec_module(_scenarios_mod)  # type: ignore[union-attr]

discover_scenarios = _scenarios_mod.discover_scenarios  # type: ignore[attr-defined]
ScenarioValidationError = _scenarios_mod.ScenarioValidationError  # type: ignore[attr-defined]

from scenario_runner import ScenarioContext, execute_scenario  # noqa: E402  (after sys.path setup)
from scenario_seed import seed_tickets, cleanup_tickets  # noqa: E402

publish_gallery = _scenarios_mod.publish_gallery  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Discover manifests at collection time.
# ---------------------------------------------------------------------------

_SCENARIO_DIR = os.path.join(os.path.dirname(__file__), "scenarios")
_MANIFESTS: list[dict] = (
    discover_scenarios(_SCENARIO_DIR) if os.path.isdir(_SCENARIO_DIR) else []
)

# ---------------------------------------------------------------------------
# Parametrized test
# ---------------------------------------------------------------------------

_NO_SCENARIOS_MARK = pytest.mark.skip(reason="No scenario manifests found in tests/scenarios/")


@pytest.mark.parametrize(
    "manifest",
    _MANIFESTS or [{}],
    ids=[m["id"] for m in _MANIFESTS] if _MANIFESTS else ["no-scenarios"],
)
def test_scenario(manifest, dashboard_server, browser, tmp_path, request):
    """Run a single scenario manifest end-to-end.

    Fixtures from conftest.py:
      dashboard_server — yields a base URL (http://host:port/project-id)
      browser          — session-scoped Chromium instance
      tmp_path         — per-test isolated directory for screenshot output
    """
    # Skip placeholder when no manifests exist.
    if not manifest:
        pytest.skip("No scenario manifests found in tests/scenarios/")

    # --scenario-id filter: skip everything that doesn't match.
    scenario_id_filter: str | None = request.config.getoption(
        "--scenario-id", default=None
    )
    if scenario_id_filter and manifest["id"] != scenario_id_filter:
        pytest.skip(f"Filtered: only running scenario '{scenario_id_filter}'")

    import json as _json
    import shutil
    import time as _time

    # Build artifact output directory
    run_id = f"{manifest['id']}-{int(_time.time())}"
    project_root = os.path.dirname(os.path.dirname(__file__))
    artifact_dir = os.path.join(project_root, ".artifacts", "scenarios", run_id)
    os.makedirs(artifact_dir, exist_ok=True)

    # Write manifest copy
    with open(os.path.join(artifact_dir, "manifest.json"), "w") as f:
        _json.dump(manifest, f, indent=2)

    # Seed deterministic data from manifest
    seed_result = None
    if manifest.get("seed", {}).get("tickets"):
        seed_result = seed_tickets(manifest["seed"], dashboard_server)

    seed_id_map = {}
    if seed_result:
        seed_id_map = {**seed_result.title_to_id, **seed_result.positional_to_id}

    screenshots_dir = os.path.join(artifact_dir, "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)

    ctx = ScenarioContext(
        base_url=dashboard_server,
        browser=browser,
        output_dir=screenshots_dir,
        manifest=manifest,
        seed_id_map=seed_id_map,
    )

    try:
        result = execute_scenario(ctx)
    finally:
        ctx.close_all()
        if seed_result:
            cleanup_tickets(seed_result.created_ids, dashboard_server)

    # Write run summary
    summary = {
        "scenario_id": result.scenario_id,
        "status": result.status,
        "duration_ms": result.duration_ms,
        "screenshots": result.screenshots,
        "failed_step_index": result.failed_step_index,
        "error_message": result.error_message,
    }
    with open(os.path.join(artifact_dir, "summary.json"), "w") as f:
        _json.dump(summary, f, indent=2)

    # --publish: copy publishable screenshots to gallery
    publish: bool = request.config.getoption("--publish", default=False)
    if publish and result.status == "passed":
        # Collect publish_slot → screenshot path mapping
        publish_map = {}
        for step in manifest.get("steps", []):
            cap = step.get("capture", {})
            slot = cap.get("publish_slot")
            name = cap.get("name")
            if slot and name:
                # Find the screenshot file matching this capture name
                for spath in result.screenshots:
                    if name in os.path.basename(spath):
                        publish_map[slot] = spath
                        break
        if publish_map:
            gallery_dir = os.path.join(project_root, "docs", "scenarios", "gallery")
            publish_gallery(publish_map, gallery_dir)

    # Surface screenshot paths in the pytest report for easy inspection.
    if result.screenshots:
        request.node.user_properties.append(
            ("screenshots", result.screenshots)
        )
    request.node.user_properties.append(("artifact_dir", artifact_dir))

    assert result.status == "passed", (
        f"Scenario '{result.scenario_id}' failed at step "
        f"{result.failed_step_index}: {result.failed_step!r}\n"
        f"Error: {result.error_message}"
    )


