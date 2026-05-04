"""Condition catalog for workflow triggers.

Each entry maps a `kind` string to a definition: human label, parameter schema
(for the UI builder), and an evaluator function.  The evaluator receives a
SubjectContext dict with the ticket's full state and returns (passed: bool, reason: str).

Keys for the SubjectContext dict (caller responsible for assembling):
  ticket:             dict (full ticket row + computed fields; must include all
                      columns that are present on the tickets table, plus
                      project_id, no_test_required, no_test_required_note)
  ticket_row:         sqlite3.Row | None  (raw Row, used by _tests_covered)
  automation_subject: dict | None  (automation_subjects row, or None)
  project_id:         str
  db:                 sqlite3.Connection
  active_run:         bool
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


# ---------------------------------------------------------------------------
# Priority ordering (low → high)
# ---------------------------------------------------------------------------

_PRIORITY_ORDER = ["low", "medium", "high", "critical"]


def _priority_rank(p: str) -> int:
    try:
        return _PRIORITY_ORDER.index((p or "medium").lower())
    except ValueError:
        return 1  # treat unknown as medium


# ---------------------------------------------------------------------------
# Dict-compatible Row wrapper (lets us pass plain dicts where sqlite3.Row needed)
# ---------------------------------------------------------------------------

class _DictRow:
    """Minimal wrapper around a dict that supports item access like sqlite3.Row."""

    def __init__(self, d: dict):
        self._d = d

    def __getitem__(self, key: str) -> Any:
        return self._d.get(key)

    def keys(self):
        return self._d.keys()


# ---------------------------------------------------------------------------
# Evaluator implementations
# ---------------------------------------------------------------------------

def _eval_automation_mode(ctx: dict, p: dict) -> tuple[bool, str]:
    """Check automation_mode from automation_subject row (or 'manual' if absent)."""
    subj = ctx.get("automation_subject")
    mode = subj["automation_mode"] if subj else "manual"
    need = p.get("value", "auto")
    return (mode == need, f"automation_mode is {mode!r}, need {need!r}")


def _eval_criteria_count_gte(ctx: dict, p: dict) -> tuple[bool, str]:
    """Count acceptance_criteria rows for this ticket."""
    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    count = db.execute(
        "SELECT COUNT(*) FROM acceptance_criteria WHERE ticket_id = ? AND project_id = ?",
        (ticket["id"], ticket["project_id"]),
    ).fetchone()[0]
    need = int(p.get("value", 1))
    return (count >= need, f"criteria count is {count}, need >= {need}")


def _eval_flag_set(ctx: dict, p: dict) -> tuple[bool, str]:
    """Check if a readiness flag row exists and has non-empty content."""
    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    flag = p.get("flag", "")
    # Map UI flag labels (D/C/S/T/L) to DB flag names
    _flag_map = {
        "D": "description",
        "C": "criteria",
        "S": "smoke",
        "T": "tests",
        "L": "learnings",
    }
    db_flag = _flag_map.get(flag, flag.lower())
    row = db.execute(
        "SELECT content FROM readiness_flags WHERE ticket_id = ? AND project_id = ? AND flag = ?",
        (ticket["id"], ticket["project_id"], db_flag),
    ).fetchone()
    if row and (row["content"] or "").strip():
        return (True, f"readiness flag {flag!r} is set")
    return (False, f"readiness flag {flag!r} is not set or has no content")


def _eval_deps_clear(ctx: dict, p: dict) -> tuple[bool, str]:
    """Delegate to actions._deps_clear — IDENTICAL logic, same source of truth."""
    from actions import _deps_clear  # type: ignore[import]

    ticket = ctx["ticket"]
    ok, blocking = _deps_clear(ctx["db"], ticket["project_id"], ticket["id"])
    if ok:
        return (True, "all dependencies are done")
    return (False, "; ".join(blocking))


def _eval_tests_covered(ctx: dict, p: dict) -> tuple[bool, str]:
    """Delegate to actions._tests_covered — IDENTICAL logic, same source of truth."""
    from actions import _tests_covered  # type: ignore[import]

    ticket_row = ctx.get("ticket_row")  # sqlite3.Row if available
    if ticket_row is not None:
        ok, reasons = _tests_covered(ctx["db"], ticket_row)
    else:
        # Build a minimal dict-wrapper that looks like a Row to _tests_covered.
        ok, reasons = _tests_covered(ctx["db"], _DictRow(ctx["ticket"]))
    if ok:
        return (True, reasons[0] if reasons else "tests covered")
    return (False, "; ".join(reasons) if reasons else "tests not covered")


def _eval_tag_includes(ctx: dict, p: dict) -> tuple[bool, str]:
    """Check if the ticket has a specific tag."""
    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    tag = p.get("value", "")
    row = db.execute(
        "SELECT 1 FROM ticket_tags WHERE ticket_id = ? AND project_id = ? AND tag = ?",
        (ticket["id"], ticket["project_id"], tag),
    ).fetchone()
    return (row is not None, f"tag {tag!r} {'found' if row else 'not found'}")


def _eval_parent_done(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if ticket has no parent, or parent is in Done section."""
    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    parent_id = ticket.get("parent")
    if not parent_id:
        return (True, "no parent ticket")
    row = db.execute(
        "SELECT section FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
        (parent_id, ticket["project_id"]),
    ).fetchone()
    if not row:
        return (False, f"parent {parent_id!r} not found")
    if row["section"] == "Done":
        return (True, f"parent {parent_id!r} is Done")
    return (False, f"parent {parent_id!r} section is {row['section']!r}, not Done")


def _eval_children_have_open_bugs(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if this ticket has at least one child bug whose status is non-terminal.

    Wraps actions._has_open_bugs to keep the predicate single-sourced.
    """
    from actions import _has_open_bugs  # type: ignore[import]

    ticket = ctx["ticket"]
    has = _has_open_bugs(ctx["db"], ticket["project_id"], ticket["id"])
    if has:
        return (True, "ticket has open child bug(s)")
    return (False, "no open child bugs")


def _eval_children_no_open_bugs(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if this ticket has no child bugs in a non-terminal status.

    Vacuously true when the ticket has no children at all.
    """
    from actions import _has_open_bugs  # type: ignore[import]

    ticket = ctx["ticket"]
    has = _has_open_bugs(ctx["db"], ticket["project_id"], ticket["id"])
    if has:
        return (False, "ticket has open child bug(s)")
    return (True, "no open child bugs")


def _eval_children_all_status_in(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if every child of this ticket has status in the selected set.

    Vacuously true when the ticket has no children — combine with a
    `has_children` condition (or check upstream) if the empty case must fail.
    """
    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    wanted = p.get("value") or p.get("values") or []
    if isinstance(wanted, str):
        wanted = [wanted]
    wanted_set = set(wanted)
    children = db.execute(
        "SELECT status FROM tickets WHERE parent = ? AND project_id = ?",
        (ticket["id"], ticket["project_id"]),
    ).fetchall()
    if not children:
        return (True, "no children (vacuously true)")
    bad = [c["status"] for c in children if c["status"] not in wanted_set]
    if not bad:
        return (True, f"all {len(children)} children in {sorted(wanted_set)}")
    return (False, f"{len(bad)} child(ren) not in {sorted(wanted_set)} (e.g. {bad[0]!r})")


def _eval_children_any_status_in(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if at least one child of this ticket has status in the selected set."""
    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    wanted = p.get("value") or p.get("values") or []
    if isinstance(wanted, str):
        wanted = [wanted]
    wanted_set = set(wanted)
    children = db.execute(
        "SELECT status FROM tickets WHERE parent = ? AND project_id = ?",
        (ticket["id"], ticket["project_id"]),
    ).fetchall()
    if not children:
        return (False, "no children")
    matching = [c["status"] for c in children if c["status"] in wanted_set]
    if matching:
        return (True, f"{len(matching)} of {len(children)} child(ren) in {sorted(wanted_set)}")
    return (False, f"no child status in {sorted(wanted_set)}")


def _eval_has_children(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if this ticket has at least one child ticket."""
    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    row = db.execute(
        "SELECT 1 FROM tickets WHERE parent = ? AND project_id = ? LIMIT 1",
        (ticket["id"], ticket["project_id"]),
    ).fetchone()
    if row:
        return (True, "ticket has children")
    return (False, "no children")


def _eval_parent_section_not_in(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if the ticket's parent is NOT in any of the named sections.

    Vacuously true when the ticket has no parent.
    """
    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    parent_id = ticket.get("parent")
    if not parent_id:
        return (True, "no parent ticket")
    sections = p.get("value") or p.get("values") or []
    if isinstance(sections, str):
        sections = [sections]
    sections_set = set(sections)
    row = db.execute(
        "SELECT section FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
        (parent_id, ticket["project_id"]),
    ).fetchone()
    if not row:
        return (True, f"parent {parent_id!r} not found (vacuously true)")
    if row["section"] in sections_set:
        return (False, f"parent {parent_id!r} section is {row['section']!r}, in excluded set")
    return (True, f"parent {parent_id!r} section is {row['section']!r}, not in {sorted(sections_set)}")


# ---------------------------------------------------------------------------
# Condition catalog
# ---------------------------------------------------------------------------

# Each entry shape:
#   "kind": {
#       "label":     str                         — human-readable label
#       "params":    list[dict]                  — parameter definitions for UI
#       "evaluator": (ctx: dict, params: dict) -> (bool, str)
#   }
#
# Evaluator returns (passed, reason_string).  reason_string explains why the
# condition passed or failed (used for diagnostics / UI tooltips).

_SECTION_OPTIONS = ["Ideas", "Backlog", "WIP", "For Review", "Done", "Bugs", "Icebox", "Won't Do"]
_AUTOMATION_MODE_OPTIONS = ["manual", "auto", "held"]

# Imported lazily inside the dict so test runs that don't have constants on path
# still load the catalog without raising. The UI uses options to render dropdowns.
try:
    from constants import STATUSES as _STATUS_OPTIONS  # type: ignore
except Exception:  # pragma: no cover
    _STATUS_OPTIONS = [
        "proposed", "specified", "ready",
        "in-progress", "blocked", "rework",
        "for-review", "done", "released",
        "bug", "bug-fixed", "icebox", "wontdo",
    ]


CONDITION_CATALOG: dict[str, dict[str, Any]] = {
    "section_equals": {
        "label": "Section is",
        "params": [{"name": "value", "type": "section_select", "options": _SECTION_OPTIONS}],
        "evaluator": lambda ctx, p: (
            ctx["ticket"]["section"] == p["value"],
            f"section is {ctx['ticket']['section']!r}, need {p['value']!r}",
        ),
    },
    "section_in": {
        "label": "Section is one of",
        "params": [{"name": "values", "type": "section_multi_select", "options": _SECTION_OPTIONS}],
        "evaluator": lambda ctx, p: (
            ctx["ticket"]["section"] in p.get("values", []),
            f"section is {ctx['ticket']['section']!r}, need one of {p.get('values', [])}",
        ),
    },
    "status_equals": {
        "label": "Status is",
        "params": [{"name": "value", "type": "status_select", "options": _STATUS_OPTIONS}],
        "evaluator": lambda ctx, p: (
            ctx["ticket"]["status"] == p["value"],
            f"status is {ctx['ticket']['status']!r}, need {p['value']!r}",
        ),
    },
    "automation_mode": {
        "label": "Automation mode is",
        "params": [{"name": "value", "type": "automation_mode_select", "options": _AUTOMATION_MODE_OPTIONS}],
        "evaluator": _eval_automation_mode,
    },
    "has_field": {
        "label": "Field is non-empty",
        "params": [
            {
                "name": "field",
                "type": "field_select",
                "options": ["description", "summary", "title", "commit_hash"],
            }
        ],
        "evaluator": lambda ctx, p: (
            bool((ctx["ticket"].get(p["field"]) or "").strip()),
            (
                f"field {p['field']!r} is non-empty"
                if (ctx["ticket"].get(p["field"]) or "").strip()
                else f"field {p['field']!r} is empty"
            ),
        ),
    },
    "criteria_count_gte": {
        "label": "Acceptance criteria count >=",
        "params": [{"name": "value", "type": "number", "min": 0}],
        "evaluator": _eval_criteria_count_gte,
    },
    "flag_set": {
        "label": "Readiness flag is set",
        "params": [
            {
                "name": "flag",
                "type": "flag_select",
                "options": ["D", "C", "S", "T", "L"],
            }
        ],
        "evaluator": _eval_flag_set,
    },
    "deps_clear": {
        "label": "All dependencies are done",
        "params": [],
        "evaluator": _eval_deps_clear,
    },
    "tests_covered": {
        "label": "Tests are covered",
        "params": [],
        "evaluator": _eval_tests_covered,
    },
    "no_active_run": {
        "label": "No active run",
        "params": [],
        "evaluator": lambda ctx, p: (
            not ctx.get("active_run", False),
            (
                "no active run"
                if not ctx.get("active_run", False)
                else "active run exists"
            ),
        ),
    },
    "tag_includes": {
        "label": "Ticket has tag",
        "params": [{"name": "value", "type": "text"}],
        "evaluator": _eval_tag_includes,
    },
    "priority_at_least": {
        "label": "Priority is at least",
        "params": [
            {
                "name": "value",
                "type": "priority_select",
                "options": ["low", "medium", "high", "critical"],
            }
        ],
        "evaluator": lambda ctx, p: (
            _priority_rank(ctx["ticket"].get("priority", "medium"))
            >= _priority_rank(p.get("value", "medium")),
            (
                f"priority {ctx['ticket'].get('priority', 'medium')!r} >= {p.get('value', 'medium')!r}"
                if _priority_rank(ctx["ticket"].get("priority", "medium"))
                >= _priority_rank(p.get("value", "medium"))
                else f"priority {ctx['ticket'].get('priority', 'medium')!r} is below {p.get('value', 'medium')!r}"
            ),
        ),
    },
    "parent_done": {
        "label": "Parent ticket is done (or no parent)",
        "params": [],
        "evaluator": _eval_parent_done,
    },
    "children_have_open_bugs": {
        "label": "Children include open bugs",
        "params": [],
        "evaluator": _eval_children_have_open_bugs,
    },
    "children_no_open_bugs": {
        "label": "All child bugs resolved",
        "params": [],
        "evaluator": _eval_children_no_open_bugs,
    },
    "children_all_status_in": {
        "label": "All children status in",
        "params": [{"name": "value", "type": "status_multi_select", "options": _STATUS_OPTIONS}],
        "evaluator": _eval_children_all_status_in,
    },
    "children_any_status_in": {
        "label": "Any child status in",
        "params": [{"name": "value", "type": "status_multi_select", "options": _STATUS_OPTIONS}],
        "evaluator": _eval_children_any_status_in,
    },
    "has_children": {
        "label": "Ticket has children",
        "params": [],
        "evaluator": _eval_has_children,
    },
    "parent_section_not_in": {
        "label": "Parent section is not one of",
        "params": [{"name": "value", "type": "section_multi_select", "options": _SECTION_OPTIONS}],
        "evaluator": _eval_parent_section_not_in,
    },
}


# ---------------------------------------------------------------------------
# Trigger evaluation
# ---------------------------------------------------------------------------

def evaluate_condition(cond: dict, ctx: dict) -> tuple[bool, str]:
    """Evaluate a single condition dict against ctx.

    cond must have a "kind" key matching CONDITION_CATALOG.
    Extra keys are passed as params.
    Returns (passed, reason_string).
    """
    kind = cond.get("kind")
    if not kind:
        return (False, "condition missing 'kind'")
    entry = CONDITION_CATALOG.get(kind)
    if not entry:
        return (False, f"unknown condition kind {kind!r}")
    params = {k: v for k, v in cond.items() if k != "kind"}
    try:
        return entry["evaluator"](ctx, params)
    except Exception as exc:
        return (False, f"evaluator error for {kind!r}: {exc}")


def evaluate_trigger(
    trigger_json: "dict | str | None",
    ctx: dict,
) -> tuple[bool, list[str]]:
    """Evaluate a workflow's trigger expression against ctx.

    trigger_json shapes:
      {"all_of": [...conditions...]}   — all must pass (AND)
      {"any_of": [...conditions...]}   — at least one must pass (OR)
      Conditions may themselves be nested trigger objects (recursive).

    Returns (passes: bool, failure_reasons: list[str]).
    An empty trigger (None or {}) passes unconditionally.
    """
    if trigger_json is None:
        return (True, [])

    if isinstance(trigger_json, str):
        try:
            trigger_json = json.loads(trigger_json)
        except (json.JSONDecodeError, TypeError):
            return (False, [f"trigger_json is not valid JSON: {trigger_json!r}"])

    if not trigger_json:
        return (True, [])

    if "all_of" in trigger_json:
        failures: list[str] = []
        for item in trigger_json["all_of"]:
            # Recurse if item looks like a nested trigger expression
            if "all_of" in item or "any_of" in item:
                ok, sub_failures = evaluate_trigger(item, ctx)
                if not ok:
                    failures.extend(sub_failures)
            else:
                ok, reason = evaluate_condition(item, ctx)
                if not ok:
                    failures.append(reason)
        return (len(failures) == 0, failures)

    if "any_of" in trigger_json:
        reasons: list[str] = []
        for item in trigger_json["any_of"]:
            if "all_of" in item or "any_of" in item:
                ok, sub_failures = evaluate_trigger(item, ctx)
                if ok:
                    return (True, [])
                reasons.extend(sub_failures)
            else:
                ok, reason = evaluate_condition(item, ctx)
                if ok:
                    return (True, [])
                reasons.append(reason)
        return (False, reasons)

    # Plain condition shorthand: treat top-level dict as a single condition
    ok, reason = evaluate_condition(trigger_json, ctx)
    return (ok, [] if ok else [reason])


# ---------------------------------------------------------------------------
# SubjectContext builder
# ---------------------------------------------------------------------------

def build_subject_context(
    db: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
) -> dict:
    """Assemble the SubjectContext dict for a ticket.

    Returns a dict with keys: ticket, ticket_row, automation_subject,
    project_id, db, active_run.
    """
    from actions import _has_active_run  # type: ignore[import]

    ticket_row = db.execute(
        "SELECT * FROM tickets WHERE UPPER(id) = UPPER(?) AND project_id = ?",
        (ticket_id, project_id),
    ).fetchone()
    if not ticket_row:
        raise ValueError(f"Ticket {ticket_id!r} not found in project {project_id!r}")

    # Convert Row -> plain dict for general-purpose use
    ticket_dict: dict = dict(ticket_row)

    subj_row = db.execute(
        "SELECT * FROM automation_subjects "
        "WHERE project_id = ? AND subject_type = 'ticket' AND subject_id = ?",
        (project_id, ticket_id),
    ).fetchone()
    automation_subject = dict(subj_row) if subj_row else None

    active = _has_active_run(db, project_id, "ticket", ticket_id)

    return {
        "ticket": ticket_dict,
        "ticket_row": ticket_row,  # sqlite3.Row — used by _tests_covered
        "automation_subject": automation_subject,
        "project_id": project_id,
        "db": db,
        "active_run": active,
    }
