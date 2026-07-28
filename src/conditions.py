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
    # Map UI flag labels (D/C/L) to DB flag names. The L-pane is stored under
    # the legacy 'reviewed' key. Smoke and Tests collapsed into acceptance
    # criteria (migration 15) — use criteria_count_gte instead.
    _flag_map = {
        "D": "description",
        "C": "criteria",
        "L": "reviewed",
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


def _delegate(
    fn_name: str, ctx: dict, ok_default: str, fail_default: str
) -> tuple[bool, str]:
    """Delegate to an actions.py predicate — IDENTICAL logic, same source of truth.

    The spec-lifecycle predicates below are the same functions accept_ticket()
    calls. Reimplementing them here is how the engine and the gate would end up
    disagreeing, so they are not reimplemented.
    """
    import actions  # type: ignore[import]

    fn = getattr(actions, fn_name)
    ticket_row = ctx.get("ticket_row")
    subject = ticket_row if ticket_row is not None else _DictRow(ctx["ticket"])
    ok, reasons = fn(ctx["db"], subject)
    if ok:
        return (True, reasons[0] if reasons else ok_default)
    return (False, "; ".join(reasons) if reasons else fail_default)


def _eval_spec_linked(ctx: dict, p: dict) -> tuple[bool, str]:
    """Delegate to actions._spec_linked — a spec lane has been declared."""
    return _delegate("_spec_linked", ctx, "spec lane declared", "no spec lane declared")


def _eval_spec_validates(ctx: dict, p: dict) -> tuple[bool, str]:
    """Delegate to actions._spec_validates — `openspec validate --strict` exits 0."""
    return _delegate("_spec_validates", ctx, "spec validates", "spec does not validate")


def _eval_verify_passed(ctx: dict, p: dict) -> tuple[bool, str]:
    """Delegate to actions._verify_passed — verify exited 0 against current HEAD."""
    return _delegate("_verify_passed", ctx, "verify passed", "verify has not passed")


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


def _eval_has_tag(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if the ticket has ALL of the listed tags (AND semantics).

    trigger_json: {"kind": "has_tag", "value": ["foo", "bar"]}
    All listed tags must be present on the ticket.
    """
    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    tags = p.get("value") or []
    if isinstance(tags, str):
        tags = [tags]
    if not tags:
        return (True, "no tags required (vacuously true)")
    existing = {
        row["tag"]
        for row in db.execute(
            "SELECT tag FROM ticket_tags WHERE ticket_id = ? AND project_id = ?",
            (ticket["id"], ticket["project_id"]),
        ).fetchall()
    }
    missing = [t for t in tags if t not in existing]
    if missing:
        return (False, f"ticket missing required tags: {missing}")
    return (True, f"ticket has all required tags: {tags}")


def _eval_lacks_tag(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if the ticket has NONE of the listed tags.

    trigger_json: {"kind": "lacks_tag", "value": ["bar"]}
    None of the listed tags may be present on the ticket.
    """
    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    tags = p.get("value") or []
    if isinstance(tags, str):
        tags = [tags]
    if not tags:
        return (True, "no tags to exclude (vacuously true)")
    existing = {
        row["tag"]
        for row in db.execute(
            "SELECT tag FROM ticket_tags WHERE ticket_id = ? AND project_id = ?",
            (ticket["id"], ticket["project_id"]),
        ).fetchall()
    }
    found = [t for t in tags if t in existing]
    if found:
        return (False, f"ticket has excluded tags: {found}")
    return (True, f"ticket lacks all excluded tags: {tags}")


def _eval_lacks_readiness_flag(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if the named readiness flag is NOT set (or has no content).

    trigger_json: {"kind": "lacks_readiness_flag", "flag": "reviewed"}
    Inverse of flag_set — used to target tickets missing a particular flag.
    """
    # Delegate to _eval_flag_set and invert.
    passed, _reason = _eval_flag_set(ctx, p)
    if passed:
        flag = p.get("flag", "")
        return (False, f"readiness flag {flag!r} is already set")
    flag = p.get("flag", "")
    return (True, f"readiness flag {flag!r} is not set (trigger condition met)")


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
    return (
        False,
        f"{len(bad)} child(ren) not in {sorted(wanted_set)} (e.g. {bad[0]!r})",
    )


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
        return (
            True,
            f"{len(matching)} of {len(children)} child(ren) in {sorted(wanted_set)}",
        )
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


def _eval_summary_stale(ctx: dict, p: dict) -> tuple[bool, str]:
    """True if the ticket's stored summary_hash differs from a freshly computed
    hash of its summary input fields. An empty stored hash always counts as
    stale (covers brand-new tickets and the first-run backfill case).
    """
    from actions import compute_summary_hash  # type: ignore[import]

    db: sqlite3.Connection = ctx["db"]
    ticket = ctx["ticket"]
    stored = (ticket.get("summary_hash") or "").strip()
    fresh = compute_summary_hash(db, ticket["project_id"], ticket["id"])
    if not fresh:
        return (False, "ticket has no content to summarise")
    if stored != fresh:
        return (True, f"summary_hash {stored or '<empty>'!r} != fresh {fresh!r}")
    return (False, "summary is up to date")


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
        return (
            False,
            f"parent {parent_id!r} section is {row['section']!r}, in excluded set",
        )
    return (
        True,
        f"parent {parent_id!r} section is {row['section']!r}, not in {sorted(sections_set)}",
    )


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

_SECTION_OPTIONS = [
    "Ideas",
    "Backlog",
    "WIP",
    "For Review",
    "Done",
    "Bugs",
    "Icebox",
    "Won't Do",
]
_AUTOMATION_MODE_OPTIONS = ["manual", "auto", "paused"]

# Imported lazily inside the dict so test runs that don't have constants on path
# still load the catalog without raising. The UI uses options to render dropdowns.
try:
    from constants import STATUSES as _STATUS_OPTIONS  # type: ignore
except Exception:  # pragma: no cover
    _STATUS_OPTIONS = [
        "proposed",
        "specified",
        "ready",
        "in-progress",
        "blocked",
        "rework",
        "for-review",
        "done",
        "released",
        "bug",
        "bug-fixed",
        "icebox",
        "wontdo",
    ]


CONDITION_CATALOG: dict[str, dict[str, Any]] = {
    "section_equals": {
        "label": "Section is",
        "params": [
            {"name": "value", "type": "section_select", "options": _SECTION_OPTIONS}
        ],
        "evaluator": lambda ctx, p: (
            ctx["ticket"]["section"] == p["value"],
            f"section is {ctx['ticket']['section']!r}, need {p['value']!r}",
        ),
    },
    "section_in": {
        "label": "Section is one of",
        "params": [
            {
                "name": "values",
                "type": "section_multi_select",
                "options": _SECTION_OPTIONS,
            }
        ],
        "evaluator": lambda ctx, p: (
            ctx["ticket"]["section"] in p.get("values", []),
            f"section is {ctx['ticket']['section']!r}, need one of {p.get('values', [])}",
        ),
    },
    "status_equals": {
        "label": "Status is",
        "params": [
            {"name": "value", "type": "status_select", "options": _STATUS_OPTIONS}
        ],
        "evaluator": lambda ctx, p: (
            ctx["ticket"]["status"] == p["value"],
            f"status is {ctx['ticket']['status']!r}, need {p['value']!r}",
        ),
    },
    "automation_mode": {
        "label": "Automation mode is",
        "params": [
            {
                "name": "value",
                "type": "automation_mode_select",
                "options": _AUTOMATION_MODE_OPTIONS,
            }
        ],
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
                "options": ["D", "C", "L"],
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
    # --- Spec lifecycle (OpenSpec) ----------------------------------------
    # These read the `spec` / `verified` readiness flags and shell out through
    # openspec_adapter. They are the same predicates actions.accept_ticket()
    # enforces, exposed here so a user-built workflow can filter on them too.
    "spec_linked": {
        "label": "Spec lane is declared",
        "params": [],
        "evaluator": _eval_spec_linked,
    },
    "spec_validates": {
        "label": "OpenSpec change validates (--strict)",
        "params": [],
        "evaluator": _eval_spec_validates,
    },
    "verify_passed": {
        "label": "Verify command passed at HEAD",
        "params": [],
        "evaluator": _eval_verify_passed,
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
    "has_tag": {
        "label": "Ticket has all tags (list)",
        "params": [{"name": "value", "type": "tag_list"}],
        "evaluator": _eval_has_tag,
    },
    "lacks_tag": {
        "label": "Ticket has none of the tags",
        "params": [{"name": "value", "type": "tag_list"}],
        "evaluator": _eval_lacks_tag,
    },
    "lacks_readiness_flag": {
        "label": "Readiness flag is NOT set",
        "params": [
            {
                "name": "flag",
                "type": "flag_select",
                "options": ["D", "C", "L"],
            }
        ],
        "evaluator": _eval_lacks_readiness_flag,
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
        "params": [
            {"name": "value", "type": "status_multi_select", "options": _STATUS_OPTIONS}
        ],
        "evaluator": _eval_children_all_status_in,
    },
    "children_any_status_in": {
        "label": "Any child status in",
        "params": [
            {"name": "value", "type": "status_multi_select", "options": _STATUS_OPTIONS}
        ],
        "evaluator": _eval_children_any_status_in,
    },
    "has_children": {
        "label": "Ticket has children",
        "params": [],
        "evaluator": _eval_has_children,
    },
    "parent_section_not_in": {
        "label": "Parent section is not one of",
        "params": [
            {
                "name": "value",
                "type": "section_multi_select",
                "options": _SECTION_OPTIONS,
            }
        ],
        "evaluator": _eval_parent_section_not_in,
    },
    "summary_stale": {
        "label": "Cached summary is stale",
        "params": [],
        "evaluator": _eval_summary_stale,
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
    trigger_json: dict | str | None,
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


# ---------------------------------------------------------------------------
# UI catalog — unified attribute domain
# ---------------------------------------------------------------------------
#
# Bridges raw CONDITION_CATALOG keys + on_success effect keys into a single
# attribute-centric model the editor UI consumes:
#
#   {attributes: [
#     {key, label,
#      filter_ops:  [{key, label, predicate_kind, value_control, params?}, ...],
#      action_ops:  [{key, label, on_success_key, value_control, params?}, ...],
#      hint?: str,  # shown when an attribute is filter-only
#     },
#     ...
#   ],
#   apply_to_targets: [{key, label}, ...]}
#
# Closed-loop principle: every attribute ideally exposes both a filter side
# and an action side. Where the action side is impossible (intrinsic state
# like Run, Dependencies; or content set by agent steps like Description), we
# emit a `hint` so the UI shows a disabled-with-explanation row instead of
# silently hiding it.

# Value-control kinds — the UI uses these to render the right form control.
#   section_select          single-select dropdown of section names
#   section_multi_select    multi-select of section names
#   status_select           single-select of status values
#   status_multi_select     multi-select of status values
#   automation_mode_select  single-select: auto / manual / paused
#   priority_select         single-select: low / medium / high
#   bool_select             single-select: yes / no
#   tag_input               free-text-with-chips
#   tag_multi_input         many-tag chip input
#   field_select            description / summary / title / commit_hash
#   flag_select             D / C / L (description / criteria / reviewed)
#   number_input            integer >= 0
#   none                    no value control (predicate is parameter-less)
#   readiness_content_input { flag, from } — flag dropdown + from='stdout'|literal


def ui_catalog() -> dict:
    """Return the unified attribute catalog the workflow editor consumes.

    Stable shape — the UI depends on these keys. Adding new attributes is
    backward-compatible; renaming existing ones is not.
    """
    return {
        "apply_to_targets": [
            {"key": "self", "label": "the ticket"},
            {"key": "parent", "label": "the parent ticket"},
        ],
        "options": {
            "sections": _SECTION_OPTIONS,
            "statuses": _STATUS_OPTIONS,
            "automation_modes": _AUTOMATION_MODE_OPTIONS,
            "priorities": ["low", "medium", "high"],
            "fields": ["description", "summary", "title", "commit_hash"],
            "flags": [
                {"key": "D", "label": "Description"},
                {"key": "C", "label": "Criteria"},
                {"key": "L", "label": "Learnings"},
            ],
        },
        "attributes": [
            {
                "key": "section",
                "label": "Section",
                "filter_ops": [
                    {
                        "key": "is",
                        "label": "is",
                        "predicate_kind": "section_equals",
                        "value_control": "section_select",
                    },
                    {
                        "key": "is_one_of",
                        "label": "is one of",
                        "predicate_kind": "section_in",
                        "value_control": "section_multi_select",
                    },
                ],
                "action_ops": [
                    {
                        "key": "set",
                        "label": "change to",
                        "on_success_key": "move_section",
                        "value_control": "section_select",
                    },
                    {
                        "key": "accept",
                        "label": "accept (move to Done + write spec)",
                        "on_success_key": "accept_ticket",
                        "value_control": "none",
                    },
                ],
            },
            {
                "key": "status",
                "label": "Status",
                "filter_ops": [
                    {
                        "key": "is",
                        "label": "is",
                        "predicate_kind": "status_equals",
                        "value_control": "status_select",
                    },
                ],
                "action_ops": [
                    {
                        "key": "set",
                        "label": "change to",
                        "on_success_key": "set_status",
                        "value_control": "status_select",
                    },
                ],
            },
            {
                "key": "automation_mode",
                "label": "Automation",
                "filter_ops": [
                    {
                        "key": "is",
                        "label": "is",
                        "predicate_kind": "automation_mode",
                        "value_control": "automation_mode_select",
                    },
                ],
                "action_ops": [
                    {
                        "key": "set",
                        "label": "change to",
                        "on_success_key": "set_automation_mode",
                        "value_control": "automation_mode_select",
                    },
                ],
            },
            {
                "key": "priority",
                "label": "Priority",
                "filter_ops": [
                    {
                        "key": "at_least",
                        "label": "is at least",
                        "predicate_kind": "priority_at_least",
                        "value_control": "priority_select",
                    },
                ],
                "action_ops": [
                    {
                        "key": "set",
                        "label": "change to",
                        "on_success_key": "set_priority",
                        "value_control": "priority_select",
                    },
                ],
            },
            {
                "key": "tags",
                "label": "Tags",
                "filter_ops": [
                    {
                        "key": "include_all_of",
                        "label": "include all of",
                        "predicate_kind": "has_tag",
                        "value_control": "tag_multi_input",
                    },
                    {
                        "key": "include_none_of",
                        "label": "include none of",
                        "predicate_kind": "lacks_tag",
                        "value_control": "tag_multi_input",
                    },
                ],
                "action_ops": [
                    {
                        "key": "add",
                        "label": "add",
                        "on_success_key": "add_tags",
                        "value_control": "tag_multi_input",
                    },
                    {
                        "key": "remove",
                        "label": "remove",
                        "on_success_key": "remove_tags",
                        "value_control": "tag_multi_input",
                    },
                ],
            },
            {
                "key": "readiness_flag",
                "label": "Readiness flag",
                "filter_ops": [
                    {
                        "key": "is_set",
                        "label": "is set",
                        "predicate_kind": "flag_set",
                        "value_control": "flag_select",
                    },
                    {
                        "key": "is_not_set",
                        "label": "is NOT set",
                        "predicate_kind": "lacks_readiness_flag",
                        "value_control": "flag_select",
                    },
                ],
                "action_ops": [
                    {
                        "key": "set_from_stdout",
                        "label": "set from agent stdout",
                        "on_success_key": "set_readiness_content",
                        "value_control": "flag_select",
                        "extra": {"from": "stdout"},
                    },
                    {
                        "key": "clear",
                        "label": "clear",
                        "on_success_key": "clear_readiness_flag",
                        "value_control": "flag_select",
                    },
                ],
            },
            {
                "key": "is_container",
                "label": "Container",
                "filter_ops": [
                    {
                        "key": "is",
                        "label": "is",
                        "predicate_kind": "is_container",  # Not yet wired in CONDITION_CATALOG
                        "value_control": "bool_select",
                    },
                ],
                "action_ops": [
                    {
                        "key": "set",
                        "label": "change to",
                        "on_success_key": "set_is_container",
                        "value_control": "bool_select",
                    },
                ],
                "hint": "Filter side requires the is_container predicate (not yet shipped); action side works today.",
            },
            {
                "key": "criteria_count",
                "label": "Acceptance criteria count",
                "filter_ops": [
                    {
                        "key": "at_least",
                        "label": "is at least",
                        "predicate_kind": "criteria_count_gte",
                        "value_control": "number_input",
                    },
                ],
                "action_ops": [],
                "hint": "Criteria are added by the agent's `propose` marker, not by a direct workflow effect.",
            },
            {
                "key": "field_present",
                "label": "Description / Summary / Title",
                "filter_ops": [
                    {
                        "key": "is_non_empty",
                        "label": "is non-empty",
                        "predicate_kind": "has_field",
                        "value_control": "field_select",
                    },
                ],
                "action_ops": [],
                "hint": "Text fields are written by the agent step itself, not by direct workflow effects.",
            },
            {
                "key": "dependencies",
                "label": "Dependencies",
                "filter_ops": [
                    {
                        "key": "all_done",
                        "label": "are all done",
                        "predicate_kind": "deps_clear",
                        "value_control": "none",
                    },
                ],
                "action_ops": [],
                "hint": "Dependency state is intrinsic — it changes when other tickets reach Done.",
            },
            {
                "key": "tests",
                "label": "Tests",
                "filter_ops": [
                    {
                        "key": "are_covered",
                        "label": "are covered",
                        "predicate_kind": "tests_covered",
                        "value_control": "none",
                    },
                ],
                "action_ops": [],
                "hint": "Test-coverage state is intrinsic — produced by the agent's work product.",
            },
            {
                "key": "spec",
                "label": "Spec",
                "filter_ops": [
                    {
                        "key": "is_linked",
                        "label": "lane is declared",
                        "predicate_kind": "spec_linked",
                        "value_control": "none",
                    },
                    {
                        "key": "validates",
                        "label": "validates (openspec --strict)",
                        "predicate_kind": "spec_validates",
                        "value_control": "none",
                    },
                ],
                "action_ops": [],
                "hint": "Spec state is intrinsic — set by `tickets-cli.py spec` and by OpenSpec itself.",
            },
            {
                "key": "verify",
                "label": "Verify",
                "filter_ops": [
                    {
                        "key": "passed",
                        "label": "passed at HEAD",
                        "predicate_kind": "verify_passed",
                        "value_control": "none",
                    },
                ],
                "action_ops": [],
                "hint": "Verify state is evidence — recorded by `tickets-cli.py verify`, never asserted by hand.",
            },
            {
                "key": "run",
                "label": "Run",
                "filter_ops": [
                    {
                        "key": "no_active",
                        "label": "is not in flight",
                        "predicate_kind": "no_active_run",
                        "value_control": "none",
                    },
                ],
                "action_ops": [],
                "hint": "Run state is intrinsic — changed by the kitchen orchestrator.",
            },
            {
                "key": "parent",
                "label": "Parent",
                "filter_ops": [
                    {
                        "key": "done_or_absent",
                        "label": "is done (or absent)",
                        "predicate_kind": "parent_done",
                        "value_control": "none",
                    },
                    {
                        "key": "section_not_in",
                        "label": "section is NOT one of",
                        "predicate_kind": "parent_section_not_in",
                        "value_control": "section_multi_select",
                    },
                ],
                "action_ops": [],
                "hint": "To change parent attributes, set apply_to=parent on any action.",
            },
            {
                "key": "children",
                "label": "Children",
                "filter_ops": [
                    {
                        "key": "exist",
                        "label": "exist",
                        "predicate_kind": "has_children",
                        "value_control": "none",
                    },
                    {
                        "key": "all_status_in",
                        "label": "all have status in",
                        "predicate_kind": "children_all_status_in",
                        "value_control": "status_multi_select",
                    },
                    {
                        "key": "any_status_in",
                        "label": "any has status in",
                        "predicate_kind": "children_any_status_in",
                        "value_control": "status_multi_select",
                    },
                    {
                        "key": "have_open_bugs",
                        "label": "include open bugs",
                        "predicate_kind": "children_have_open_bugs",
                        "value_control": "none",
                    },
                    {
                        "key": "no_open_bugs",
                        "label": "have no open bugs",
                        "predicate_kind": "children_no_open_bugs",
                        "value_control": "none",
                    },
                ],
                "action_ops": [],
                "hint": "Acting on children directly is not yet supported — use a self-mutating effect (e.g. add a tag) to break the loop.",
            },
            {
                "key": "summary_cache",
                "label": "Cached summary",
                "filter_ops": [
                    {
                        "key": "is_stale",
                        "label": "is stale",
                        "predicate_kind": "summary_stale",
                        "value_control": "none",
                    },
                ],
                "action_ops": [
                    {
                        "key": "refresh_from_stdout",
                        "label": "refresh from agent stdout",
                        "on_success_key": "set_summary_oneliner",
                        "value_control": "none",
                    },
                ],
            },
        ],
        # Reverse-index: which attribute does each on_success key mutate? Used
        # by the linter to decide if any action's attribute matches any
        # filter's attribute (closed-loop principle).
        "effect_to_attribute": {
            "move_section": "section",
            "move_to": "section",
            "set_status": "status",
            "set_priority": "priority",
            "set_automation_mode": "automation_mode",
            "set_is_container": "is_container",
            "add_tags": "tags",
            "remove_tags": "tags",
            "set_readiness_content": "readiness_flag",
            "clear_readiness_flag": "readiness_flag",
            "set_summary_oneliner": "summary_cache",
            "accept_ticket": "section",
        },
        # Reverse-index: which attribute does each predicate kind read?
        "predicate_to_attribute": {
            "section_equals": "section",
            "section_in": "section",
            "status_equals": "status",
            "automation_mode": "automation_mode",
            "priority_at_least": "priority",
            "has_tag": "tags",
            "has_all_tags": "tags",
            "lacks_tag": "tags",
            "tag_includes": "tags",
            "flag_set": "readiness_flag",
            "lacks_readiness_flag": "readiness_flag",
            "is_container": "is_container",
            "criteria_count_gte": "criteria_count",
            "has_field": "field_present",
            "deps_clear": "dependencies",
            "tests_covered": "tests",
            "spec_linked": "spec",
            "spec_validates": "spec",
            "verify_passed": "verify",
            "no_active_run": "run",
            "parent_done": "parent",
            "parent_section_not_in": "parent",
            "has_children": "children",
            "children_all_status_in": "children",
            "children_any_status_in": "children",
            "children_have_open_bugs": "children",
            "children_no_open_bugs": "children",
            "summary_stale": "summary_cache",
        },
    }


def lint_closed_loop(
    trigger_json: dict | str | None, on_success_json: dict | str | None
) -> dict:
    """Check whether any action attribute matches any filter attribute.

    Returns a small dict the UI renders as an advisory:
      {"status": "ok"|"warn"|"manual"|"empty",
       "filter_attributes": [...],
       "action_attributes": [...],
       "shared": [...],
       "message": "..."}

    Status values:
      ok      — at least one attribute is on both sides; rule self-terminates
      warn    — filter has read attributes but no action mutates one of them
      manual  — trigger_json is null/empty (manual-only workflow)
      empty   — no on_success effects at all (likely intentional; agent does
                the work). UI may surface as info, not warning.
    """
    cat = ui_catalog()
    pred_to_attr = cat["predicate_to_attribute"]
    eff_to_attr = cat["effect_to_attribute"]

    # Normalise inputs to dicts
    def _coerce(x):
        if x in (None, "", "null"):
            return {}
        if isinstance(x, str):
            try:
                return json.loads(x)
            except (json.JSONDecodeError, TypeError):
                return {}
        return x or {}

    tj = _coerce(trigger_json)
    oj = _coerce(on_success_json)

    if not tj:
        return {
            "status": "manual",
            "filter_attributes": [],
            "action_attributes": [],
            "shared": [],
            "message": "Manual run only — no auto-fire.",
        }

    # Walk trigger predicates (flat or one-level nested all_of/any_of)
    def _collect_attrs(node) -> set:
        out = set()
        if not isinstance(node, dict):
            return out
        if "all_of" in node or "any_of" in node:
            for child in node.get("all_of") or node.get("any_of") or []:
                out |= _collect_attrs(child)
            return out
        kind = node.get("kind")
        if kind and kind in pred_to_attr:
            out.add(pred_to_attr[kind])
        return out

    filter_attrs = _collect_attrs(tj)
    # Action attributes
    action_attrs: set = set()
    for k, v in oj.items() if isinstance(oj, dict) else []:
        if k == "apply_to":
            continue
        if k in eff_to_attr and v:
            action_attrs.add(eff_to_attr[k])

    if not action_attrs:
        return {
            "status": "empty",
            "filter_attributes": sorted(filter_attrs),
            "action_attributes": [],
            "shared": [],
            "message": "No on_success effects — the agent step is the entire payload.",
        }

    shared = filter_attrs & action_attrs
    if shared:
        return {
            "status": "ok",
            "filter_attributes": sorted(filter_attrs),
            "action_attributes": sorted(action_attrs),
            "shared": sorted(shared),
            "message": f"Closed loop: actions mutate {', '.join(sorted(shared))}, "
            f"which the trigger reads.",
        }
    return {
        "status": "warn",
        "filter_attributes": sorted(filter_attrs),
        "action_attributes": sorted(action_attrs),
        "shared": [],
        "message": "Effects don't change any attribute the trigger reads — "
        "this rule may re-fire after running. Consider adding a "
        "tag effect to break the loop.",
    }
