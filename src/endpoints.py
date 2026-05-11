"""Model endpoint abstraction.

Owns the runtime configuration that the agents layer used to bundle
inline: command, args, prompt-template, session-resume config, and
capability advertisement.

Phase 1: only endpoint_type='cli' executes. Other types validate and
persist but raise UnsupportedEndpointType at invocation time.
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

VALID_TYPES = {"cli", "anthropic_api", "openai_api", "gemini_api", "ssh_cli"}
VALID_PROMPT_MODES = {"template", "stdin"}


class UnsupportedEndpointType(Exception):
    """Raised when build_invocation is called on a non-CLI endpoint."""


class EndpointMisconfigured(Exception):
    """Raised when an endpoint's args or other config is invalid for execution."""


@dataclass
class Endpoint:
    id: str
    name: str
    endpoint_type: str = "cli"
    command: Optional[str] = None
    args: list = field(default_factory=list)
    prompt_mode: str = "template"
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    timeout_s: int = 120
    capabilities: dict = field(default_factory=dict)
    session_config: dict = field(default_factory=dict)
    system: int = 0
    created_at: Optional[str] = None


def from_row(row: sqlite3.Row) -> Endpoint:
    """Inflate an Endpoint from a sqlite Row. JSON columns are parsed."""
    d = dict(row)
    d["args"] = json.loads(d.get("args") or "[]")
    d["capabilities"] = json.loads(d.get("capabilities") or "{}")
    d["session_config"] = json.loads(d.get("session_config") or "{}")
    return Endpoint(**d)


def list_endpoints(conn) -> list[Endpoint]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM endpoints ORDER BY system DESC, id ASC"
    ).fetchall()
    return [from_row(r) for r in rows]


def get_endpoint(conn, endpoint_id: str) -> Optional[Endpoint]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)
    ).fetchone()
    return from_row(row) if row else None


def create_endpoint(conn, ep: Endpoint) -> Endpoint:
    _validate_for_persist(ep)
    conn.execute("""
        INSERT INTO endpoints
          (id, name, endpoint_type, provider, model, base_url, api_key_env,
           command, args, prompt_mode, timeout_s, capabilities,
           session_config, system)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ep.id, ep.name, ep.endpoint_type, ep.provider, ep.model,
        ep.base_url, ep.api_key_env, ep.command,
        json.dumps(ep.args), ep.prompt_mode, ep.timeout_s,
        json.dumps(ep.capabilities), json.dumps(ep.session_config),
        ep.system,
    ))
    conn.commit()
    return get_endpoint(conn, ep.id)


def update_endpoint(conn, endpoint_id: str, **fields) -> Endpoint:
    existing = get_endpoint(conn, endpoint_id)
    if existing is None:
        raise KeyError(endpoint_id)
    if existing.system == 1:
        raise PermissionError("system_endpoint")
    # Validate the merged result
    merged = Endpoint(**{**asdict(existing), **fields})
    _validate_for_persist(merged)
    # Build dynamic UPDATE
    sets, vals = [], []
    for k, v in fields.items():
        if k in ("args", "capabilities", "session_config"):
            v = json.dumps(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(endpoint_id)
    conn.execute(
        f"UPDATE endpoints SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return get_endpoint(conn, endpoint_id)


def delete_endpoint(conn, endpoint_id: str) -> int:
    """Delete a user endpoint. Returns the count of agents that were
    unlinked by the FK SET NULL cascade. Raises PermissionError on
    system rows."""
    existing = get_endpoint(conn, endpoint_id)
    if existing is None:
        raise KeyError(endpoint_id)
    if existing.system == 1:
        raise PermissionError("system_endpoint")
    affected = conn.execute(
        "SELECT COUNT(*) FROM workflow_agents WHERE endpoint_id = ?",
        (endpoint_id,)
    ).fetchone()[0]
    conn.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
    conn.commit()
    return affected


def _validate_for_persist(ep: Endpoint) -> None:
    if not re.match(r"^[a-zA-Z0-9_-]+$", ep.id or ""):
        raise EndpointMisconfigured(
            f"endpoint id must match [a-zA-Z0-9_-]+, got {ep.id!r}")
    if ep.endpoint_type not in VALID_TYPES:
        raise EndpointMisconfigured(
            f"endpoint_type must be in {VALID_TYPES}, got {ep.endpoint_type!r}")
    if ep.prompt_mode not in VALID_PROMPT_MODES:
        raise EndpointMisconfigured(
            f"prompt_mode must be in {VALID_PROMPT_MODES}, got {ep.prompt_mode!r}")
    if ep.endpoint_type == "cli" and not ep.command:
        raise EndpointMisconfigured("cli endpoints require 'command'")
    if ep.endpoint_type.endswith("_api"):
        if not ep.provider:
            raise EndpointMisconfigured(
                f"{ep.endpoint_type} endpoints require 'provider'")
        if not ep.api_key_env:
            raise EndpointMisconfigured(
                f"{ep.endpoint_type} endpoints require 'api_key_env'")
    if not isinstance(ep.args, list):
        raise EndpointMisconfigured("args must be a list")
    for i, a in enumerate(ep.args):
        if not isinstance(a, str):
            raise EndpointMisconfigured(
                f"args[{i}] must be a string, got {type(a).__name__}")


def build_invocation(
    endpoint: Endpoint, prompt: str, *, session_id: Optional[str] = None
) -> list[str]:
    """Build the argv for executing `prompt` against `endpoint`.

    Phase 1: only endpoint_type='cli' is supported.
    See spec section 'endpoints.build_invocation contract' for details.
    """
    if endpoint.endpoint_type != "cli":
        raise UnsupportedEndpointType(
            f"endpoint {endpoint.id!r} has type {endpoint.endpoint_type!r}; "
            f"API endpoint execution is not implemented (phase 1 = CLI only)"
        )
    if endpoint.prompt_mode == "stdin":
        raise NotImplementedError(
            "prompt_mode='stdin' is reserved for a future phase"
        )
    if not isinstance(endpoint.args, list):
        raise EndpointMisconfigured(
            f"endpoint {endpoint.id!r} args must be a list, got "
            f"{type(endpoint.args).__name__}"
        )
    for i, a in enumerate(endpoint.args):
        if not isinstance(a, str):
            raise EndpointMisconfigured(
                f"endpoint {endpoint.id!r} args[{i}] must be a string"
            )

    # Session-resume path: resume_args fully replaces args
    if session_id is not None:
        resume_template = (endpoint.session_config or {}).get("resume_args")
        if resume_template:
            substituted = _substitute(
                resume_template, prompt, session_id=session_id)
            return [endpoint.command] + substituted
        log.warning(
            "endpoint=%s advertises sessions but has no resume_args "
            "template — session resume skipped", endpoint.id,
        )

    # Normal path
    substituted = _substitute(endpoint.args, prompt)
    if "{prompt}" not in " ".join(endpoint.args):
        substituted.append(prompt)
    return [endpoint.command] + substituted


def _substitute(template: list[str], prompt: str,
                *, session_id: Optional[str] = None) -> list[str]:
    out = []
    for tok in template:
        new = tok.replace("{prompt}", prompt)
        if session_id is not None:
            new = new.replace("{session_id}", session_id)
        out.append(new)
    return out
