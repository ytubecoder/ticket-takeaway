"""Deterministic seed helpers for scenario manifests.

Creates tickets via the serve.py API and tracks them for cleanup.
Builds a title->ID lookup map for the scenario runner to resolve
step targets that reference tickets by title or position.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class SeedResult:
    """Holds the ID maps and created-ID list produced by seed_tickets()."""

    # Maps seed ticket title -> assigned ticket ID (e.g. "My Ticket" -> "B-42")
    title_to_id: dict[str, str] = field(default_factory=dict)
    # Maps positional key -> assigned ticket ID (e.g. "ticket-0" -> "B-42")
    positional_to_id: dict[str, str] = field(default_factory=dict)
    # Ordered list of created IDs, used by cleanup_tickets() for teardown.
    created_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Local HTTP helpers (mirroring conftest.py — no import to avoid circularity)
# ---------------------------------------------------------------------------


def _api_post(api_base: str, path: str, payload: dict) -> tuple[int, dict]:
    """POST JSON to api_base + path, return (status_code, parsed_json)."""
    url = f"{api_base}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"raw": body.decode(errors="replace")}
        return e.code, parsed


def _api_put(api_base: str, path: str, payload: dict) -> tuple[int, dict]:
    """PUT JSON to api_base + path, return (status_code, parsed_json)."""
    url = f"{api_base}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"raw": body.decode(errors="replace")}
        return e.code, parsed


def _api_delete(api_base: str, path: str) -> tuple[int, dict]:
    """DELETE api_base + path, return (status_code, parsed_json)."""
    url = f"{api_base}{path}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body) if body else {}
            except Exception:
                return resp.status, {}
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"raw": body.decode(errors="replace")}
        return e.code, parsed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def seed_tickets(manifest_seed: dict, api_base: str) -> SeedResult:
    """Create seed tickets via the serve.py API and return a SeedResult.

    Parameters
    ----------
    manifest_seed:
        The ``"seed"`` dict from a scenario manifest.  Must contain a
        ``"tickets"`` list.  Each element may have:
          - title       (required)
          - section     (default: "Backlog")
          - status      (optional — applied via a follow-up PUT if non-default)
          - description (optional)
          - priority    (optional)
          - complexity  (optional)
          - criteria    (optional list[str])
    api_base:
        Base URL of the running serve.py instance including the project
        prefix, e.g. ``"http://localhost:8787/ticket-takeaway"``.

    Returns
    -------
    SeedResult
        Maps for title->id and positional->id lookups, plus a flat list of
        all created IDs for cleanup.

    Raises
    ------
    RuntimeError
        If any POST fails (non-2xx), so the caller knows seeding is incomplete
        before attempting to run steps.
    """
    tickets_spec: list[dict] = manifest_seed.get("tickets", [])
    result = SeedResult()

    for index, spec in enumerate(tickets_spec):
        title = spec["title"]
        section = spec.get("section", "Backlog")

        # Build the creation payload — only include keys the API accepts.
        payload: dict = {
            "title": title,
            "section": section.lower(),
        }
        for optional_key in ("description", "priority", "complexity"):
            if optional_key in spec:
                payload[optional_key] = spec[optional_key]

        # criteria is a list[str] in the seed spec; map to acceptance_criteria.
        if "criteria" in spec:
            payload["acceptance_criteria"] = spec["criteria"]

        status_code, body = _api_post(api_base, "/api/tickets", payload)
        if status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to seed ticket {index!r} ({title!r}): "
                f"HTTP {status_code} — {body}"
            )

        ticket_id: str | None = body.get("id")
        if not ticket_id:
            raise RuntimeError(
                f"Seed ticket {index!r} ({title!r}): API response missing 'id' field. "
                f"Response: {body}"
            )

        logger.debug("Seeded ticket %s -> %s", title, ticket_id)

        # Record in all three maps.
        result.title_to_id[title] = ticket_id
        result.positional_to_id[f"ticket-{index}"] = ticket_id
        result.created_ids.append(ticket_id)

        # If a status was requested and it differs from the section default,
        # apply it via a follow-up PUT.
        requested_status: str | None = spec.get("status")
        if requested_status:
            put_code, put_body = _api_put(
                api_base,
                f"/api/tickets/{ticket_id}",
                {"status": requested_status},
            )
            if put_code not in (200, 201):
                logger.warning(
                    "Could not set status %r on ticket %s: HTTP %s — %s",
                    requested_status,
                    ticket_id,
                    put_code,
                    put_body,
                )

    return result


def cleanup_tickets(created_ids: list[str], api_base: str) -> None:
    """DELETE each ticket created during seeding.

    Designed to run from a ``finally`` block — logs failures but never raises,
    so a seed error doesn't mask the underlying test failure.

    Parameters
    ----------
    created_ids:
        List of ticket IDs to delete (``SeedResult.created_ids``).
    api_base:
        Base URL of the running serve.py instance including the project prefix.
    """
    for ticket_id in created_ids:
        try:
            status_code, body = _api_delete(api_base, f"/api/tickets/{ticket_id}")
            if status_code not in (200, 204, 404):
                logger.warning(
                    "Unexpected status %s when deleting seed ticket %s: %s",
                    status_code,
                    ticket_id,
                    body,
                )
            else:
                logger.debug("Cleaned up seed ticket %s", ticket_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to delete seed ticket %s during cleanup: %s",
                ticket_id,
                exc,
            )
