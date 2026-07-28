"""Render a workflow trigger_json into a plain-English sentence.

Used by /workflows page rendering and the future ticket-detail trigger preview.
The translator covers every predicate kind in conditions.CONDITION_CATALOG and
gracefully renders unknown kinds verbatim so a missing translation never breaks
the page.

Output is one sentence: 'When <predicate1>, <predicate2>, AND <predicateN>.'
For any_of we use 'OR' between predicates. Manual workflows (trigger_json is
None or empty) render as 'Manual run only — does not auto-fire.'
"""

from __future__ import annotations

import json
from typing import Any


def _humanize_section(s: str) -> str:
    return s


def _humanize_status(s: str) -> str:
    return str(s).replace("-", " ")


def _humanize_field(s: str) -> str:
    mapping = {
        "description": "a description",
        "summary": "a summary",
        "title": "a title",
        "commit_hash": "a commit hash",
    }
    return mapping.get(s, f"a {s}")


def _humanize_flag(s: str) -> str:
    mapping = {
        "D": "Description",
        "C": "Criteria",
        "L": "Learnings",
        "description": "Description",
        "criteria": "Criteria",
        "reviewed": "Learnings",
    }
    return mapping.get(s, str(s))


def _join_quoted(values: Any, joiner: str = " or ") -> str:
    if not isinstance(values, list):
        values = [values]
    rendered = [f"“{v}”" for v in values]
    if len(rendered) == 1:
        return rendered[0]
    if len(rendered) == 2:
        return f"{rendered[0]}{joiner}{rendered[1]}"
    return ", ".join(rendered[:-1]) + f"{joiner}{rendered[-1]}"


def _describe_predicate(p: dict) -> str:
    """Render one predicate as a clause (no leading 'When', no trailing period)."""
    kind = p.get("kind")
    if kind == "section_equals":
        return f"the ticket is in {_humanize_section(p.get('value', '?'))}"
    if kind == "section_in":
        vals = p.get("values") or p.get("value") or []
        return f"the ticket is in {_join_quoted(vals, ' or ')}"
    if kind == "status_equals":
        return f"its status is “{_humanize_status(p.get('value', '?'))}”"
    if kind == "automation_mode":
        v = p.get("value", "auto")
        if v == "auto":
            return "the ticket has automation set to On"
        if v == "manual":
            return "the ticket has automation set to Off"
        return f"the ticket's automation mode is “{v}”"
    if kind == "has_field":
        return f"it has {_humanize_field(p.get('field', '?'))}"
    if kind == "criteria_count_gte":
        n = p.get("value", 1)
        return f"it has at least {n} acceptance criterion" + (
            "s" if isinstance(n, int) and n != 1 else ""
        )
    if kind == "flag_set":
        return f"the {_humanize_flag(p.get('flag', '?'))} flag is set"
    if kind == "deps_clear":
        return "all of its dependencies are done"
    if kind == "tests_covered":
        return "tests are covered"
    if kind == "spec_linked":
        return "a spec lane has been declared for it"
    if kind == "spec_validates":
        return "its OpenSpec change validates in strict mode"
    if kind == "verify_passed":
        return "its verify command passed at the current commit"
    if kind == "no_active_run":
        return "no run is already in flight for it"
    if kind == "tag_includes":
        return f"it has the tag {_join_quoted(p.get('value', '?'))}"
    if kind == "has_tag":
        vals = p.get("value") or []
        if isinstance(vals, str):
            vals = [vals]
        return f"it has tag(s) {_join_quoted(vals, ' and ')}"
    if kind == "lacks_tag":
        vals = p.get("value") or []
        if isinstance(vals, str):
            vals = [vals]
        return f"it does NOT have any of the tags {_join_quoted(vals, ' or ')}"
    if kind == "lacks_readiness_flag":
        return f"the {_humanize_flag(p.get('flag', '?'))} flag is NOT set"
    if kind == "priority_at_least":
        return f"its priority is at least “{p.get('value', '?')}”"
    if kind == "parent_done":
        return "its parent ticket is done (or it has no parent)"
    if kind == "children_have_open_bugs":
        return "it has children with open bugs"
    if kind == "children_no_open_bugs":
        return "all of its child bugs are resolved"
    if kind == "children_all_status_in":
        vals = p.get("value") or []
        rendered = _join_quoted([_humanize_status(v) for v in vals], " or ")
        return f"every child has status {rendered}"
    if kind == "children_any_status_in":
        vals = p.get("value") or []
        rendered = _join_quoted([_humanize_status(v) for v in vals], " or ")
        return f"any child has status {rendered}"
    if kind == "has_children":
        return "it has children"
    if kind == "parent_section_not_in":
        vals = p.get("value") or []
        return f"its parent is NOT in {_join_quoted(vals, ' or ')}"
    if kind == "summary_stale":
        return "the cached summary is stale (content changed since last summary)"
    return f"({kind})"


def describe_trigger(trigger_json: dict | str | None) -> str:
    """Convert a trigger_json into a plain-English sentence.

    Empty / None / 'null' → 'Manual run only — does not auto-fire.'
    Predicate-only top-level dicts are treated as a single-predicate trigger.
    Nested all_of / any_of are flattened with 'AND' / 'OR' joiners.
    """
    if trigger_json in (None, "", "null"):
        return "Manual run only — does not auto-fire."
    if isinstance(trigger_json, str):
        try:
            trigger_json = json.loads(trigger_json)
        except (json.JSONDecodeError, TypeError):
            return "Manual run only — does not auto-fire."
    if not trigger_json:
        return "Manual run only — does not auto-fire."

    if "all_of" in trigger_json:
        parts = trigger_json["all_of"]
        joiner = " AND "
    elif "any_of" in trigger_json:
        parts = trigger_json["any_of"]
        joiner = " OR "
    else:
        return f"When {_describe_predicate(trigger_json)}."

    clauses: list[str] = []
    for p in parts:
        if isinstance(p, dict) and ("all_of" in p or "any_of" in p):
            inner = describe_trigger(p)
            inner = inner.removeprefix("When ")
            inner = inner.removesuffix(".")
            clauses.append(f"({inner})")
        else:
            clauses.append(_describe_predicate(p))

    if not clauses:
        return "Manual run only — does not auto-fire."
    if len(clauses) == 1:
        return f"When {clauses[0]}."
    return "When " + ", ".join(clauses[:-1]) + joiner + clauses[-1] + "."


_PREDICATE_LABELS = {
    "section_equals": "Section is",
    "section_in": "Section is one of",
    "status_equals": "Status is",
    "automation_mode": "Automation mode is",
    "has_field": "Field is non-empty",
    "criteria_count_gte": "Acceptance criteria count ≥",
    "flag_set": "Readiness flag is set",
    "deps_clear": "All dependencies are done",
    "tests_covered": "Tests are covered",
    "spec_linked": "Spec lane is declared",
    "spec_validates": "OpenSpec change validates (--strict)",
    "verify_passed": "Verify command passed at HEAD",
    "no_active_run": "No run already in flight",
    "tag_includes": "Has tag",
    "has_tag": "Has all tags",
    "lacks_tag": "Has none of the tags",
    "lacks_readiness_flag": "Readiness flag is NOT set",
    "priority_at_least": "Priority is at least",
    "parent_done": "Parent is done (or none)",
    "children_have_open_bugs": "Children include open bugs",
    "children_no_open_bugs": "All child bugs resolved",
    "children_all_status_in": "Every child has status",
    "children_any_status_in": "Any child has status",
    "has_children": "Has children",
    "parent_section_not_in": "Parent section NOT in",
    "summary_stale": "Cached summary is stale",
}


def _predicate_value(p: dict) -> str:
    """Render the value side of a predicate as a short string ('Backlog', 'auto', etc).

    Empty string when the predicate is parameter-less ('All dependencies are done').
    """
    kind = p.get("kind")
    if kind in (
        "deps_clear",
        "tests_covered",
        "no_active_run",
        "parent_done",
        "children_have_open_bugs",
        "children_no_open_bugs",
        "has_children",
        "summary_stale",
        "spec_linked",
        "spec_validates",
        "verify_passed",
    ):
        return ""
    if kind == "automation_mode":
        v = p.get("value")
        return {"auto": "On", "manual": "Off", "paused": "Paused"}.get(v, str(v))
    if kind == "has_field":
        return _humanize_field(p.get("field", "?"))
    if kind in ("flag_set", "lacks_readiness_flag"):
        return _humanize_flag(p.get("flag", "?"))
    if kind in ("status_equals",):
        return _humanize_status(p.get("value", "?"))
    if kind == "criteria_count_gte":
        return str(p.get("value", 1))
    if kind == "priority_at_least":
        return str(p.get("value", "?"))
    # Multi-value predicates
    vals = p.get("values") if "values" in p else p.get("value")
    if vals is None:
        return ""
    if isinstance(vals, list):
        if all(isinstance(v, str) for v in vals):
            return (
                ", ".join(vals)
                if len(vals) <= 6
                else ", ".join(vals[:6]) + f", +{len(vals) - 6} more"
            )
        return str(vals)
    return str(vals)


def predicate_rows(trigger_json: dict | str | None) -> list[tuple[str, str, bool]]:
    """Return [(label, value, is_negation_or_special), …] for the Edit panel.

    Used to render the trigger structure as a read-only list inside the Edit
    panel, so users can see the rule logic at a glance even on zero-step
    workflows where "No agent step" might otherwise look like "no logic".
    Returns an empty list for manual workflows.
    """
    if trigger_json in (None, "", "null"):
        return []
    if isinstance(trigger_json, str):
        try:
            trigger_json = json.loads(trigger_json)
        except (json.JSONDecodeError, TypeError):
            return []
    if not trigger_json:
        return []

    if "all_of" in trigger_json:
        items = trigger_json["all_of"]
    elif "any_of" in trigger_json:
        items = trigger_json["any_of"]
    else:
        items = [trigger_json]

    rows: list[tuple[str, str, bool]] = []
    for p in items:
        if not isinstance(p, dict):
            continue
        if "all_of" in p or "any_of" in p:
            # Nested groups — flatten one level for display
            inner = predicate_rows(p)
            rows.extend(inner)
            continue
        kind = p.get("kind", "")
        label = _PREDICATE_LABELS.get(kind, kind or "(unknown)")
        value = _predicate_value(p)
        # 'lacks_*' predicates are negations — the UI will style them differently
        is_negation = kind.startswith("lacks_") or "no_" in kind
        rows.append((label, value, is_negation))
    return rows


def effect_rows(on_success_json: dict | str | None) -> list[tuple[str, str]]:
    """Return [(label, value), …] for the on_success effects section.

    Empty list when the workflow has no effects (the agent's run output is
    the entire payload).
    """
    if on_success_json in (None, "", "null"):
        return []
    if isinstance(on_success_json, str):
        try:
            on_success_json = json.loads(on_success_json)
        except (json.JSONDecodeError, TypeError):
            return []
    if not on_success_json:
        return []

    apply_to = (on_success_json.get("apply_to") or "self").lower()
    target_label = "parent ticket" if apply_to == "parent" else "ticket"

    rows: list[tuple[str, str]] = []
    move_to = on_success_json.get("move_section") or on_success_json.get("move_to")
    if move_to:
        rows.append((f"Move {target_label} to", move_to))
    if on_success_json.get("set_status"):
        rows.append(("Set status to", on_success_json["set_status"]))
    if on_success_json.get("add_tags"):
        tags = on_success_json["add_tags"]
        rows.append(
            ("Add tag(s)", ", ".join(tags) if isinstance(tags, list) else str(tags))
        )
    if on_success_json.get("remove_tags"):
        tags = on_success_json["remove_tags"]
        rows.append(
            ("Remove tag(s)", ", ".join(tags) if isinstance(tags, list) else str(tags))
        )
    if on_success_json.get("accept_ticket"):
        rows.append((f"Accept {target_label}", ""))
    sr = on_success_json.get("set_readiness_content")
    if isinstance(sr, dict) and sr.get("flag"):
        flag = _humanize_flag(sr.get("flag"))
        src = sr.get("from", "stdout")
        src_desc = "the agent's stdout" if src == "stdout" else f"“{src}”"
        rows.append((f"Write {src_desc} into", f"{flag} flag"))
    return rows


def describe_on_success(on_success_json: dict | str | None) -> str:
    """Render on_success effects as a short sentence ('Then …').

    Returns '' if there are no effects.
    """
    if on_success_json in (None, "", "null"):
        return ""
    if isinstance(on_success_json, str):
        try:
            on_success_json = json.loads(on_success_json)
        except (json.JSONDecodeError, TypeError):
            return ""
    if not on_success_json:
        return ""

    apply_to = (on_success_json.get("apply_to") or "self").lower()
    target = "the parent ticket" if apply_to == "parent" else "the ticket"

    parts: list[str] = []
    move_to = on_success_json.get("move_section") or on_success_json.get("move_to")
    if move_to:
        parts.append(f"move {target} to {move_to}")
    if on_success_json.get("set_status"):
        parts.append(f"set status to “{on_success_json['set_status']}”")
    if on_success_json.get("add_tags"):
        parts.append(f"add tag(s) {_join_quoted(on_success_json['add_tags'], ' and ')}")
    if on_success_json.get("remove_tags"):
        parts.append(
            f"remove tag(s) {_join_quoted(on_success_json['remove_tags'], ' and ')}"
        )
    if on_success_json.get("accept_ticket"):
        parts.append(f"accept {target}")
    sr = on_success_json.get("set_readiness_content")
    if isinstance(sr, dict) and sr.get("flag"):
        flag = _humanize_flag(sr.get("flag"))
        src = sr.get("from", "stdout")
        src_desc = "the agent's output" if src == "stdout" else f"“{src}”"
        parts.append(f"write {src_desc} into the {flag} flag")

    if not parts:
        return ""
    if len(parts) == 1:
        return f"Then {parts[0]}."
    return "Then " + ", ".join(parts[:-1]) + ", and " + parts[-1] + "."
