"""TDD tests for the scenario_drafting module (Phase 5).

Validates:
- Intent detection accuracy across common phrases
- Candidate generation for each intent type
- Manifest validity (all generated manifests pass validate_manifest)
- Prerequisite detection for unautomatable flows
- Duplication warning when existing scenario has same id
- Actor resolution: default, hints, lifecycle defaults
- DraftContext defaults when omitted
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scenario_drafting import (
    DraftContext,
    DraftRequest,
    _detect_intent,
    _slugify,
    generate_drafts,
)
from scenarios import ScenarioValidationError, validate_manifest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def all_manifests_valid(result) -> list[str]:
    """Return list of validation errors; empty list means all valid."""
    errors = []
    for c in result.candidates:
        try:
            validate_manifest(dict(c.manifest), filepath="test")
        except ScenarioValidationError as exc:
            errors.append(str(exc))
    return errors


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------


class TestIntentDetection:
    def test_create_from_creates(self):
        intents = _detect_intent("user creates a ticket")
        assert "create" in intents

    def test_move_from_moves(self):
        intents = _detect_intent("user moves ticket to wip")
        assert "move" in intents

    def test_lifecycle_detected(self):
        intents = _detect_intent("full lifecycle journey end-to-end")
        assert "lifecycle" in intents

    def test_lifecycle_before_create(self):
        # lifecycle is higher priority — should be first
        intents = _detect_intent("lifecycle journey where user creates and moves")
        assert intents[0] == "lifecycle"

    def test_edit_detected(self):
        intents = _detect_intent("edit the ticket description")
        assert "edit" in intents

    def test_review_detected(self):
        intents = _detect_intent("review and accept a ticket")
        assert "review" in intents

    def test_delete_detected(self):
        intents = _detect_intent("delete a ticket from backlog")
        assert "delete" in intents

    def test_overview_is_fallback(self):
        intents = _detect_intent("random unrelated text with no keywords")
        assert "overview" in intents

    def test_multiple_intents_order(self):
        intents = _detect_intent("user creates a ticket and moves it to WIP")
        # Both move and create should be present; move comes before create in pattern order
        assert "move" in intents
        assert "create" in intents


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self):
        assert _slugify("Create Ticket") == "create-ticket"

    def test_special_chars(self):
        assert _slugify("ticket & board!") == "ticket-board"

    def test_leading_trailing_hyphens(self):
        slug = _slugify("  hello world  ")
        assert not slug.startswith("-")
        assert not slug.endswith("-")

    def test_max_length(self):
        long = "a" * 100
        assert len(_slugify(long)) <= 60


# ---------------------------------------------------------------------------
# Candidate generation and manifest validity
# ---------------------------------------------------------------------------


class TestGenerateDrafts:
    def test_create_intent_produces_candidates(self):
        req = DraftRequest(goal="create a new ticket in backlog")
        result = generate_drafts(req)
        assert len(result.candidates) >= 1
        errors = all_manifests_valid(result)
        assert errors == [], errors

    def test_move_intent_produces_candidates(self):
        req = DraftRequest(goal="move ticket from backlog to WIP")
        result = generate_drafts(req)
        assert len(result.candidates) >= 1
        errors = all_manifests_valid(result)
        assert errors == [], errors

    def test_edit_intent_produces_candidates(self):
        req = DraftRequest(goal="edit ticket description in detail overlay")
        result = generate_drafts(req)
        assert len(result.candidates) >= 1
        errors = all_manifests_valid(result)
        assert errors == [], errors

    def test_lifecycle_intent_produces_candidates(self):
        req = DraftRequest(goal="full end-to-end lifecycle")
        result = generate_drafts(req)
        assert len(result.candidates) >= 1
        errors = all_manifests_valid(result)
        assert errors == [], errors

    def test_review_intent_produces_candidates(self):
        req = DraftRequest(goal="review and accept a ticket")
        result = generate_drafts(req)
        assert len(result.candidates) >= 1
        errors = all_manifests_valid(result)
        assert errors == [], errors

    def test_delete_intent_produces_candidates(self):
        req = DraftRequest(goal="delete a ticket from the board")
        result = generate_drafts(req)
        assert len(result.candidates) >= 1
        errors = all_manifests_valid(result)
        assert errors == [], errors

    def test_overview_intent_produces_candidates(self):
        req = DraftRequest(goal="capture a board overview screenshot")
        result = generate_drafts(req)
        assert len(result.candidates) >= 1
        errors = all_manifests_valid(result)
        assert errors == [], errors

    def test_all_manifests_have_required_fields(self):
        req = DraftRequest(goal="user creates a ticket and moves it to WIP")
        result = generate_drafts(req)
        for c in result.candidates:
            m = c.manifest
            for field in ("id", "title", "tags", "actors", "seed", "steps"):
                assert field in m, f"Missing '{field}' in manifest {m.get('id', '?')}"

    def test_tags_from_request_propagated(self):
        req = DraftRequest(goal="create a ticket", tags=["showcase", "regression"])
        result = generate_drafts(req)
        for c in result.candidates:
            for tag in ["showcase", "regression"]:
                assert tag in c.manifest["tags"], (
                    f"Tag '{tag}' not in {c.manifest['tags']}"
                )

    def test_no_context_uses_defaults(self):
        req = DraftRequest(goal="create a ticket")
        result = generate_drafts(req, context=None)
        assert result.candidates

    def test_intent_summary_non_empty(self):
        req = DraftRequest(goal="create a ticket")
        result = generate_drafts(req)
        assert result.intent_summary.strip()


# ---------------------------------------------------------------------------
# Actor resolution
# ---------------------------------------------------------------------------


class TestActorResolution:
    def test_default_actor_is_user(self):
        req = DraftRequest(goal="create a ticket")
        result = generate_drafts(req)
        actors = result.candidates[0].manifest["actors"]
        assert "user" in actors

    def test_actor_hints_respected(self):
        req = DraftRequest(goal="create a ticket", actor_hints=["scheduler", "agent"])
        result = generate_drafts(req)
        actors = result.candidates[0].manifest["actors"]
        assert "scheduler" in actors
        assert "agent" in actors

    def test_lifecycle_gets_multi_actors_by_default(self):
        req = DraftRequest(goal="full lifecycle journey")
        result = generate_drafts(req)
        actors = result.candidates[0].manifest["actors"]
        assert len(actors) >= 2

    def test_review_gets_multi_actors(self):
        req = DraftRequest(goal="review workflow")
        result = generate_drafts(req)
        actors = result.candidates[0].manifest["actors"]
        assert len(actors) >= 2


# ---------------------------------------------------------------------------
# Prerequisite detection
# ---------------------------------------------------------------------------


class TestPrerequisiteDetection:
    def test_login_adds_prerequisite(self):
        req = DraftRequest(goal="user logs in and creates a ticket")
        result = generate_drafts(req)
        all_prereqs = [p for c in result.candidates for p in c.prerequisites]
        assert any("auth" in p.lower() or "login" in p.lower() for p in all_prereqs)

    def test_captcha_adds_prerequisite(self):
        req = DraftRequest(goal="user passes captcha and submits")
        result = generate_drafts(req)
        all_prereqs = [p for c in result.candidates for p in c.prerequisites]
        assert any("captcha" in p.lower() for p in all_prereqs)

    def test_no_false_positive_for_normal_goal(self):
        req = DraftRequest(goal="create a ticket in backlog")
        result = generate_drafts(req)
        # Move candidate may have a prerequisite about drag; create candidates should not
        create_candidates = [
            c for c in result.candidates if "create" in c.title.lower()
        ]
        for c in create_candidates:
            auth_prereqs = [
                p
                for p in c.prerequisites
                if "auth" in p.lower() or "login" in p.lower()
            ]
            assert auth_prereqs == []


# ---------------------------------------------------------------------------
# Duplication warning
# ---------------------------------------------------------------------------


class TestDuplicationWarning:
    def test_warns_when_id_exists(self):
        req = DraftRequest(goal="board overview screenshot")
        result = generate_drafts(req)
        # Grab the id the generator would produce
        generated_id = result.candidates[0].manifest["id"]

        existing = [
            {
                "id": generated_id,
                "title": "Existing",
                "tags": [],
                "actors": {"u": {}},
                "seed": {},
                "steps": [],
            }
        ]
        ctx = DraftContext(existing_scenarios=existing)
        result2 = generate_drafts(req, context=ctx)
        assert any(generated_id in w for w in result2.warnings)

    def test_no_warning_when_no_duplicates(self):
        req = DraftRequest(goal="create a new ticket")
        ctx = DraftContext(existing_scenarios=[])
        result = generate_drafts(req, context=ctx)
        dup_warnings = [w for w in result.warnings if "already exists" in w]
        assert dup_warnings == []


# ---------------------------------------------------------------------------
# Confidence levels
# ---------------------------------------------------------------------------


class TestConfidenceLevels:
    def test_overview_is_high_confidence(self):
        req = DraftRequest(goal="board overview")
        result = generate_drafts(req)
        assert result.candidates[0].confidence == "high"

    def test_delete_is_low_confidence(self):
        req = DraftRequest(goal="delete a ticket")
        result = generate_drafts(req)
        assert result.candidates[0].confidence == "low"

    def test_create_is_high_confidence(self):
        req = DraftRequest(goal="create a ticket in backlog")
        result = generate_drafts(req)
        create_candidates = [
            c for c in result.candidates if "create" in c.title.lower()
        ]
        assert all(c.confidence == "high" for c in create_candidates)
