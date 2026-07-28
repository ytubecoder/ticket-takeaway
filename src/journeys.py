"""User Journeys — business logic for journey CRUD, compilation, and execution.

Follows the same pattern as actions.py: pure DB operations on an open
sqlite3.Connection.  Callers are responsible for conn.commit().
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from actions import NotFoundError
from scenarios import VALID_ACTIONS


class JourneyNotFoundError(NotFoundError):
    code = "journey_not_found"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOURNEY_STATUSES = {"draft", "active", "validated", "archived"}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug or "journey"


def _find_journey(
    conn: sqlite3.Connection, project_id: str, journey_id: str
) -> sqlite3.Row:
    """Locate a journey by ID. Raises JourneyNotFoundError if not found."""
    row = conn.execute(
        "SELECT * FROM journeys WHERE id = ? AND project_id = ?",
        (journey_id, project_id),
    ).fetchone()
    if not row:
        raise JourneyNotFoundError(
            f"Journey '{journey_id}' not found in project '{project_id}'."
        )
    return row


def _next_step_order(conn: sqlite3.Connection, journey_id: str, project_id: str) -> int:
    """Return max(sort_order)+1 for a journey's steps, or 0 if none."""
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order "
        "FROM journey_steps WHERE journey_id = ? AND project_id = ?",
        (journey_id, project_id),
    ).fetchone()
    return row["next_order"]


def _unique_id(conn: sqlite3.Connection, project_id: str, base_slug: str) -> str:
    """Return a slug that doesn't collide with existing journey IDs."""
    if not conn.execute(
        "SELECT 1 FROM journeys WHERE id = ? AND project_id = ?",
        (base_slug, project_id),
    ).fetchone():
        return base_slug
    # Append a numeric suffix
    for i in range(2, 1000):
        candidate = f"{base_slug}-{i}"
        if not conn.execute(
            "SELECT 1 FROM journeys WHERE id = ? AND project_id = ?",
            (candidate, project_id),
        ).fetchone():
            return candidate
    raise ValueError(f"Could not generate unique ID for slug '{base_slug}'")


# ---------------------------------------------------------------------------
# Journey CRUD
# ---------------------------------------------------------------------------


def add_journey(
    conn: sqlite3.Connection,
    project_id: str,
    title: str,
    description: str = "",
    persona: str = "",
    *,
    journey_id: str | None = None,
) -> dict[str, Any]:
    """Create a new journey.  Returns the inserted row as a dict."""
    jid = journey_id or _unique_id(conn, project_id, _slugify(title))
    conn.execute(
        "INSERT INTO journeys (id, project_id, title, description, persona) "
        "VALUES (?, ?, ?, ?, ?)",
        (jid, project_id, title, description, persona),
    )
    return dict(_find_journey(conn, project_id, jid))


def update_journey(
    conn: sqlite3.Connection,
    project_id: str,
    journey_id: str,
    **fields,
) -> dict[str, Any]:
    """Update journey fields.  Returns the updated row as a dict."""
    _find_journey(conn, project_id, journey_id)  # existence check

    if "status" in fields and fields["status"] not in JOURNEY_STATUSES:
        raise ValueError(
            f"Invalid status '{fields['status']}'; valid: {sorted(JOURNEY_STATUSES)}"
        )

    allowed = {
        "title",
        "description",
        "persona",
        "status",
        "seed_json",
        "actors_json",
        "viewport_json",
        "theme",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return dict(_find_journey(conn, project_id, journey_id))

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [journey_id, project_id]
    conn.execute(
        f"UPDATE journeys SET {set_clause}, updated_at = datetime('now') "
        f"WHERE id = ? AND project_id = ?",
        values,
    )
    return dict(_find_journey(conn, project_id, journey_id))


def delete_journey(conn: sqlite3.Connection, project_id: str, journey_id: str) -> None:
    """Delete a journey and all its steps/runs/links (via CASCADE)."""
    _find_journey(conn, project_id, journey_id)  # existence check
    conn.execute(
        "DELETE FROM journeys WHERE id = ? AND project_id = ?",
        (journey_id, project_id),
    )


def list_journeys(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    """List all journeys for a project with step count and last run status."""
    rows = conn.execute(
        """
        SELECT j.*,
               (SELECT COUNT(*) FROM journey_steps s
                WHERE s.journey_id = j.id AND s.project_id = j.project_id) AS step_count,
               (SELECT jr.status FROM journey_runs jr
                WHERE jr.journey_id = j.id AND jr.project_id = j.project_id
                ORDER BY jr.started_at DESC LIMIT 1) AS last_run_status,
               (SELECT jr.started_at FROM journey_runs jr
                WHERE jr.journey_id = j.id AND jr.project_id = j.project_id
                ORDER BY jr.started_at DESC LIMIT 1) AS last_run_at
        FROM journeys j
        WHERE j.project_id = ?
        ORDER BY j.updated_at DESC
        """,
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_journey(
    conn: sqlite3.Connection,
    project_id: str,
    journey_id: str,
) -> dict[str, Any]:
    """Get a journey with its steps and recent runs."""
    journey = dict(_find_journey(conn, project_id, journey_id))

    steps = conn.execute(
        "SELECT * FROM journey_steps "
        "WHERE journey_id = ? AND project_id = ? ORDER BY sort_order",
        (journey_id, project_id),
    ).fetchall()
    journey["steps"] = [dict(s) for s in steps]

    runs = conn.execute(
        "SELECT * FROM journey_runs "
        "WHERE journey_id = ? AND project_id = ? ORDER BY started_at DESC LIMIT 10",
        (journey_id, project_id),
    ).fetchall()
    journey["runs"] = [dict(r) for r in runs]

    # Linked tickets
    links = conn.execute(
        "SELECT * FROM journey_tickets WHERE journey_id = ? AND project_id = ?",
        (journey_id, project_id),
    ).fetchall()
    journey["linked_tickets"] = [dict(l) for l in links]

    return journey


# ---------------------------------------------------------------------------
# Step CRUD
# ---------------------------------------------------------------------------


def add_step(
    conn: sqlite3.Connection,
    journey_id: str,
    project_id: str,
    *,
    action: str,
    label: str = "",
    actor: str = "user",
    target: dict | None = None,
    value: str = "",
    key: str = "",
    capture: dict | None = None,
    assertion: dict | None = None,
) -> dict[str, Any]:
    """Add a step to a journey.  Returns the inserted row as a dict."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid action '{action}'; valid: {sorted(VALID_ACTIONS)}")

    sort_order = _next_step_order(conn, journey_id, project_id)
    target_json = json.dumps(target) if target else "{}"
    capture_json = json.dumps(capture) if capture else ""
    assert_json = json.dumps(assertion) if assertion else ""

    cursor = conn.execute(
        "INSERT INTO journey_steps "
        "(journey_id, project_id, sort_order, label, actor, action, "
        "target_json, value, key, capture_json, assert_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            journey_id,
            project_id,
            sort_order,
            label,
            actor,
            action,
            target_json,
            value,
            key,
            capture_json,
            assert_json,
        ),
    )
    row = conn.execute(
        "SELECT * FROM journey_steps WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row)


def update_step(conn: sqlite3.Connection, step_id: int, **fields) -> dict[str, Any]:
    """Update step fields.  Returns the updated row as a dict."""
    existing = conn.execute(
        "SELECT * FROM journey_steps WHERE id = ?", (step_id,)
    ).fetchone()
    if not existing:
        raise ValueError(f"Step {step_id} not found.")

    # Translate dict fields to JSON
    updates = {}
    for k, v in fields.items():
        if k == "target":
            updates["target_json"] = json.dumps(v) if v else "{}"
        elif k == "capture":
            updates["capture_json"] = json.dumps(v) if v else ""
        elif k == "assertion":
            updates["assert_json"] = json.dumps(v) if v else ""
        elif k in ("label", "actor", "action", "value", "key", "sort_order"):
            updates[k] = v

    if not updates:
        return dict(existing)

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [step_id]
    conn.execute(f"UPDATE journey_steps SET {set_clause} WHERE id = ?", values)

    return dict(
        conn.execute("SELECT * FROM journey_steps WHERE id = ?", (step_id,)).fetchone()
    )


def delete_step(conn: sqlite3.Connection, step_id: int) -> None:
    """Delete a step."""
    conn.execute("DELETE FROM journey_steps WHERE id = ?", (step_id,))


def reorder_steps(
    conn: sqlite3.Connection,
    journey_id: str,
    project_id: str,
    step_ids: list[int],
) -> None:
    """Set step sort_order based on position in step_ids list."""
    for i, sid in enumerate(step_ids):
        conn.execute(
            "UPDATE journey_steps SET sort_order = ? "
            "WHERE id = ? AND journey_id = ? AND project_id = ?",
            (i, sid, journey_id, project_id),
        )


# ---------------------------------------------------------------------------
# Compilation: Journey → Scenario Manifest
# ---------------------------------------------------------------------------


def _step_to_manifest_step(step: dict[str, Any]) -> dict[str, Any]:
    """Convert a journey_steps row dict into a scenario manifest step dict."""
    result: dict[str, Any] = {
        "actor": step["actor"],
        "action": step["action"],
    }

    # Target
    target = json.loads(step["target_json"]) if step["target_json"] else {}
    if target:
        result["target"] = target

    # Value: for fill action it's 'value', for open action it's 'path'
    if step["value"]:
        if step["action"] == "open":
            result["path"] = step["value"]
        else:
            result["value"] = step["value"]

    # Key (for press action)
    if step["key"]:
        result["key"] = step["key"]

    # Capture
    if step["capture_json"]:
        result["capture"] = json.loads(step["capture_json"])

    # Assert
    if step["assert_json"]:
        result["assert"] = json.loads(step["assert_json"])

    return result


def compile_to_manifest(
    conn: sqlite3.Connection,
    project_id: str,
    journey_id: str,
) -> dict[str, Any]:
    """Compile a journey into a scenario manifest dict.

    The result passes validate_manifest() and can be fed directly to
    execute_scenario().
    """
    journey = _find_journey(conn, project_id, journey_id)

    steps = conn.execute(
        "SELECT * FROM journey_steps "
        "WHERE journey_id = ? AND project_id = ? ORDER BY sort_order",
        (journey_id, project_id),
    ).fetchall()
    if not steps:
        raise ValueError(f"Journey '{journey_id}' has no steps — cannot compile.")

    manifest: dict[str, Any] = {
        "id": f"journey-{journey_id}",
        "title": journey["title"],
        "tags": ["journey", "auto-compiled"],
        "actors": json.loads(journey["actors_json"]),
        "seed": json.loads(journey["seed_json"]),
        "viewport": json.loads(journey["viewport_json"]),
        "steps": [_step_to_manifest_step(dict(s)) for s in steps],
    }

    theme = journey["theme"]
    if theme:
        manifest["theme"] = theme

    return manifest


# ---------------------------------------------------------------------------
# Run result storage
# ---------------------------------------------------------------------------


def store_run_results(
    conn: sqlite3.Connection,
    project_id: str,
    journey_id: str,
    run_result: dict[str, Any],
    step_ids: list[int],
    artifact_dir: str = "",
) -> str:
    """Persist a RunResult to the DB.  Returns the run ID."""
    now = datetime.now(timezone.utc).isoformat()
    run_id = f"{journey_id}-{int(time.time())}"

    conn.execute(
        "INSERT INTO journey_runs "
        "(id, journey_id, project_id, status, started_at, finished_at, "
        "duration_ms, error_message, artifact_dir) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            journey_id,
            project_id,
            run_result["status"],
            now,
            now,
            run_result.get("duration_ms", 0),
            run_result.get("error_message", ""),
            artifact_dir,
        ),
    )

    failed_idx = run_result.get("failed_step_index")
    for i, step_id in enumerate(step_ids):
        if (
            run_result["status"] == "passed"
            or failed_idx is not None
            and i < failed_idx
        ):
            step_status = "passed"
        elif failed_idx is not None and i == failed_idx:
            step_status = "failed"
        else:
            step_status = "skipped"

        error_msg = (
            run_result.get("error_message", "") if step_status == "failed" else ""
        )
        conn.execute(
            "INSERT INTO journey_step_results "
            "(run_id, step_id, sort_order, status, error_message) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, step_id, i, step_status, error_msg),
        )

    # Backfill screenshot paths from artifact directory
    if artifact_dir:
        _backfill_screenshots(conn, run_id, artifact_dir, journey_id)

    return run_id


def _backfill_screenshots(
    conn: sqlite3.Connection, run_id: str, artifact_dir: str, journey_id: str
) -> None:
    """Scan artifact dir for .png files and assign to capture step results."""
    import os

    if not os.path.isdir(artifact_dir):
        return
    pngs = sorted(f for f in os.listdir(artifact_dir) if f.endswith(".png"))
    if not pngs:
        return

    # Get step results with their journey step action type
    step_results = conn.execute(
        "SELECT jsr.id, jsr.sort_order, js.action "
        "FROM journey_step_results jsr "
        "LEFT JOIN journey_steps js ON jsr.step_id = js.id "
        "WHERE jsr.run_id = ? ORDER BY jsr.sort_order",
        (run_id,),
    ).fetchall()

    # Match screenshots to capture steps only
    capture_results = [sr for sr in step_results if sr["action"] == "capture"]
    for i, png in enumerate(pngs):
        screenshot_url = f"/api/journeys/{journey_id}/runs/{run_id}/screenshots/{png}"
        if i < len(capture_results):
            conn.execute(
                "UPDATE journey_step_results SET screenshot_path = ? WHERE id = ?",
                (screenshot_url, capture_results[i]["id"]),
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Ticket linking
# ---------------------------------------------------------------------------


def link_ticket(
    conn: sqlite3.Connection,
    journey_id: str,
    project_id: str,
    ticket_id: str,
    step_id: int | None = None,
) -> None:
    """Link a ticket to a journey (idempotent)."""
    conn.execute(
        "INSERT OR REPLACE INTO journey_tickets "
        "(journey_id, project_id, ticket_id, step_id) "
        "VALUES (?, ?, ?, ?)",
        (journey_id, project_id, ticket_id, step_id),
    )


def unlink_ticket(
    conn: sqlite3.Connection,
    journey_id: str,
    project_id: str,
    ticket_id: str,
) -> None:
    """Remove a ticket link from a journey."""
    conn.execute(
        "DELETE FROM journey_tickets "
        "WHERE journey_id = ? AND project_id = ? AND ticket_id = ?",
        (journey_id, project_id, ticket_id),
    )


# ---------------------------------------------------------------------------
# Inference engine: tickets → journey suggestions
# ---------------------------------------------------------------------------


def _manifest_step_to_db_fields(step: dict[str, Any]) -> dict[str, Any]:
    """Convert a scenario manifest step dict to journey_steps DB fields."""
    value = step.get("value", "")
    if step["action"] == "open" and "path" in step:
        value = step["path"]
    return {
        "actor": step.get("actor", "user"),
        "action": step["action"],
        "target": step.get("target"),
        "value": value,
        "key": step.get("key", ""),
        "capture": step.get("capture"),
        "assertion": step.get("assert"),
    }


def infer_journeys(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[dict[str, Any]]:
    """Generate journey suggestions from existing tickets.

    Groups tickets by lifecycle stage and generates journey steps using
    template builders from scenario_drafting.py.

    Returns a list of suggestion dicts, each with:
        title, description, persona, steps (list of step field dicts),
        actors_json, seed_json
    """
    from scenario_drafting import (
        _steps_capture,
        _steps_close_detail,
        _steps_create_ticket,
        _steps_open_board,
        _steps_open_detail,
    )

    tickets = conn.execute(
        "SELECT * FROM tickets WHERE project_id = ? AND archived = 0 "
        "ORDER BY section, sort_order",
        (project_id,),
    ).fetchall()

    if not tickets:
        return []

    suggestions: list[dict[str, Any]] = []

    # Group by section
    by_section: dict[str, list] = {}
    for t in tickets:
        sec = t["section"]
        by_section.setdefault(sec, []).append(dict(t))

    # 1. Board overview journey (always)
    overview_steps = _steps_open_board("user") + _steps_capture(
        "user", "board-overview"
    )
    suggestions.append(
        {
            "title": "Board Overview",
            "description": "Open the board and verify it loads correctly",
            "persona": "Any user",
            "steps": [_manifest_step_to_db_fields(s) for s in overview_steps],
            "actors_json": '{"user": {"label": "User"}}',
            "seed_json": "{}",
        }
    )

    # 2. Feature creation journey (from Ideas/Backlog tickets)
    idea_tickets = by_section.get("Ideas", []) + by_section.get("Backlog", [])
    if idea_tickets:
        sample = idea_tickets[0]
        create_steps = (
            _steps_open_board("user")
            + _steps_create_ticket("user", sample["title"], "Backlog")
            + _steps_capture("user", "after-create")
        )
        suggestions.append(
            {
                "title": f"Create Feature: {sample['title']}",
                "description": f"Create a new ticket for '{sample['title']}' and verify it appears",
                "persona": "Product manager",
                "steps": [_manifest_step_to_db_fields(s) for s in create_steps],
                "actors_json": '{"user": {"label": "Product Manager"}}',
                "seed_json": "{}",
            }
        )

    # 3. Feature inspection journey (from WIP/Review tickets)
    active_tickets = by_section.get("WIP", []) + by_section.get("For Review", [])
    if active_tickets:
        sample = active_tickets[0]
        inspect_steps = (
            _steps_open_board("user")
            + _steps_open_detail("user", sample["title"])
            + _steps_capture("user", "detail-view")
            + _steps_close_detail("user")
        )
        seed = {"tickets": [{"title": sample["title"], "section": sample["section"]}]}
        suggestions.append(
            {
                "title": f"Inspect: {sample['title']}",
                "description": f"Open the detail view for '{sample['title']}' and verify fields",
                "persona": "Developer",
                "steps": [_manifest_step_to_db_fields(s) for s in inspect_steps],
                "actors_json": '{"user": {"label": "Developer"}}',
                "seed_json": json.dumps(seed),
            }
        )

    # 4. Completed feature verification (from Done tickets)
    done_tickets = by_section.get("Done", [])
    if done_tickets:
        sample = done_tickets[0]
        verify_steps = (
            _steps_open_board("user")
            + _steps_open_detail("user", sample["title"])
            + _steps_capture("user", "done-detail")
            + _steps_close_detail("user")
        )
        seed = {"tickets": [{"title": sample["title"], "section": "Done"}]}
        suggestions.append(
            {
                "title": f"Verify Done: {sample['title']}",
                "description": f"Verify completed feature '{sample['title']}' is accessible",
                "persona": "QA",
                "steps": [_manifest_step_to_db_fields(s) for s in verify_steps],
                "actors_json": '{"user": {"label": "QA"}}',
                "seed_json": json.dumps(seed),
            }
        )

    return suggestions


# ---------------------------------------------------------------------------
# Path Builder: screen-level path → element-level steps
# ---------------------------------------------------------------------------

_SCREEN_ROUTES: dict[str, str] = {
    "Board": "",
    "Settings": "/settings",
    "Journeys": "/journeys",
    "Project Picker": "/",
    "Detail Overlay": "",
}


def _make_step(
    actor: str,
    action: str,
    *,
    label: str = "",
    value: str = "",
    target: dict | None = None,
    key: str = "",
    capture: dict | None = None,
    assertion: dict | None = None,
) -> dict[str, Any]:
    """Build a step dict with all keys expected by add_step()."""
    return {
        "actor": actor,
        "action": action,
        "label": label,
        "value": value,
        "target": target,
        "key": key,
        "capture": capture,
        "assertion": assertion,
    }


def build_steps_from_path(
    path: list[dict],
    actor: str = "user",
) -> list[dict]:
    """Convert a screen-level path into element-level step dicts.

    Each path entry: {"screen": "Board", "interaction": {...} or None}
    Interaction dict: {"type": "button"|"text-input"|"screenshot", "testid": "...",
                        "name": "...", "fill_value": "...", "navigates_to": "..."}
    """
    steps: list[dict] = []
    current_screen: str | None = None

    for entry in path:
        screen = entry["screen"]
        interaction = entry.get("interaction")

        if screen != current_screen and interaction is None:
            route = _SCREEN_ROUTES.get(screen, "")
            steps.append(
                _make_step(actor, "open", label=f"Go to {screen}", value=route)
            )
            steps.append(
                _make_step(
                    actor,
                    "capture",
                    label=f"Screenshot: {screen}",
                    capture={"name": screen.lower().replace(" ", "-")},
                )
            )
            current_screen = screen
            continue

        if interaction is None:
            continue

        itype = interaction.get("type", "button")
        testid = interaction.get("testid", "")
        name = interaction.get("name", "")

        if itype == "screenshot":
            steps.append(
                _make_step(
                    actor,
                    "capture",
                    label=f"Screenshot: {name or current_screen}",
                    capture={
                        "name": (name or current_screen or "capture")
                        .lower()
                        .replace(" ", "-")
                    },
                )
            )
        elif itype in ("text-input", "textarea"):
            target = {"testid": testid} if testid else None
            steps.append(
                _make_step(
                    actor,
                    "fill",
                    label=f"Fill: {name}",
                    value=interaction.get("fill_value", ""),
                    target=target,
                )
            )
        elif itype == "select":
            target = {"testid": testid} if testid else None
            steps.append(
                _make_step(
                    actor,
                    "select",
                    label=f"Select: {name}",
                    value=interaction.get("fill_value", ""),
                    target=target,
                )
            )
        else:
            target = {"testid": testid} if testid else None
            steps.append(
                _make_step(
                    actor,
                    "click",
                    label=f"Click: {name}",
                    target=target,
                )
            )

        navigates_to = interaction.get("navigates_to")
        if navigates_to:
            current_screen = navigates_to
            steps.append(
                _make_step(
                    actor,
                    "capture",
                    label=f"Screenshot: {navigates_to}",
                    capture={"name": navigates_to.lower().replace(" ", "-")},
                )
            )

    return steps
