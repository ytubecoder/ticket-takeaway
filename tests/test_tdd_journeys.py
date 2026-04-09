"""TDD tests for User Journeys — data model, CRUD, compilation to scenario manifests.

Tests run against an in-memory SQLite DB with no server required.
"""

import json
import sqlite3

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import init_db
from journeys import (
    add_journey,
    update_journey,
    delete_journey,
    list_journeys,
    get_journey,
    add_step,
    update_step,
    delete_step,
    reorder_steps,
    compile_to_manifest,
    store_run_results,
    link_ticket,
    unlink_ticket,
    infer_journeys,
    JOURNEY_STATUSES,
)
from scenarios import validate_manifest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """In-memory DB with schema initialized."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c


PID = "test-project"


# ===========================================================================
# Schema tests
# ===========================================================================

class TestJourneysTable:
    def test_journeys_table_exists(self, conn):
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "journeys" in tables

    def test_journey_steps_table_exists(self, conn):
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "journey_steps" in tables

    def test_journey_runs_table_exists(self, conn):
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "journey_runs" in tables

    def test_journey_step_results_table_exists(self, conn):
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "journey_step_results" in tables

    def test_journey_tickets_table_exists(self, conn):
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "journey_tickets" in tables


# ===========================================================================
# Journey CRUD
# ===========================================================================

class TestAddJourney:
    def test_add_journey_returns_row(self, conn):
        j = add_journey(conn, PID, "Onboarding flow", "New user signs up", "New User")
        assert j["id"] is not None
        assert j["title"] == "Onboarding flow"
        assert j["description"] == "New user signs up"
        assert j["persona"] == "New User"
        assert j["status"] == "draft"

    def test_add_journey_generates_slug_id(self, conn):
        j = add_journey(conn, PID, "My Cool Journey!", "", "")
        # Should be a lowercase slug
        assert j["id"] == "my-cool-journey"

    def test_add_journey_deduplicates_slug(self, conn):
        j1 = add_journey(conn, PID, "Test Journey", "", "")
        j2 = add_journey(conn, PID, "Test Journey", "", "")
        assert j1["id"] != j2["id"]
        assert j2["id"].startswith("test-journey-")

    def test_add_journey_with_custom_id(self, conn):
        j = add_journey(conn, PID, "Custom", "", "", journey_id="custom-id")
        assert j["id"] == "custom-id"


class TestUpdateJourney:
    def test_update_title(self, conn):
        j = add_journey(conn, PID, "Original", "", "")
        updated = update_journey(conn, PID, j["id"], title="Updated")
        assert updated["title"] == "Updated"

    def test_update_status(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        updated = update_journey(conn, PID, j["id"], status="active")
        assert updated["status"] == "active"

    def test_update_invalid_status_raises(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        with pytest.raises(ValueError, match="Invalid status"):
            update_journey(conn, PID, j["id"], status="bogus")

    def test_update_nonexistent_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            update_journey(conn, PID, "nonexistent", title="Nope")


class TestDeleteJourney:
    def test_delete_removes_journey(self, conn):
        j = add_journey(conn, PID, "To Delete", "", "")
        delete_journey(conn, PID, j["id"])
        result = list_journeys(conn, PID)
        assert len(result) == 0

    def test_delete_cascades_steps(self, conn):
        j = add_journey(conn, PID, "With Steps", "", "")
        add_step(conn, j["id"], PID, action="open", label="Open board")
        delete_journey(conn, PID, j["id"])
        steps = conn.execute("SELECT * FROM journey_steps").fetchall()
        assert len(steps) == 0

    def test_delete_nonexistent_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            delete_journey(conn, PID, "nonexistent")


class TestListJourneys:
    def test_list_empty(self, conn):
        result = list_journeys(conn, PID)
        assert result == []

    def test_list_returns_step_count(self, conn):
        j = add_journey(conn, PID, "With Steps", "", "")
        add_step(conn, j["id"], PID, action="open", label="Step 1")
        add_step(conn, j["id"], PID, action="click", label="Step 2",
                 target={"testid": "btn"})
        result = list_journeys(conn, PID)
        assert len(result) == 1
        assert result[0]["step_count"] == 2

    def test_list_filters_by_project(self, conn):
        add_journey(conn, "proj-a", "Journey A", "", "")
        add_journey(conn, "proj-b", "Journey B", "", "")
        result = list_journeys(conn, "proj-a")
        assert len(result) == 1
        assert result[0]["title"] == "Journey A"


class TestGetJourney:
    def test_get_returns_full_detail(self, conn):
        j = add_journey(conn, PID, "Detail Test", "desc", "persona")
        add_step(conn, j["id"], PID, action="open", label="Open")
        result = get_journey(conn, PID, j["id"])
        assert result["title"] == "Detail Test"
        assert len(result["steps"]) == 1

    def test_get_nonexistent_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            get_journey(conn, PID, "nonexistent")


# ===========================================================================
# Step CRUD
# ===========================================================================

class TestAddStep:
    def test_add_step_returns_row(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        s = add_step(conn, j["id"], PID, action="click", label="Click button",
                     target={"testid": "submit-btn"})
        assert s["id"] is not None
        assert s["action"] == "click"
        assert s["label"] == "Click button"
        assert json.loads(s["target_json"]) == {"testid": "submit-btn"}

    def test_add_step_auto_increments_sort_order(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        s1 = add_step(conn, j["id"], PID, action="open", label="Step 1")
        s2 = add_step(conn, j["id"], PID, action="click", label="Step 2",
                      target={"testid": "btn"})
        assert s1["sort_order"] == 0
        assert s2["sort_order"] == 1

    def test_add_step_with_capture(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        s = add_step(conn, j["id"], PID, action="capture", label="Screenshot",
                     capture={"name": "board-overview"})
        assert json.loads(s["capture_json"]) == {"name": "board-overview"}

    def test_add_step_with_assert(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        s = add_step(conn, j["id"], PID, action="click", label="Click",
                     target={"testid": "btn"},
                     assertion={"text_visible": "Success"})
        assert json.loads(s["assert_json"]) == {"text_visible": "Success"}

    def test_add_step_invalid_action_raises(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        with pytest.raises(ValueError, match="Invalid action"):
            add_step(conn, j["id"], PID, action="invalid_action", label="Bad")


class TestUpdateStep:
    def test_update_step_label(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        s = add_step(conn, j["id"], PID, action="open", label="Old")
        updated = update_step(conn, s["id"], label="New label")
        assert updated["label"] == "New label"

    def test_update_step_target(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        s = add_step(conn, j["id"], PID, action="click", label="Click",
                     target={"testid": "old"})
        updated = update_step(conn, s["id"], target={"testid": "new"})
        assert json.loads(updated["target_json"]) == {"testid": "new"}


class TestDeleteStep:
    def test_delete_step_removes_it(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        s = add_step(conn, j["id"], PID, action="open", label="To remove")
        delete_step(conn, s["id"])
        steps = conn.execute(
            "SELECT * FROM journey_steps WHERE journey_id = ?", (j["id"],)
        ).fetchall()
        assert len(steps) == 0


class TestReorderSteps:
    def test_reorder_changes_sort_order(self, conn):
        j = add_journey(conn, PID, "Test", "", "")
        s1 = add_step(conn, j["id"], PID, action="open", label="A")
        s2 = add_step(conn, j["id"], PID, action="click", label="B",
                      target={"testid": "x"})
        s3 = add_step(conn, j["id"], PID, action="capture", label="C",
                      capture={"name": "cap"})
        # Reverse order
        reorder_steps(conn, j["id"], PID, [s3["id"], s1["id"], s2["id"]])
        steps = conn.execute(
            "SELECT id, sort_order FROM journey_steps WHERE journey_id = ? ORDER BY sort_order",
            (j["id"],)
        ).fetchall()
        assert [s["id"] for s in steps] == [s3["id"], s1["id"], s2["id"]]


# ===========================================================================
# Compilation to scenario manifest
# ===========================================================================

class TestCompileToManifest:
    def test_compile_produces_valid_manifest(self, conn):
        j = add_journey(conn, PID, "Compile Test", "desc", "User")
        add_step(conn, j["id"], PID, action="open", label="Open board",
                 actor="user")
        add_step(conn, j["id"], PID, action="click", label="Click card",
                 target={"testid": "ticket-card-B-01"}, actor="user")
        add_step(conn, j["id"], PID, action="capture", label="Screenshot",
                 capture={"name": "board-state"}, actor="user")

        manifest = compile_to_manifest(conn, PID, j["id"])

        # Should pass scenario validation
        validated = validate_manifest(manifest)
        assert validated["id"].startswith("journey-")
        assert validated["title"] == "Compile Test"
        assert len(validated["steps"]) == 3

    def test_compile_preserves_step_order(self, conn):
        j = add_journey(conn, PID, "Order Test", "", "")
        add_step(conn, j["id"], PID, action="open", label="First")
        add_step(conn, j["id"], PID, action="click", label="Second",
                 target={"testid": "btn"})
        add_step(conn, j["id"], PID, action="capture", label="Third",
                 capture={"name": "cap"})

        manifest = compile_to_manifest(conn, PID, j["id"])
        actions = [s["action"] for s in manifest["steps"]]
        assert actions == ["open", "click", "capture"]

    def test_compile_includes_target_and_capture(self, conn):
        j = add_journey(conn, PID, "Fields Test", "", "")
        add_step(conn, j["id"], PID, action="click", label="Click",
                 target={"testid": "btn"}, capture={"name": "after-click"})

        manifest = compile_to_manifest(conn, PID, j["id"])
        step = manifest["steps"][0]
        assert step["target"] == {"testid": "btn"}
        assert step["capture"] == {"name": "after-click"}

    def test_compile_includes_seed_and_actors(self, conn):
        j = add_journey(conn, PID, "Seed Test", "", "")
        seed = {"tickets": [{"title": "Test ticket", "section": "Backlog"}]}
        actors = {"admin": {"label": "Admin"}, "user": {"label": "User"}}
        update_journey(conn, PID, j["id"],
                       seed_json=json.dumps(seed),
                       actors_json=json.dumps(actors))
        add_step(conn, j["id"], PID, action="open", label="Open", actor="admin")

        manifest = compile_to_manifest(conn, PID, j["id"])
        assert manifest["seed"] == seed
        assert manifest["actors"] == actors

    def test_compile_empty_journey_raises(self, conn):
        j = add_journey(conn, PID, "Empty", "", "")
        with pytest.raises(ValueError, match="no steps"):
            compile_to_manifest(conn, PID, j["id"])

    def test_compile_includes_fill_value(self, conn):
        j = add_journey(conn, PID, "Fill Test", "", "")
        add_step(conn, j["id"], PID, action="fill", label="Fill input",
                 target={"testid": "name-input"}, value="Hello World")

        manifest = compile_to_manifest(conn, PID, j["id"])
        step = manifest["steps"][0]
        assert step["value"] == "Hello World"

    def test_compile_includes_press_key(self, conn):
        j = add_journey(conn, PID, "Press Test", "", "")
        add_step(conn, j["id"], PID, action="press", label="Press Enter",
                 target={"testid": "input"}, key="Enter")

        manifest = compile_to_manifest(conn, PID, j["id"])
        step = manifest["steps"][0]
        assert step["key"] == "Enter"

    def test_compile_includes_theme(self, conn):
        j = add_journey(conn, PID, "Theme Test", "", "")
        update_journey(conn, PID, j["id"], theme="dark")
        add_step(conn, j["id"], PID, action="open", label="Open")

        manifest = compile_to_manifest(conn, PID, j["id"])
        assert manifest["theme"] == "dark"


# ===========================================================================
# Run result storage
# ===========================================================================

class TestStoreRunResults:
    def _setup_journey_with_steps(self, conn):
        """Helper: create a journey with 3 steps, return (journey, [step_ids])."""
        j = add_journey(conn, PID, "Run Test", "", "")
        s1 = add_step(conn, j["id"], PID, action="open", label="Open")
        s2 = add_step(conn, j["id"], PID, action="click", label="Click",
                      target={"testid": "btn"})
        s3 = add_step(conn, j["id"], PID, action="capture", label="Cap",
                      capture={"name": "cap"})
        return j, [s1["id"], s2["id"], s3["id"]]

    def test_store_passing_run(self, conn):
        j, step_ids = self._setup_journey_with_steps(conn)
        run_result = {
            "status": "passed",
            "duration_ms": 1500,
            "screenshots": ["/tmp/cap.png"],
            "failed_step_index": None,
            "error_message": "",
        }
        run_id = store_run_results(conn, PID, j["id"], run_result, step_ids)

        run = conn.execute("SELECT * FROM journey_runs WHERE id = ?", (run_id,)).fetchone()
        assert run["status"] == "passed"
        assert run["duration_ms"] == 1500

        results = conn.execute(
            "SELECT * FROM journey_step_results WHERE run_id = ? ORDER BY sort_order",
            (run_id,)
        ).fetchall()
        assert len(results) == 3
        assert all(r["status"] == "passed" for r in results)

    def test_store_failing_run(self, conn):
        j, step_ids = self._setup_journey_with_steps(conn)
        run_result = {
            "status": "failed",
            "duration_ms": 800,
            "screenshots": [],
            "failed_step_index": 1,
            "error_message": "Element not found",
        }
        run_id = store_run_results(conn, PID, j["id"], run_result, step_ids)

        results = conn.execute(
            "SELECT * FROM journey_step_results WHERE run_id = ? ORDER BY sort_order",
            (run_id,)
        ).fetchall()
        assert results[0]["status"] == "passed"
        assert results[1]["status"] == "failed"
        assert results[1]["error_message"] == "Element not found"
        assert results[2]["status"] == "skipped"


# ===========================================================================
# Ticket linking
# ===========================================================================

class TestTicketLinking:
    def _create_ticket(self, conn, ticket_id="B-01"):
        conn.execute(
            "INSERT INTO tickets (id, project_id, title) VALUES (?, ?, ?)",
            (ticket_id, PID, "Test Ticket"),
        )

    def test_link_ticket(self, conn):
        j = add_journey(conn, PID, "Link Test", "", "")
        self._create_ticket(conn)
        link_ticket(conn, j["id"], PID, "B-01")

        links = conn.execute(
            "SELECT * FROM journey_tickets WHERE journey_id = ?", (j["id"],)
        ).fetchall()
        assert len(links) == 1
        assert links[0]["ticket_id"] == "B-01"

    def test_link_ticket_to_step(self, conn):
        j = add_journey(conn, PID, "Link Step Test", "", "")
        s = add_step(conn, j["id"], PID, action="open", label="Open")
        self._create_ticket(conn)
        link_ticket(conn, j["id"], PID, "B-01", step_id=s["id"])

        link = conn.execute(
            "SELECT step_id FROM journey_tickets WHERE journey_id = ?", (j["id"],)
        ).fetchone()
        assert link["step_id"] == s["id"]

    def test_unlink_ticket(self, conn):
        j = add_journey(conn, PID, "Unlink Test", "", "")
        self._create_ticket(conn)
        link_ticket(conn, j["id"], PID, "B-01")
        unlink_ticket(conn, j["id"], PID, "B-01")

        links = conn.execute(
            "SELECT * FROM journey_tickets WHERE journey_id = ?", (j["id"],)
        ).fetchall()
        assert len(links) == 0

    def test_link_duplicate_is_idempotent(self, conn):
        j = add_journey(conn, PID, "Dup Test", "", "")
        self._create_ticket(conn)
        link_ticket(conn, j["id"], PID, "B-01")
        link_ticket(conn, j["id"], PID, "B-01")  # Should not raise

        links = conn.execute(
            "SELECT * FROM journey_tickets WHERE journey_id = ?", (j["id"],)
        ).fetchall()
        assert len(links) == 1


# ===========================================================================
# Constants
# ===========================================================================

class TestInferJourneys:
    def _seed_tickets(self, conn, sections):
        """Create tickets in the given sections. Returns count."""
        for i, section in enumerate(sections):
            conn.execute(
                "INSERT INTO tickets (id, project_id, title, section) VALUES (?, ?, ?, ?)",
                (f"T-{i+1:02d}", PID, f"Ticket in {section}", section),
            )
        return len(sections)

    def test_infer_empty_project(self, conn):
        result = infer_journeys(conn, PID)
        assert result == []

    def test_infer_always_includes_overview(self, conn):
        self._seed_tickets(conn, ["Backlog"])
        result = infer_journeys(conn, PID)
        titles = [s["title"] for s in result]
        assert "Board Overview" in titles

    def test_infer_creates_feature_journey_from_backlog(self, conn):
        self._seed_tickets(conn, ["Backlog"])
        result = infer_journeys(conn, PID)
        assert any("Create Feature" in s["title"] for s in result)

    def test_infer_creates_inspect_journey_from_wip(self, conn):
        self._seed_tickets(conn, ["WIP"])
        result = infer_journeys(conn, PID)
        assert any("Inspect" in s["title"] for s in result)

    def test_infer_creates_verify_journey_from_done(self, conn):
        self._seed_tickets(conn, ["Done"])
        result = infer_journeys(conn, PID)
        assert any("Verify Done" in s["title"] for s in result)

    def test_infer_suggestions_have_steps(self, conn):
        self._seed_tickets(conn, ["Backlog", "WIP"])
        result = infer_journeys(conn, PID)
        for suggestion in result:
            assert len(suggestion["steps"]) > 0
            assert suggestion["actors_json"]
            assert suggestion["title"]


class TestJourneyConstants:
    def test_journey_statuses(self):
        assert "draft" in JOURNEY_STATUSES
        assert "active" in JOURNEY_STATUSES
        assert "validated" in JOURNEY_STATUSES
        assert "archived" in JOURNEY_STATUSES
