"""Kitchen attention-feed builder.

Produces the structured payload consumed by the new time-bucketed Kitchen view
(mobile + desktop). One flat, recency-sorted feed of tickets that need
attention, grouped client-side by time bucket and project.

Output schema (FROZEN — the renderer depends on this exact shape):

    {
      "paused": bool,
      "totals": {"all": int, "needs_me": int, "running": int,
                 "ready_to_delegate": int, "paused_ticket": int, "failed": int},
      "projects": [
        {"id": str, "name": str,
         "counts": {"all": int, "needs_me": int, "running": int,
                    "ready_to_delegate": int, "paused_ticket": int, "failed": int}}
      ],
      "items": [
        {
          "ticket_id": str,
          "project_id": str,
          "project_name": str,
          "title": str,
          "section": str,
          "status": str,
          "bucket": str,            # "needs_me"|"running"|"ready_to_delegate"|
                                    # "paused_ticket"|"failed"
          "time_bucket": str,       # "today"|"yesterday"|"this_week"|"older"
          "updated_at": str,        # ISO-8601 from tickets row
          "is_unread": bool,
          "automation_mode": str,   # "auto"|"manual"|"paused"
          "agent_name": str | None,
          "latest_run_status": str | None,
          "pause_reason": str | None
        }
        # newest first overall (by updated_at desc, ties broken by ticket_id desc)
      ]
    }

Bucket priority (each ticket appears in at most one bucket; first match wins):
    1. needs_me           — latest run status == "needs_input"
    2. running            — latest run status in ("preparing", "running")
    3. failed             — latest run status in ("failed", "stalled")
    4. paused_ticket      — automation_subjects.automation_mode == "paused"
    5. ready_to_delegate  — automation_mode == "auto" AND eligibility passes

Tickets matching none of the above are excluded from the feed.

Note: the key `paused_ticket` (ticket-level pause) is intentionally distinct
from the top-level `paused` (system-wide Kitchen pause), avoiding collision.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from actions import eligibility as _elig


_BUCKETS: tuple[str, ...] = (
    "needs_me",
    "running",
    "ready_to_delegate",
    "paused_ticket",
    "failed",
)


def _zero_counts() -> dict:
    return {"all": 0, **{k: 0 for k in _BUCKETS}}


def _parse_updated_at(raw: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse. Returns None on failure or empty input."""
    if not raw:
        return None
    try:
        # fromisoformat handles "YYYY-MM-DD HH:MM:SS" and "YYYY-MM-DDTHH:MM:SS"
        # plus optional fractional seconds. Strip a trailing 'Z' for safety.
        s = raw.rstrip("Z")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _time_bucket_for(updated_at: datetime | None, now: datetime) -> str:
    """Classify a timestamp into today/yesterday/this_week/older.

    Boundaries use ``now.date()`` as today's midnight reference.
    """
    if updated_at is None:
        return "older"
    today_midnight = datetime.combine(now.date(), datetime.min.time())
    yesterday_midnight = today_midnight - timedelta(days=1)
    week_floor = today_midnight - timedelta(days=7)
    if updated_at >= today_midnight:
        return "today"
    if updated_at >= yesterday_midnight:
        return "yesterday"
    if updated_at >= week_floor:
        return "this_week"
    return "older"


def _classify_bucket(
    conn,
    pid: str,
    tid: str,
    automation_mode: str,
    run_status: str | None,
) -> str | None:
    """Return the bucket name for this ticket, or None if it doesn't belong.

    Mirrors `_aggregate_kitchen_state` priorities, with the rename
    paused -> paused_ticket. Eligibility errors are swallowed silently.
    """
    if run_status == "needs_input":
        return "needs_me"
    if run_status in ("preparing", "running"):
        return "running"
    if run_status in ("failed", "stalled"):
        return "failed"
    if automation_mode == "paused":
        return "paused_ticket"
    if automation_mode == "auto":
        try:
            er = _elig(conn, pid, "ticket", tid)
            if er.eligible:
                return "ready_to_delegate"
        except Exception:
            pass
    return None


def _is_unread(
    bucket: str,
    status: str,
    updated_at: datetime | None,
    now: datetime,
) -> bool:
    """Blue-dot heuristic: actionable + recent activity in the last 24h."""
    if updated_at is None:
        return False
    if (now - updated_at) > timedelta(hours=24):
        return False
    if bucket in ("needs_me", "failed", "paused_ticket"):
        return True
    if status in ("proposed", "blocked"):
        return True
    return False


def build_attention_feed(
    conn,
    projects: list[dict],
    is_paused: bool,
    now: datetime | None = None,
) -> dict:
    """Build the attention feed payload. See module docstring for schema."""
    if now is None:
        now = datetime.utcnow()

    # Skip projects flagged unwatched (default True if missing).
    watched_projects = [p for p in (projects or []) if p.get("watched", True)]

    totals = _zero_counts()
    project_summaries: list[dict] = []
    items: list[dict] = []

    for proj in watched_projects:
        pid = proj["id"]
        pname = proj.get("name", pid)
        pcounts = _zero_counts()

        rows = conn.execute(
            "SELECT id, title, section, status, updated_at FROM tickets "
            "WHERE project_id = ? AND archived = 0 AND draft = 0",
            (pid,),
        ).fetchall()

        for t in rows:
            tid = t["id"]

            am_row = conn.execute(
                "SELECT automation_mode, pause_reason FROM automation_subjects "
                "WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ?",
                (pid, tid),
            ).fetchone()
            mode = am_row["automation_mode"] if am_row else "manual"
            pause_reason = am_row["pause_reason"] if am_row else None

            latest = conn.execute(
                "SELECT status, metadata_json FROM runs "
                "WHERE project_id = ? AND subject_type='ticket' AND subject_id=? "
                "ORDER BY id DESC LIMIT 1",
                (pid, tid),
            ).fetchone()
            run_status = latest["status"] if latest else None

            agent_name: str | None = None
            if latest is not None and latest["metadata_json"]:
                try:
                    _rm = json.loads(latest["metadata_json"])
                    if isinstance(_rm, dict):
                        agent_name = _rm.get("workflow_name")
                except (ValueError, TypeError):
                    pass

            bucket = _classify_bucket(conn, pid, tid, mode, run_status)
            if bucket is None:
                continue

            updated_raw = t["updated_at"] if "updated_at" in t.keys() else None
            updated_dt = _parse_updated_at(updated_raw)
            time_bucket = _time_bucket_for(updated_dt, now)
            is_unread = _is_unread(bucket, t["status"], updated_dt, now)

            items.append({
                "ticket_id": tid,
                "project_id": pid,
                "project_name": pname,
                "title": t["title"],
                "section": t["section"],
                "status": t["status"],
                "bucket": bucket,
                "time_bucket": time_bucket,
                "updated_at": updated_raw or "",
                "is_unread": is_unread,
                "automation_mode": mode,
                "agent_name": agent_name,
                "latest_run_status": run_status,
                "pause_reason": pause_reason,
            })

            pcounts["all"] += 1
            pcounts[bucket] += 1
            totals["all"] += 1
            totals[bucket] += 1

        project_summaries.append({
            "id": pid,
            "name": pname,
            "counts": pcounts,
        })

    # Newest first overall; tiebreak by ticket_id desc for determinism.
    items.sort(key=lambda x: (x["updated_at"], x["ticket_id"]), reverse=True)

    return {
        "paused": bool(is_paused),
        "totals": totals,
        "projects": project_summaries,
        "items": items,
    }
