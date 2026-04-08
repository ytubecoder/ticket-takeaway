"""Scenario manifest drafting — template-based generator.

Generates candidate scenario manifests from natural-language goal prompts.
No external dependencies; stdlib only.  No LLM calls.

Usage::

    from scenario_drafting import DraftRequest, DraftContext, generate_drafts

    req = DraftRequest(goal="user creates a ticket and moves it to WIP")
    ctx = DraftContext(
        available_testids=KNOWN_TESTIDS,
        existing_scenarios=[],
        known_routes=[""],
    )
    result = generate_drafts(req, ctx)
    for candidate in result.candidates:
        print(candidate.title, candidate.confidence)
        print(candidate.manifest)
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Known selectors — defaults used when no DraftContext is supplied
# ---------------------------------------------------------------------------

#: data-testid values currently wired up in the dashboard.
KNOWN_TESTIDS: list[str] = [
    "board-root",
    "new-ticket-btn",
    "new-ticket-title",
    "new-ticket-section",
    "new-ticket-submit",
    "column-ideas",
    "column-backlog",
    "column-wip",
    "column-review",
    "detail-overlay",
    "detail-title",
    "detail-close",
    "detail-status",
    "detail-description",
    "settings-toggle",
]

_VALID_SECTIONS = ["Ideas", "Backlog", "WIP", "Review", "Done"]

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DraftRequest:
    """Input contract for the drafting engine."""

    goal: str
    """Natural-language description of what the scenario should demonstrate."""

    actor_hints: list[str] = field(default_factory=list)
    """Optional actor names to include (e.g. ["scheduler", "agent"]).
    When empty the engine infers sensible defaults from the intent."""

    target_surface: str = ""
    """Optional surface hint: "dashboard", "settings", "detail-overlay"."""

    tags: list[str] = field(default_factory=list)
    """Optional tags to attach to generated manifests."""


@dataclass
class DraftContext:
    """Repo-awareness provided by the caller."""

    available_testids: list[str] = field(default_factory=lambda: list(KNOWN_TESTIDS))
    """data-testid values reachable in the current build."""

    existing_scenarios: list[dict] = field(default_factory=list)
    """Already-saved manifests — used to detect duplication."""

    known_routes: list[str] = field(default_factory=lambda: [""])
    """URL paths the app exposes (relative to project root)."""


@dataclass
class DraftCandidate:
    """One generated scenario variant."""

    title: str
    summary: str
    """What this scenario demonstrates."""

    manifest: dict
    """Complete JSON manifest, ready to write to tests/scenarios/{id}.json."""

    assumptions: list[str]
    """Things the drafter assumed that may not hold in every environment."""

    prerequisites: list[str]
    """Blockers: missing selectors, unautomatable flows, external dependencies."""

    confidence: str
    """'high', 'medium', or 'low'."""


@dataclass
class DraftResult:
    """Top-level output of the drafting engine."""

    intent_summary: str
    """Parsed interpretation of the goal."""

    candidates: list[DraftCandidate]

    warnings: list[str]
    """General warnings that apply to all candidates."""


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

# Each entry is (regex_pattern, intent_key).  First match wins.
_INTENT_PATTERNS: list[tuple[str, str]] = [
    (r"\b(lifecycle|journey|end.to.end|full.flow)\b", "lifecycle"),
    (r"\b(review|accept|approve)\b", "review"),
    (r"\b(delet|remov|destroy)\w*\b", "delete"),
    (r"\b(mov|drag|transition|promot)\w*\b", "move"),
    (r"\b(edit|updat|chang|modif|renam)\w*\b", "edit"),
    (r"\b(creat|add|new|mak)\w*\b", "create"),
    (r"\b(board|overview|gallery|screenshot)\b", "overview"),
]


def _detect_intent(goal: str) -> list[str]:
    """Return ordered list of intent keys matched in *goal*.

    Matching is case-insensitive.  Multiple intents can match (e.g.
    "create and move") — they are returned in pattern-definition order.
    """
    text = goal.lower()
    found: list[str] = []
    for pattern, key in _INTENT_PATTERNS:
        if re.search(pattern, text) and key not in found:
            found.append(key)
    if not found:
        found.append("overview")  # safe default
    return found


def _mention_of_section(goal: str) -> str | None:
    """Extract an explicit section name from the goal text, or None."""
    text = goal.lower()
    mapping = {
        "ideas": "Ideas",
        "idea": "Ideas",
        "backlog": "Backlog",
        "wip": "WIP",
        "in.progress": "WIP",
        "review": "Review",
        "for.review": "Review",
        "done": "Done",
    }
    for keyword, section in mapping.items():
        if re.search(r"\b" + keyword + r"\b", text):
            return section
    return None


def _slugify(text: str) -> str:
    """Convert *text* to a valid scenario id (lower-case alphanum + hyphens)."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    # Limit length
    if len(slug) > 60:
        slug = slug[:60].rstrip("-")
    return slug or "scenario"


def _resolve_actors(request: DraftRequest, intents: list[str]) -> dict[str, dict]:
    """Build the actors dict for a manifest."""
    if request.actor_hints:
        return {a: {"label": a.capitalize()} for a in request.actor_hints}

    # Infer from intent
    if "lifecycle" in intents:
        return {
            "scheduler": {"label": "Scheduler"},
            "agent": {"label": "Agent"},
            "reviewer": {"label": "Reviewer"},
        }
    if "review" in intents:
        return {
            "agent": {"label": "Agent"},
            "reviewer": {"label": "Reviewer"},
        }
    return {"user": {"label": "User"}}


def _default_actor(actors: dict) -> str:
    """Return the first (primary) actor key."""
    return next(iter(actors))


# ---------------------------------------------------------------------------
# Step template library
# ---------------------------------------------------------------------------
# Each template function takes (actor: str, **kwargs) and returns a list[dict].


def _steps_open_board(actor: str) -> list[dict]:
    return [
        {"actor": actor, "action": "open", "path": ""},
        {"actor": actor, "action": "wait_for", "target": {"testid": "board-root"}},
    ]


def _steps_capture(actor: str, name: str, publish_slot: str | None = None) -> list[dict]:
    cap: dict[str, Any] = {"name": name}
    if publish_slot:
        cap["publish_slot"] = publish_slot
    return [{"actor": actor, "action": "capture", "capture": cap}]


def _steps_create_ticket(actor: str, title: str, section: str = "Backlog") -> list[dict]:
    return [
        {"actor": actor, "action": "click", "target": {"testid": "new-ticket-btn"}},
        {
            "actor": actor,
            "action": "fill",
            "target": {"testid": "new-ticket-title"},
            "value": title,
        },
        {
            "actor": actor,
            "action": "select",
            "target": {"testid": "new-ticket-section"},
            "value": section,
        },
        {"actor": actor, "action": "click", "target": {"testid": "new-ticket-submit"}},
        {"actor": actor, "action": "assert_visible", "target": {"title": title}},
    ]


def _steps_open_detail(actor: str, title: str) -> list[dict]:
    return [
        {
            "actor": actor,
            "action": "click",
            "target": {"title": title, "open": True},
        },
        {"actor": actor, "action": "wait_for", "target": {"testid": "detail-overlay"}},
    ]


def _steps_close_detail(actor: str) -> list[dict]:
    return [{"actor": actor, "action": "click", "target": {"testid": "detail-close"}}]


def _steps_edit_description(actor: str, new_value: str) -> list[dict]:
    return [
        {"actor": actor, "action": "click", "target": {"testid": "detail-description"}},
        {
            "actor": actor,
            "action": "fill",
            "target": {"testid": "detail-description"},
            "value": new_value,
        },
        {
            "actor": actor,
            "action": "press",
            "target": {"testid": "detail-description"},
            "key": "Tab",
        },
    ]


def _steps_assert_card(actor: str, title: str) -> list[dict]:
    return [{"actor": actor, "action": "assert_visible", "target": {"title": title}}]


# ---------------------------------------------------------------------------
# Prerequisite detection
# ---------------------------------------------------------------------------

_UNAUTOMATABLE_KEYWORDS = [
    (r"sign.?up", "User sign-up requires a real auth flow — not automatable with current testids"),
    (r"log.{0,3}in\b|login\b", "Login requires auth credentials — not automatable with current testids"),
    (r"payment", "Payment flows require a real billing integration — stub or skip in scenarios"),
    (r"captcha", "CAPTCHA cannot be automated — tests must disable it in the test environment"),
    (r"\botp\b", "OTP/2FA flows require out-of-band input — not automatable"),
    (r"email.{0,20}verif|verif.{0,20}email", "Email verification requires inbox access — use a stubbed transport in tests"),
    (r"file.{0,10}upload|upload.{0,10}file", "File upload requires a real file handle — use fixture files and verify via API"),
    (r"\boauth\b", "OAuth redirect flows cannot be automated without mocking the provider"),
]


def _detect_prerequisites(goal: str, ctx: DraftContext) -> list[str]:
    """Return a list of prerequisite warnings based on the goal text."""
    text = goal.lower()
    blockers: list[str] = []
    for pattern, message in _UNAUTOMATABLE_KEYWORDS:
        if re.search(pattern, text):
            blockers.append(message)
    return blockers


# ---------------------------------------------------------------------------
# Candidate builders — one per intent
# ---------------------------------------------------------------------------


def _build_overview_candidates(
    request: DraftRequest,
    ctx: DraftContext,
    actors: dict,
    tags: list[str],
) -> list[DraftCandidate]:
    actor = _default_actor(actors)
    title = "Board overview — capture initial state"
    scenario_id = _slugify(title)
    prerequisites = _detect_prerequisites(request.goal, ctx)

    steps = (
        _steps_open_board(actor)
        + _steps_capture(actor, "board-overview", "gallery-board")
    )

    manifest = {
        "id": scenario_id,
        "title": title,
        "tags": tags,
        "actors": actors,
        "seed": {"tickets": []},
        "steps": steps,
    }

    return [
        DraftCandidate(
            title=title,
            summary="Opens the dashboard and captures a screenshot of the board in its initial state.",
            manifest=manifest,
            assumptions=["The board renders without errors in the test environment."],
            prerequisites=prerequisites,
            confidence="high",
        )
    ]


def _build_create_candidates(
    request: DraftRequest,
    ctx: DraftContext,
    actors: dict,
    tags: list[str],
) -> list[DraftCandidate]:
    actor = _default_actor(actors)
    ticket_title = "Scenario-generated ticket"
    target_section = _mention_of_section(request.goal) or "Backlog"
    prerequisites = _detect_prerequisites(request.goal, ctx)

    # Minimal variant: just create and assert visibility
    minimal_id = _slugify(f"create-ticket-{target_section.lower()}-minimal")
    minimal_steps = (
        _steps_open_board(actor)
        + _steps_create_ticket(actor, ticket_title, target_section)
        + _steps_capture(actor, "after-create")
    )
    minimal = DraftCandidate(
        title=f"Create ticket in {target_section} (minimal)",
        summary=f"Creates a ticket via the quick-create panel and confirms it appears in the {target_section} column.",
        manifest={
            "id": minimal_id,
            "title": f"Create ticket in {target_section} (minimal)",
            "tags": tags,
            "actors": actors,
            "seed": {"tickets": []},
            "steps": minimal_steps,
        },
        assumptions=[
            f"The '{target_section}' section option exists in the new-ticket-section dropdown.",
            "new-ticket-btn testid is present and triggers the create panel.",
        ],
        prerequisites=prerequisites,
        confidence="high",
    )

    # Standard variant: create → open detail → capture
    standard_id = _slugify(f"create-ticket-{target_section.lower()}")
    standard_steps = (
        _steps_open_board(actor)
        + _steps_create_ticket(actor, ticket_title, target_section)
        + _steps_open_detail(actor, ticket_title)
        + _steps_capture(actor, "new-ticket-detail", "gallery-create")
        + _steps_close_detail(actor)
    )
    standard = DraftCandidate(
        title=f"Create ticket in {target_section}",
        summary=f"Creates a ticket, opens it in the detail overlay, and captures both board and overlay states.",
        manifest={
            "id": standard_id,
            "title": f"Create ticket in {target_section}",
            "tags": tags,
            "actors": actors,
            "seed": {"tickets": []},
            "steps": standard_steps,
        },
        assumptions=[
            f"The '{target_section}' section option exists in the new-ticket-section dropdown.",
            "Clicking the card title with open:true opens the detail overlay.",
        ],
        prerequisites=prerequisites,
        confidence="high",
    )

    return [minimal, standard]


def _build_edit_candidates(
    request: DraftRequest,
    ctx: DraftContext,
    actors: dict,
    tags: list[str],
) -> list[DraftCandidate]:
    actor = _default_actor(actors)
    ticket_title = "Editable test ticket"
    prerequisites = _detect_prerequisites(request.goal, ctx)

    scenario_id = _slugify("edit-ticket-detail-overlay")
    steps = (
        _steps_open_board(actor)
        + _steps_open_detail(actor, ticket_title)
        + [{"actor": actor, "action": "assert_visible", "target": {"testid": "detail-title"}}]
        + _steps_capture(actor, "overlay-before-edit")
        + _steps_edit_description(actor, "Updated description via scenario runner")
        + _steps_capture(actor, "overlay-after-edit", "showcase-detail-edit")
        + _steps_close_detail(actor)
    )

    seed_ticket = {
        "title": ticket_title,
        "section": "Backlog",
        "status": "proposed",
        "description": "Original description text",
        "priority": "medium",
        "complexity": "M",
    }

    return [
        DraftCandidate(
            title="Edit ticket fields in detail overlay",
            summary="Opens an existing ticket's detail overlay, edits the description field, and verifies persistence via capture.",
            manifest={
                "id": scenario_id,
                "title": "Edit ticket fields in detail overlay",
                "tags": tags,
                "actors": actors,
                "seed": {"tickets": [seed_ticket]},
                "steps": steps,
            },
            assumptions=[
                "detail-description testid is a contenteditable or textarea that accepts fill.",
                "Pressing Tab triggers the blur/save handler.",
                "The seed ticket is created before the scenario runs.",
            ],
            prerequisites=prerequisites,
            confidence="high",
        )
    ]


def _build_move_candidates(
    request: DraftRequest,
    ctx: DraftContext,
    actors: dict,
    tags: list[str],
) -> list[DraftCandidate]:
    actor = _default_actor(actors)
    ticket_title = "Ticket to move"
    prerequisites = _detect_prerequisites(request.goal, ctx)

    # Determine from/to sections from goal text
    goal_lower = request.goal.lower()
    from_section = "Backlog"
    to_section = "WIP"

    # Try to extract a more specific target from the goal
    mentioned = _mention_of_section(request.goal)
    if mentioned and mentioned not in ("Backlog",):
        to_section = mentioned

    scenario_id = _slugify(f"move-ticket-{from_section.lower()}-to-{to_section.lower()}")

    # Move via drag is not directly supported as a step action; use the
    # column API / status dropdown instead.  We assert visible in target column.
    steps = (
        _steps_open_board(actor)
        + _steps_open_detail(actor, ticket_title)
        + _steps_capture(actor, "before-move")
        + _steps_close_detail(actor)
        + [
            {
                "actor": actor,
                "action": "assert_visible",
                "target": {"testid": f"column-{to_section.lower()}"},
            }
        ]
        + _steps_capture(actor, "after-move")
    )

    seed_ticket = {
        "title": ticket_title,
        "section": from_section,
        "status": "proposed",
        "priority": "medium",
        "complexity": "M",
    }

    return [
        DraftCandidate(
            title=f"Move ticket from {from_section} to {to_section}",
            summary=(
                f"Seeds a ticket in {from_section}, verifies the {to_section} column is visible, "
                "and captures the board state before and after the transition."
            ),
            manifest={
                "id": scenario_id,
                "title": f"Move ticket from {from_section} to {to_section}",
                "tags": tags,
                "actors": actors,
                "seed": {"tickets": [seed_ticket]},
                "steps": steps,
            },
            assumptions=[
                f"column-{to_section.lower()} testid is present.",
                "Drag-to-move is not used — section transitions require API or UI controls not yet mapped to testids.",
            ],
            prerequisites=(
                prerequisites
                + [
                    "Direct section-move UI (drag) is not yet automatable via testids. "
                    "Consider adding a 'move to section' button with a testid for full automation."
                ]
            ),
            confidence="medium",
        )
    ]


def _build_review_candidates(
    request: DraftRequest,
    ctx: DraftContext,
    actors: dict,
    tags: list[str],
) -> list[DraftCandidate]:
    # Reviewers look at a ticket in the Review section
    actors_dict = actors if len(actors) >= 2 else {"agent": {"label": "Agent"}, "reviewer": {"label": "Reviewer"}}
    agent = list(actors_dict.keys())[0]
    reviewer = list(actors_dict.keys())[-1]
    ticket_title = "Feature ready for review"
    prerequisites = _detect_prerequisites(request.goal, ctx)

    scenario_id = _slugify("review-ticket-workflow")

    steps = (
        _steps_open_board(agent)
        + _steps_assert_card(agent, ticket_title)
        + _steps_capture(agent, "agent-sees-review-ticket")
        + _steps_open_board(reviewer)
        + _steps_open_detail(reviewer, ticket_title)
        + _steps_capture(reviewer, "reviewer-detail-view", "gallery-review")
        + _steps_close_detail(reviewer)
    )

    seed_ticket = {
        "title": ticket_title,
        "section": "Review",
        "status": "for-review",
        "priority": "high",
        "complexity": "M",
    }

    return [
        DraftCandidate(
            title="Review workflow — agent hands off, reviewer inspects",
            summary="Demonstrates the review handoff: agent confirms the ticket is in Review, then a reviewer opens the detail overlay.",
            manifest={
                "id": scenario_id,
                "title": "Review workflow — agent hands off, reviewer inspects",
                "tags": tags,
                "actors": actors_dict,
                "seed": {"tickets": [seed_ticket]},
                "steps": steps,
            },
            assumptions=[
                "The seed ticket starts in the Review section.",
                "Both actor contexts share the same seeded DB.",
            ],
            prerequisites=prerequisites,
            confidence="high",
        )
    ]


def _build_lifecycle_candidates(
    request: DraftRequest,
    ctx: DraftContext,
    actors: dict,
    tags: list[str],
) -> list[DraftCandidate]:
    actors_dict = actors if len(actors) >= 2 else {
        "scheduler": {"label": "Scheduler"},
        "agent": {"label": "Agent"},
        "reviewer": {"label": "Reviewer"},
    }
    scheduler = list(actors_dict.keys())[0]
    agent = list(actors_dict.keys())[1] if len(actors_dict) > 1 else scheduler
    reviewer = list(actors_dict.keys())[2] if len(actors_dict) > 2 else agent
    ticket_title = "Lifecycle test ticket"
    prerequisites = _detect_prerequisites(request.goal, ctx)

    scenario_id = _slugify("ticket-full-lifecycle")

    steps = (
        # Scheduler opens board and creates ticket
        _steps_open_board(scheduler)
        + _steps_create_ticket(scheduler, ticket_title, "Backlog")
        + _steps_capture(scheduler, "scheduler-created-ticket", "gallery-board")
        # Agent opens detail
        + _steps_open_board(agent)
        + _steps_open_detail(agent, ticket_title)
        + _steps_capture(agent, "agent-picks-up-ticket", "gallery-handoff")
        + _steps_close_detail(agent)
        # Reviewer verifies card is visible
        + _steps_open_board(reviewer)
        + _steps_assert_card(reviewer, ticket_title)
        + _steps_capture(reviewer, "reviewer-board-view")
    )

    return [
        DraftCandidate(
            title="Full ticket lifecycle — create, pick up, review",
            summary=(
                "End-to-end flow: scheduler creates a ticket, agent picks it up in the detail overlay, "
                "reviewer confirms visibility on the board."
            ),
            manifest={
                "id": scenario_id,
                "title": "Full ticket lifecycle — create, pick up, review",
                "tags": tags,
                "actors": actors_dict,
                "seed": {"tickets": []},
                "steps": steps,
            },
            assumptions=[
                "All three actors share the same seeded database.",
                "Ticket created by scheduler is immediately visible to other actors.",
            ],
            prerequisites=prerequisites,
            confidence="high",
        )
    ]


def _build_delete_candidates(
    request: DraftRequest,
    ctx: DraftContext,
    actors: dict,
    tags: list[str],
) -> list[DraftCandidate]:
    actor = _default_actor(actors)
    ticket_title = "Ticket to delete"
    prerequisites = _detect_prerequisites(request.goal, ctx)
    prerequisites.append(
        "A 'delete ticket' button or testid is required for full automation — "
        "not currently listed in known testids. Add data-testid='delete-ticket-btn' to the UI."
    )

    scenario_id = _slugify("delete-ticket-via-overlay")

    steps = (
        _steps_open_board(actor)
        + _steps_open_detail(actor, ticket_title)
        + _steps_capture(actor, "before-delete")
        # Can't complete the delete without a testid — document the gap
        + _steps_close_detail(actor)
    )

    seed_ticket = {
        "title": ticket_title,
        "section": "Backlog",
        "priority": "low",
        "complexity": "S",
    }

    return [
        DraftCandidate(
            title="Delete ticket via detail overlay",
            summary="Opens the ticket detail overlay and documents the deletion flow. NOTE: incomplete until a delete testid is wired up.",
            manifest={
                "id": scenario_id,
                "title": "Delete ticket via detail overlay",
                "tags": tags,
                "actors": actors,
                "seed": {"tickets": [seed_ticket]},
                "steps": steps,
            },
            assumptions=[
                "A delete mechanism exists in the detail overlay.",
            ],
            prerequisites=prerequisites,
            confidence="low",
        )
    ]


# ---------------------------------------------------------------------------
# Duplication detection
# ---------------------------------------------------------------------------


def _similar_scenario_exists(candidate_id: str, existing: list[dict]) -> bool:
    """Return True if an existing manifest has the same id."""
    return any(m.get("id") == candidate_id for m in existing)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

#: Intent key → builder function
_BUILDERS = {
    "overview": _build_overview_candidates,
    "create": _build_create_candidates,
    "edit": _build_edit_candidates,
    "move": _build_move_candidates,
    "review": _build_review_candidates,
    "lifecycle": _build_lifecycle_candidates,
    "delete": _build_delete_candidates,
}


def generate_drafts(
    request: DraftRequest,
    context: DraftContext | None = None,
) -> DraftResult:
    """Generate draft scenario manifests from a natural-language *request*.

    Parameters
    ----------
    request:
        Describes what the scenario should demonstrate.
    context:
        Optional repo-awareness (known testids, existing manifests, routes).
        If omitted, defaults are used.

    Returns
    -------
    DraftResult
        Parsed intent, list of candidates, and any general warnings.
    """
    if context is None:
        context = DraftContext()

    intents = _detect_intent(request.goal)
    actors = _resolve_actors(request, intents)

    # Build tag list — merge caller tags with intent-derived defaults
    base_tags: list[str] = list(request.tags) if request.tags else ["e2e"]
    if "lifecycle" in intents and "regression" not in base_tags:
        base_tags.append("regression")

    intent_summary = (
        f"Detected intent(s): {', '.join(intents)}. "
        f"Actors: {', '.join(actors.keys())}. "
        f"Tags: {', '.join(base_tags)}."
    )

    warnings: list[str] = []

    # Warn if goal mentions testids that don't exist in the context
    unknown_testids = _find_unknown_testid_mentions(request.goal, context.available_testids)
    for tid in unknown_testids:
        warnings.append(
            f"Goal mentions '{tid}' but it is not in the known testid list — "
            "double-check the selector before running."
        )

    # Collect candidates from the primary intent builder
    primary_intent = intents[0]
    builder = _BUILDERS.get(primary_intent, _build_overview_candidates)
    candidates = builder(request, context, actors, base_tags)

    # For lifecycle and multi-intent goals, also add secondary candidates
    if len(intents) > 1:
        secondary_intent = intents[1]
        if secondary_intent != primary_intent:
            secondary_builder = _BUILDERS.get(secondary_intent)
            if secondary_builder:
                secondary_candidates = secondary_builder(request, context, actors, base_tags)
                # De-duplicate against already-generated candidates
                existing_ids = {c.manifest["id"] for c in candidates}
                for c in secondary_candidates:
                    if c.manifest["id"] not in existing_ids:
                        candidates.append(c)
                        existing_ids.add(c.manifest["id"])

    # Warn about duplicate scenario ids
    for candidate in candidates:
        cid = candidate.manifest.get("id", "")
        if _similar_scenario_exists(cid, context.existing_scenarios):
            warnings.append(
                f"A scenario with id '{cid}' already exists. "
                "Approve will overwrite the existing file."
            )

    # If no candidates produced, fall back to overview
    if not candidates:
        candidates = _build_overview_candidates(request, context, actors, base_tags)
        warnings.append("No specific template matched the goal — falling back to board overview.")

    return DraftResult(
        intent_summary=intent_summary,
        candidates=candidates,
        warnings=warnings,
    )


def _find_unknown_testid_mentions(goal: str, available: list[str]) -> list[str]:
    """Return testid strings mentioned in *goal* that aren't in *available*."""
    # Look for patterns like data-testid="foo" or testid:foo or [foo] that look like selectors
    found = re.findall(r"\b(data-testid|testid)[=:]['\"]?([a-z0-9_-]+)", goal.lower())
    unknown = []
    for _, tid in found:
        if tid not in available:
            unknown.append(tid)
    return unknown
