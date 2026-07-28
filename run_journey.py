#!/usr/bin/env python3
"""Execute a journey and copy screenshots to /pics."""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tests"))

from playwright.sync_api import sync_playwright
from scenario_runner import ScenarioContext, execute_scenario
from scenario_seed import cleanup_tickets, seed_tickets

from db import get_db, init_db
from journeys import compile_to_manifest, store_run_results
from scenarios import validate_manifest

JOURNEY_ID = "dashboard-screenshot-tour"
PROJECT_ID = "ticket-takeaway"
BASE_URL = "http://localhost:8787/ticket-takeaway"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "pics")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Compile journey to manifest
conn = get_db()
init_db(conn)
manifest = compile_to_manifest(conn, PROJECT_ID, JOURNEY_ID)
validate_manifest(manifest)

# Get step IDs for result storage
steps = conn.execute(
    "SELECT id FROM journey_steps WHERE journey_id = ? AND project_id = ? ORDER BY sort_order",
    (JOURNEY_ID, PROJECT_ID),
).fetchall()
step_ids = [s["id"] for s in steps]

print(f"Compiled journey: {manifest['title']}")
print(f"Steps: {len(manifest['steps'])}")
print(f"Output: {OUTPUT_DIR}")
print()

# Execute with Playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    # Seed tickets if manifest has seed data
    seed_result = None
    seed = manifest.get("seed", {})
    if seed.get("tickets"):
        api_base = BASE_URL.rstrip("/")
        seed_result = seed_tickets(seed, api_base)
        seed_id_map = {**seed_result.title_to_id, **seed_result.positional_to_id}
    else:
        seed_id_map = {}

    # Build context
    artifact_dir = os.path.join(
        os.path.dirname(__file__),
        ".artifacts",
        "journeys",
        JOURNEY_ID,
        f"{JOURNEY_ID}-{int(time.time())}",
    )
    os.makedirs(artifact_dir, exist_ok=True)

    ctx = ScenarioContext(
        base_url=BASE_URL,
        browser=browser,
        manifest=manifest,
        output_dir=artifact_dir,
        seed_id_map=seed_id_map,
    )

    try:
        result = execute_scenario(ctx)
        print(f"\nResult: {result.status}")
        print(f"Duration: {result.duration_ms}ms")
        print(f"Screenshots: {len(result.screenshots)}")

        # Copy screenshots to /pics
        for ss_path in result.screenshots:
            if os.path.exists(ss_path):
                dest = os.path.join(OUTPUT_DIR, os.path.basename(ss_path))
                shutil.copy2(ss_path, dest)
                print(f"  -> {dest}")

        # Store results in DB
        run_result = {
            "status": result.status,
            "duration_ms": result.duration_ms,
            "screenshots": result.screenshots,
            "failed_step_index": result.failed_step_index,
            "error_message": result.error_message or "",
        }
        run_id = store_run_results(
            conn, PROJECT_ID, JOURNEY_ID, run_result, step_ids, artifact_dir
        )
        conn.commit()
        print(f"\nRun stored: {run_id}")

    except Exception as e:
        print(f"\nFailed: {e}")
        # Try to get the run result from the exception
        rr = getattr(e, "__run_result__", None)
        if rr:
            run_result = {
                "status": rr.status,
                "duration_ms": rr.duration_ms,
                "screenshots": rr.screenshots,
                "failed_step_index": rr.failed_step_index,
                "error_message": rr.error_message or str(e),
            }
            run_id = store_run_results(
                conn, PROJECT_ID, JOURNEY_ID, run_result, step_ids, artifact_dir
            )
            conn.commit()
            # Still copy any screenshots taken before failure
            for ss_path in rr.screenshots:
                if os.path.exists(ss_path):
                    dest = os.path.join(OUTPUT_DIR, os.path.basename(ss_path))
                    shutil.copy2(ss_path, dest)
                    print(f"  -> {dest}")
    finally:
        ctx.close_all()
        browser.close()
        if seed_result:
            cleanup_tickets(seed_result.created_ids, BASE_URL)

conn.close()
print("\nDone!")
