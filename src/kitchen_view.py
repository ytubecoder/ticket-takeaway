"""Kitchen "attention feed" renderer — mobile-first time-bucketed view.

Renders a single responsive page showing tickets that need attention, grouped
by recency buckets (today/yesterday/this_week/older), with two rows of filter
chips (projects, run-states) and a sticky header with hamburger + overflow
pause/resume menu. Designed to feel like the Claude Code mobile task list.

The only public entrypoint is `render_attention_feed()`. The caller is
responsible for assembling the `state` dict (see contract in this file's
docstring) and passing in the nav-rail JS and PWA head tags.

CSS classes are prefixed `att-` (Attention) to avoid collision with the
legacy `kv-*` classes still used by the old kitchen view.
"""

from __future__ import annotations

import html as _html
import json
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fixed display order; labels match the spec.
_TIME_BUCKETS: list[tuple[str, str]] = [
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("this_week", "This week"),
    ("older", "Older"),
]

# Run-state chip catalog. The "All" chip is rendered separately because its
# count is derived from totals["all"].
_STATE_CHIPS: list[tuple[str, str]] = [
    ("needs_me", "Needs me"),
    ("running", "Running"),
    ("ready_to_delegate", "Ready to delegate"),
    ("paused_ticket", "Paused"),
    ("failed", "Failed"),
]

# Human-readable label for each Kitchen bucket. Used inline on cards so
# users can see the run-state without a separate pictorial element.
_BUCKET_LABELS: dict[str, str] = {
    "needs_me": "Needs me",
    "running": "Running",
    "ready_to_delegate": "Ready",
    "paused_ticket": "Paused",
    "failed": "Failed",
}


# ---------------------------------------------------------------------------
# Small escaping helpers (keep local to dodge serve.py import cycles)
# ---------------------------------------------------------------------------


def _t(s: Any) -> str:
    """Escape for text node context."""
    return _html.escape("" if s is None else str(s), quote=False)


def _a(s: Any) -> str:
    """Escape for attribute context."""
    return _html.escape("" if s is None else str(s), quote=True)


# ---------------------------------------------------------------------------
# Card / chip render helpers
# ---------------------------------------------------------------------------


def _card_html(item: dict) -> str:
    """Render a single ticket card as an <a>.

    Minimal layout — ticket ID + title on top, then section / kitchen-state /
    project as plain meta below. A thin left border tinted by the kitchen
    bucket is the only color cue on the card body. Blue corner dot signals
    unread / actionable. No pictorial tile, no glyph.
    """
    ticket_id = item.get("ticket_id", "")
    project_id = item.get("project_id", "")
    # The kanban listens for a #ticket/{id} hash and auto-opens that detail
    # overlay (src/generate.py::_parseTicketHash). Query form would just
    # land on the board with no overlay.
    href = f"/{project_id}/#ticket/{ticket_id}"
    bucket = item.get("bucket", "")
    time_bucket = item.get("time_bucket", "")
    is_unread = bool(item.get("is_unread"))
    title = item.get("title", "")
    project_name = item.get("project_name", project_id)
    section = item.get("section", "")
    bucket_label = _BUCKET_LABELS.get(bucket, "")

    meta_parts: list[str] = []
    if section:
        meta_parts.append(f'<span class="att-card-section">{_t(section)}</span>')
    if bucket_label:
        meta_parts.append(f'<span class="att-card-bucket">{_t(bucket_label)}</span>')
    if project_name:
        meta_parts.append(f'<span class="att-card-proj">{_t(project_name)}</span>')
    meta_inner = '<span class="att-card-dot" aria-hidden="true">·</span>'.join(
        meta_parts
    )

    unread_html = (
        '<span class="att-unread-dot" aria-label="Unread"></span>' if is_unread else ""
    )

    return (
        f'<a class="att-card" href="{_a(href)}"'
        f' data-ticket-id="{_a(ticket_id)}"'
        f' data-project="{_a(project_id)}"'
        f' data-bucket="{_a(bucket)}"'
        f' data-time-bucket="{_a(time_bucket)}"'
        f' data-section="{_a(section)}"'
        f' data-unread="{"1" if is_unread else "0"}">'
        f'<span class="att-card-body">'
        f'<span class="att-card-head">'
        f'<span class="att-card-id">{_t(ticket_id)}</span>'
        f'<span class="att-card-title">{_t(title)}</span>'
        f"</span>"
        f'<span class="att-card-meta">{meta_inner}</span>'
        f"</span>"
        f"{unread_html}"
        f"</a>"
    )


def _state_chip_row_html(totals: dict, active_state: str) -> str:
    """Run-state chip row (Needs me / Running / Ready / Paused / Failed)."""
    all_count = int(totals.get("all", 0))
    all_active = "active" if active_state == "all" else ""
    out = [
        f'<button class="att-chip {all_active}" type="button" data-state="all">'
        f'<span class="att-chip-label">All</span>'
        f'<span class="att-chip-count">{all_count}</span>'
        f"</button>"
    ]
    for key, label in _STATE_CHIPS:
        count = int(totals.get(key, 0))
        active = "active" if active_state == key else ""
        out.append(
            f'<button class="att-chip {active}" type="button" data-state="{_a(key)}">'
            f'<span class="att-chip-label">{_t(label)}</span>'
            f'<span class="att-chip-count">{count}</span>'
            f"</button>"
        )
    return "".join(out)


def _project_checkboxes_html(projects: list, totals: dict) -> str:
    """Project filter as a checkbox list inside the overflow panel.

    All projects checked by default — user unchecks to hide. We keep the
    "All / None" toggle button for quick mass-toggling.
    """
    rows = []
    for p in projects:
        pid = p.get("id", "")
        name = p.get("name", pid)
        count = int((p.get("counts") or {}).get("all", 0))
        rows.append(
            f'<label class="att-proj-row" data-project="{_a(pid)}">'
            f'  <input type="checkbox" class="att-proj-check"'
            f'         data-project="{_a(pid)}" checked>'
            f'  <span class="att-proj-name">{_t(name)}</span>'
            f'  <span class="att-proj-count">{count}</span>'
            f"</label>"
        )
    if not rows:
        rows.append('<div class="att-proj-empty">No projects registered.</div>')
    return (
        '<div class="att-proj-section">'
        '  <div class="att-proj-header">'
        '    <span class="att-proj-heading">Projects</span>'
        '    <button class="att-proj-toggle" type="button" data-action="all">All</button>'
        "  </div>"
        f'  <div class="att-proj-list">{"".join(rows)}</div>'
        "</div>"
    )


def _bucket_section_html(time_key: str, label: str, items: list, hidden: bool) -> str:
    """One time-bucket section. Hidden when the (server-filtered) list is empty
    for the default chip selection — JS toggles `att-hidden` on filter changes."""
    cards = "".join(_card_html(it) for it in items)
    cls = "att-bucket att-hidden" if hidden else "att-bucket"
    return (
        f'<section class="{cls}" data-time-bucket="{_a(time_key)}">'
        f'<h2 class="att-bucket-label">{_t(label)}</h2>'
        f'<div class="att-bucket-list">{cards}</div>'
        f"</section>"
    )


# ---------------------------------------------------------------------------
# CSS (inline — no external dependencies)
# ---------------------------------------------------------------------------

_CSS = r"""
:root, [data-theme="dark"] {
  --bg-page: #0c0c0e; --bg-surface: #151518; --bg-card: #1b1b20; --bg-hover: #232329;
  --border-subtle: #1f1f26; --border-default: #2c2c35; --border-strong: #3c3c47;
  --text-primary: #eaeaed; --text-secondary: #9e9eab; --text-tertiary: #6a6a76;
  --accent: #3b82f6;
  --status-wip: #3b82f6; --status-review: #f59e0b;
  --status-done: #22c55e; --status-idea: #8b5cf6;
  --att-tile-needs-bg: rgba(245,158,11,0.18);
  --att-tile-needs-fg: #f59e0b;
  --att-tile-running-bg: rgba(59,130,246,0.18);
  --att-tile-running-fg: #6b9eff;
  --att-tile-ready-bg: rgba(34,197,94,0.18);
  --att-tile-ready-fg: #22c55e;
  --att-tile-paused-bg: rgba(158,158,171,0.15);
  --att-tile-paused-fg: #9e9eab;
  --att-tile-failed-bg: rgba(239,68,68,0.18);
  --att-tile-failed-fg: #ef4444;
  --att-chip-bg: #1b1b20;
  --att-chip-border: #2c2c35;
  --att-card-bg: #17171c;
  --att-card-hover: #1f1f25;
}
[data-theme="light"] {
  --bg-page: #f6f7f9; --bg-surface: #ffffff; --bg-card: #ffffff; --bg-hover: #f3f4f6;
  --border-subtle: #e5e7eb; --border-default: #d1d5db; --border-strong: #9ca3af;
  --text-primary: #111827; --text-secondary: #6b7280; --text-tertiary: #9ca3af;
  --accent: #2563eb;
  --status-wip: #2563eb; --status-review: #d97706;
  --status-done: #059669; --status-idea: #7c3aed;
  --att-tile-needs-bg: rgba(217,119,6,0.12);
  --att-tile-needs-fg: #b45309;
  --att-tile-running-bg: rgba(37,99,235,0.10);
  --att-tile-running-fg: #2563eb;
  --att-tile-ready-bg: rgba(5,150,105,0.12);
  --att-tile-ready-fg: #059669;
  --att-tile-paused-bg: rgba(107,114,128,0.10);
  --att-tile-paused-fg: #6b7280;
  --att-tile-failed-bg: rgba(220,38,38,0.12);
  --att-tile-failed-fg: #dc2626;
  --att-chip-bg: #ffffff;
  --att-chip-border: #e5e7eb;
  --att-card-bg: #ffffff;
  --att-card-hover: #f3f4f6;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg-page);
  color: var(--text-primary);
  font: 14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

/* Outer layout — leaves room for the nav-rail and a centered feed column. */
.att-main {
  min-height: 100vh;
  padding-bottom: max(48px, env(safe-area-inset-bottom));
}
.att-shell {
  max-width: 720px;
  margin: 0 auto;
  padding: 0 16px 32px;
}

/* Sticky header */
.att-header {
  position: sticky;
  top: 0;
  z-index: 20;
  background: var(--bg-page);
  display: grid;
  grid-template-columns: 44px 1fr 44px;
  align-items: center;
  padding: 8px 8px;
  padding-top: max(8px, env(safe-area-inset-top));
  border-bottom: 1px solid var(--border-subtle);
}
.att-header-spacer {
  /* The nav-rail lives outside .att-main; this column reserves space for the
     hamburger icon that the rail renders in its top-left collapsed state. */
  width: 44px;
  height: 44px;
}
.att-header-title {
  text-align: center;
  font-size: 17px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--text-primary);
}
.att-header-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 6px;
}

/* Live / Paused indicator surfaced in the header — always visible so the
   system pause state isn't buried under the overflow dots. Tapping it
   opens the same overflow panel that holds the pause/resume button. */
.att-header-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 10px;
  border-radius: 999px;
  border: 1px solid var(--border-default);
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  line-height: 1;
  white-space: nowrap;
}
.att-header-status:hover { background: var(--bg-hover); color: var(--text-primary); }
.att-header-status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 0 0 rgba(34,197,94,0.45);
  animation: att-header-pulse 2.2s ease-in-out infinite;
}
.att-header-status.paused .att-header-status-dot {
  background: #f59e0b;
  animation: none;
}
@keyframes att-header-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(34,197,94,0.45); }
  50%      { box-shadow: 0 0 0 4px rgba(34,197,94,0); }
}

.att-overflow-btn {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-primary);
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
}
.att-overflow-btn:hover { background: var(--bg-hover); border-color: var(--border-subtle); }
.att-overflow-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* Overflow panel — sliding mini-sheet under the header */
.att-overflow-panel {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  margin: 8px 0 0;
  padding: 14px 14px 12px;
  box-shadow: 0 8px 28px rgba(0,0,0,0.28);
  display: flex;
  flex-direction: column;
  gap: 10px;
  transform-origin: top right;
  animation: att-pop 0.14s ease-out;
}
.att-overflow-panel.hidden { display: none; }
@keyframes att-pop {
  from { opacity: 0; transform: translateY(-6px) scale(0.98); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}
.att-pause-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.att-pause-dot {
  width: 9px; height: 9px; border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34,197,94,0.18);
}
.att-pause-status.paused .att-pause-dot {
  background: #f59e0b;
  box-shadow: 0 0 0 3px rgba(245,158,11,0.18);
}
.att-pause-note {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.45;
}
.att-pause-btn {
  appearance: none;
  border: 1px solid var(--border-default);
  background: var(--bg-card);
  color: var(--text-primary);
  padding: 10px 12px;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.att-pause-btn:hover { background: var(--bg-hover); border-color: var(--border-strong); }
.att-pause-btn:disabled { opacity: 0.55; cursor: progress; }

/* Project filter — multi-select checkbox list inside the overflow panel */
.att-proj-section {
  margin-top: 12px;
  border-top: 1px solid var(--border-subtle);
  padding-top: 12px;
}
.att-proj-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 2px 8px 2px;
}
.att-proj-heading {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--text-tertiary); font-weight: 600;
}
.att-proj-toggle {
  background: transparent; border: 1px solid var(--border-default);
  color: var(--text-secondary); border-radius: 6px; padding: 3px 10px;
  font-size: 11px; cursor: pointer; font-weight: 600;
}
.att-proj-toggle:hover { color: var(--text-primary); border-color: var(--border-strong); }
.att-proj-list { display: flex; flex-direction: column; gap: 2px; }
.att-proj-row {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 6px; border-radius: 6px; cursor: pointer;
  font-size: 14px; color: var(--text-primary);
  user-select: none;
}
.att-proj-row:hover { background: var(--bg-hover); }
.att-proj-check { width: 16px; height: 16px; accent-color: var(--accent); flex-shrink: 0; }
.att-proj-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.att-proj-count {
  font-size: 12px; color: var(--text-tertiary); font-variant-numeric: tabular-nums;
  background: var(--bg-card); padding: 1px 8px; border-radius: 10px;
  border: 1px solid var(--border-subtle);
}
.att-proj-empty { font-size: 12px; color: var(--text-tertiary); padding: 6px; }

/* Demo route banner — only injected by JS when location is /kitchen/demo */
.att-demo-banner {
  margin: 8px 12px 0 12px;
  padding: 10px 14px;
  background: rgba(59, 130, 246, 0.12);
  border: 1px solid rgba(59, 130, 246, 0.35);
  color: var(--text-secondary);
  font-size: 13px;
  border-radius: 8px;
  transition: background 0.25s, border-color 0.25s;
}
.att-demo-banner strong { color: var(--accent); font-weight: 700; }
.att-demo-banner a { color: var(--accent); }
.att-demo-banner-flash {
  background: rgba(59, 130, 246, 0.28);
  border-color: rgba(59, 130, 246, 0.7);
}

/* Inert cards on the demo route — no pointer cursor to set expectations */
body[data-demo="1"] .att-card { cursor: default; }

/* Overflow button gets a subtle accent dot when a project filter is active */
.att-overflow-btn-filtered { position: relative; }
.att-overflow-btn-filtered::after {
  content: ""; position: absolute; top: 6px; right: 6px;
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--accent);
}

/* Chip rows */
.att-chip-row {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  scrollbar-width: none;
  padding: 10px 0 6px;
  -webkit-overflow-scrolling: touch;
}
.att-chip-row::-webkit-scrollbar { display: none; }
.att-chip-row-states { padding-top: 14px; }
.att-chip-row-states { padding-bottom: 14px; border-bottom: 1px solid var(--border-subtle); margin-bottom: 8px; }

.att-chip {
  appearance: none;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border-radius: 999px;
  background: var(--att-chip-bg);
  border: 1px solid var(--att-chip-border);
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.12s, border-color 0.12s, color 0.12s;
}
.att-chip:hover { background: var(--bg-hover); color: var(--text-primary); }
.att-chip.active {
  background: var(--accent);
  border-color: var(--accent);
  color: white;
}
.att-chip.active .att-chip-count {
  background: rgba(255,255,255,0.22);
  color: white;
}
.att-chip-label { line-height: 1; }
.att-chip-count {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(255,255,255,0.04);
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
[data-theme="light"] .att-chip-count { background: rgba(17,24,39,0.05); }

/* Feed & bucket sections */
.att-feed { display: flex; flex-direction: column; gap: 18px; }
.att-bucket { display: flex; flex-direction: column; gap: 8px; }
.att-bucket.att-hidden { display: none; }
.att-bucket-label {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  text-transform: none;
  color: var(--text-tertiary);
  padding: 4px 4px;
  letter-spacing: -0.005em;
}
.att-bucket-list { display: flex; flex-direction: column; gap: 8px; }

/* Cards */
.att-card {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px 12px 16px;
  border-radius: 14px;
  background: var(--att-card-bg);
  border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--border-default);
  color: var(--text-primary);
  text-decoration: none;
  transition: background 0.12s, border-color 0.12s, transform 0.08s;
  min-height: 56px;
}
.att-card:hover { background: var(--att-card-hover); border-color: var(--border-default); }
.att-card:hover { border-left-color: var(--border-strong); }
.att-card:active { transform: scale(0.995); }
.att-card.att-hidden { display: none; }

/* Thin left-border tinted by the Kitchen bucket — the only color cue. */
.att-card[data-bucket="needs_me"]          { border-left-color: var(--att-tile-needs-fg); }
.att-card[data-bucket="running"]           { border-left-color: var(--att-tile-running-fg); }
.att-card[data-bucket="ready_to_delegate"] { border-left-color: var(--att-tile-ready-fg); }
.att-card[data-bucket="paused_ticket"]     { border-left-color: var(--att-tile-paused-fg); }
.att-card[data-bucket="failed"]            { border-left-color: var(--att-tile-failed-fg); }

/* Blue unread dot — top-right of the card */
.att-unread-dot {
  position: absolute;
  top: 10px;
  right: 12px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
}

.att-card-body {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  flex: 1;
}
.att-card-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.att-card-id {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  font-weight: 700;
  color: var(--accent);
  opacity: 0.8;
  letter-spacing: 0.02em;
  flex-shrink: 0;
}
.att-card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  letter-spacing: -0.005em;
  flex: 1;
  min-width: 0;
}
.att-card-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.att-card-proj {
  color: var(--text-secondary);
  font-weight: 500;
}
.att-card-dot { color: var(--text-tertiary); }

/* Section pill — neutral, just a small tag indicating the kanban column. */
.att-card-section {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.01em;
  background: transparent;
  color: var(--text-secondary);
  border: 1px solid var(--border-default);
  flex-shrink: 0;
}

/* Bucket label — plain text, color matches the card's left border so the
   user can read "Running" and connect it back to the bucket without a
   separate pictorial tile. Subtle, not a bright pill. */
.att-card-bucket { font-weight: 600; }
.att-card[data-bucket="needs_me"]          .att-card-bucket { color: var(--att-tile-needs-fg); }
.att-card[data-bucket="running"]           .att-card-bucket { color: var(--att-tile-running-fg); }
.att-card[data-bucket="ready_to_delegate"] .att-card-bucket { color: var(--att-tile-ready-fg); }
.att-card[data-bucket="paused_ticket"]     .att-card-bucket { color: var(--att-tile-paused-fg); }
.att-card[data-bucket="failed"]            .att-card-bucket { color: var(--att-tile-failed-fg); }

/* Empty state */
.att-empty {
  text-align: center;
  padding: 60px 20px 40px;
  color: var(--text-tertiary);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
.att-empty-icon {
  width: 56px;
  height: 56px;
  border-radius: 16px;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-tertiary);
  font-size: 24px;
  line-height: 1;
}
.att-empty-msg { font-size: 14px; font-weight: 500; }
.att-empty.att-hidden { display: none; }

/* Wider viewports — center the feed and add a touch more breathing room. */
@media (min-width: 760px) {
  .att-shell { padding: 0 24px 48px; }
  .att-card { padding: 14px 18px; min-height: 68px; }
  .att-header { padding: 10px 8px; }
  .att-header-title { font-size: 18px; }
}

/* Reduced motion — kill animations */
@media (prefers-reduced-motion: reduce) {
  .att-overflow-panel,
  .att-header-status-dot { animation: none !important; }
  .att-card { transition: none; }
}
"""


# ---------------------------------------------------------------------------
# Client JS (inline)
# ---------------------------------------------------------------------------

_JS = r"""
(function(){
  var STATE_PAUSED = window.__ATT_PAUSED__;
  // Project filter is now a multi-select Set of project IDs (those that are
  // CHECKED, i.e. visible). Lives under the overflow menu's Projects section.
  var ACTIVE_PROJECTS = null;  // null means "all" (no project filter applied)
  var ACTIVE_STATE    = window.__ATT_DEFAULT_STATE__;
  var POLL_MS = 5000;

  var menuOpen = false;
  var inFlight = false;
  var pollTimer = null;

  // ---- helpers ----------------------------------------------------------
  function $(sel, root){ return (root||document).querySelector(sel); }
  function $$(sel, root){ return Array.prototype.slice.call((root||document).querySelectorAll(sel)); }

  function setChipsActive(rowSel, attr, value){
    $$(rowSel + ' .att-chip').forEach(function(btn){
      btn.classList.toggle('active', btn.getAttribute(attr) === value);
    });
  }

  function applyFilters(){
    var cards = $$('.att-card');
    var visibleByBucket = {today:0, yesterday:0, this_week:0, older:0};
    var projFilter = ACTIVE_PROJECTS;  // null = no project filter
    cards.forEach(function(card){
      var matchProj = (projFilter === null) ||
                      projFilter[card.getAttribute('data-project')] === true;
      var matchState = (ACTIVE_STATE === 'all') ||
                       (card.getAttribute('data-bucket') === ACTIVE_STATE);
      var show = matchProj && matchState;
      card.classList.toggle('att-hidden', !show);
      if (show) {
        var tb = card.getAttribute('data-time-bucket') || 'older';
        visibleByBucket[tb] = (visibleByBucket[tb]||0) + 1;
      }
    });
    var totalVisible = 0;
    $$('.att-bucket').forEach(function(sec){
      var tb = sec.getAttribute('data-time-bucket');
      var n = visibleByBucket[tb] || 0;
      sec.classList.toggle('att-hidden', n === 0);
      totalVisible += n;
    });
    var empty = $('.att-empty');
    if (empty) empty.classList.toggle('att-hidden', totalVisible > 0);
  }

  // Walks the project checkboxes and rebuilds ACTIVE_PROJECTS. Returns null
  // (= all) when every box is checked, so filtering can short-circuit.
  function rebuildProjectFilter(){
    var boxes = $$('.att-proj-check');
    if (!boxes.length) { ACTIVE_PROJECTS = null; return; }
    var allChecked = true;
    var picked = {};
    boxes.forEach(function(b){
      if (b.checked) picked[b.getAttribute('data-project')] = true;
      else allChecked = false;
    });
    ACTIVE_PROJECTS = allChecked ? null : picked;
    // Reflect the filter state on the overflow button so the user can tell
    // they have a filter applied without opening the menu.
    var ofb = $('.att-overflow-btn');
    if (ofb) ofb.classList.toggle('att-overflow-btn-filtered', !allChecked);
  }

  // ---- filter chip wiring ----------------------------------------------
  function wireChips(){
    $$('.att-proj-check').forEach(function(box){
      box.addEventListener('change', function(){
        rebuildProjectFilter();
        applyFilters();
      });
    });
    var allBtn = $('.att-proj-toggle');
    if (allBtn) {
      allBtn.addEventListener('click', function(e){
        e.stopPropagation();
        var boxes = $$('.att-proj-check');
        var anyUnchecked = boxes.some(function(b){ return !b.checked; });
        // If at least one is unchecked → check all. Else → uncheck all.
        var nextState = anyUnchecked;
        boxes.forEach(function(b){ b.checked = nextState; });
        rebuildProjectFilter();
        applyFilters();
      });
    }
    $$('.att-chip-row-states .att-chip').forEach(function(btn){
      btn.addEventListener('click', function(){
        ACTIVE_STATE = btn.getAttribute('data-state');
        setChipsActive('.att-chip-row-states', 'data-state', ACTIVE_STATE);
        applyFilters();
      });
    });
  }

  // ---- overflow menu ----------------------------------------------------
  function wireOverflow(){
    var btn = $('.att-overflow-btn');
    var panel = $('.att-overflow-panel');
    var headerStatus = $('.att-header-status');
    if (!btn || !panel) return;
    function toggle(){
      menuOpen = !menuOpen;
      panel.classList.toggle('hidden', !menuOpen);
      btn.setAttribute('aria-expanded', menuOpen ? 'true' : 'false');
    }
    btn.addEventListener('click', function(e){ e.stopPropagation(); toggle(); });
    // The header Live/Paused indicator opens the same panel so users can
    // toggle the system pause directly from where they see the state.
    if (headerStatus) {
      headerStatus.addEventListener('click', function(e){
        e.stopPropagation();
        toggle();
      });
    }
    document.addEventListener('click', function(e){
      if (!menuOpen) return;
      if (panel.contains(e.target) || btn.contains(e.target)) return;
      if (headerStatus && headerStatus.contains(e.target)) return;
      menuOpen = false;
      panel.classList.add('hidden');
      btn.setAttribute('aria-expanded', 'false');
    });
    document.addEventListener('keydown', function(e){
      if (e.key === 'Escape' && menuOpen) {
        menuOpen = false;
        panel.classList.add('hidden');
        btn.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // ---- pause / resume ---------------------------------------------------
  function wirePauseBtn(){
    var btn = $('.att-pause-btn');
    if (!btn) return;
    btn.addEventListener('click', function(){
      if (inFlight) return;
      inFlight = true;
      btn.disabled = true;
      var url = STATE_PAUSED ? '/api/kitchen/resume' : '/api/kitchen/pause';
      fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'} })
        .then(function(r){ return r.ok ? r.json().catch(function(){return {};}) : Promise.reject(r); })
        .then(function(){ return refreshFromServer(); })
        .catch(function(err){ console.warn('[att] pause/resume failed', err); })
        .then(function(){ inFlight = false; btn.disabled = false; });
    });
  }

  // ---- polling / refresh ------------------------------------------------
  function setChipCount(rowSel, attr, value, count){
    var chip = document.querySelector(rowSel + ' .att-chip[' + attr + '="' + value + '"]');
    if (!chip) return;
    var num = chip.querySelector('.att-chip-count');
    if (num) num.textContent = String(count);
  }

  function rebuildFeed(items){
    var feed = $('.att-feed');
    if (!feed) return;
    // Group items by time_bucket while preserving newest-first ordering.
    var groups = {today: [], yesterday: [], this_week: [], older: []};
    items.forEach(function(it){
      var tb = it.time_bucket || 'older';
      if (!groups[tb]) groups[tb] = [];
      groups[tb].push(it);
    });
    var ORDER = [
      ['today',     'Today'],
      ['yesterday', 'Yesterday'],
      ['this_week', 'This week'],
      ['older',     'Older']
    ];
    var html = '';
    ORDER.forEach(function(pair){
      var key = pair[0], label = pair[1];
      var rows = groups[key] || [];
      var cardsHTML = rows.map(renderCard).join('');
      html += '<section class="att-bucket" data-time-bucket="'+key+'">'
            + '<h2 class="att-bucket-label">'+label+'</h2>'
            + '<div class="att-bucket-list">'+cardsHTML+'</div>'
            + '</section>';
    });
    feed.innerHTML = html;
    // Empty state is a sibling — toggle below.
    var empty = $('.att-empty');
    if (empty && !feed.parentNode.contains(empty)) {
      feed.parentNode.appendChild(empty);
    }
    applyFilters();
  }

  function esc(s){
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  var BUCKET_LABELS = {
    needs_me:'Needs me', running:'Running', ready_to_delegate:'Ready',
    paused_ticket:'Paused', failed:'Failed'
  };
  function renderCard(it){
    var href = '/' + encodeURIComponent(it.project_id || '') +
               '/#ticket/' + encodeURIComponent(it.ticket_id || '');
    var bucket = it.bucket || '';
    var section = it.section || '';
    var bucketLbl = BUCKET_LABELS[bucket] || '';

    var dot = '<span class="att-card-dot" aria-hidden="true">·</span>';
    var parts = [];
    if (section)   parts.push('<span class="att-card-section">'+esc(section)+'</span>');
    if (bucketLbl) parts.push('<span class="att-card-bucket">'+esc(bucketLbl)+'</span>');
    var proj = it.project_name || it.project_id || '';
    if (proj)      parts.push('<span class="att-card-proj">'+esc(proj)+'</span>');
    var metaInner = parts.join(dot);
    var unread = it.is_unread
      ? '<span class="att-unread-dot" aria-label="Unread"></span>' : '';

    return '<a class="att-card" href="'+href+'"'
         + ' data-ticket-id="'+esc(it.ticket_id||'')+'"'
         + ' data-project="'+esc(it.project_id||'')+'"'
         + ' data-bucket="'+esc(bucket)+'"'
         + ' data-time-bucket="'+esc(it.time_bucket||'older')+'"'
         + ' data-section="'+esc(section)+'"'
         + ' data-unread="'+(it.is_unread?'1':'0')+'">'
         + '<span class="att-card-body">'
         + '<span class="att-card-head">'
         + '<span class="att-card-id">'+esc(it.ticket_id||'')+'</span>'
         + '<span class="att-card-title">'+esc(it.title||'')+'</span>'
         + '</span>'
         + '<span class="att-card-meta">'+metaInner+'</span>'
         + '</span>'
         + unread
         + '</a>';
  }

  function refreshFromServer(){
    return fetch('/api/kitchen/feed', { headers: {'Accept': 'application/json'} })
      .then(function(r){ return r.ok ? r.json() : Promise.reject(r); })
      .then(function(state){
        // Update totals + chip counts
        var totals = state.totals || {};
        setChipCount('.att-chip-row-states', 'data-state', 'all', totals.all || 0);
        ['needs_me','running','ready_to_delegate','paused_ticket','failed'].forEach(function(k){
          setChipCount('.att-chip-row-states', 'data-state', k, totals[k] || 0);
        });
        var projects = state.projects || [];
        // Refresh the per-project counts inside the overflow menu's checkbox list.
        projects.forEach(function(p){
          var row = $('.att-proj-row[data-project="' + (p.id || '').replace(/"/g, '\\"') + '"]');
          if (!row) return;
          var c = (p.counts || {}).all || 0;
          var cnt = row.querySelector('.att-proj-count');
          if (cnt) cnt.textContent = c;
        });
        // Paused state
        var wasPaused = STATE_PAUSED;
        STATE_PAUSED = !!state.paused;
        if (STATE_PAUSED !== wasPaused) {
          var status = $('.att-pause-status');
          var label = $('.att-pause-label');
          var btn = $('.att-pause-btn');
          var note = $('.att-pause-note');
          var hdr = $('.att-header-status');
          var hdrLbl = $('.att-header-status-label');
          if (status) status.classList.toggle('paused', STATE_PAUSED);
          if (hdr) hdr.classList.toggle('paused', STATE_PAUSED);
          if (label) label.textContent = STATE_PAUSED ? 'Paused' : 'Live';
          if (hdrLbl) hdrLbl.textContent = STATE_PAUSED ? 'Paused' : 'Live';
          if (btn) btn.textContent = STATE_PAUSED ? 'Resume auto-dispatch' : 'Pause auto-dispatch';
          if (note) {
            if (STATE_PAUSED) {
              var rdy = totals.ready_to_delegate || 0;
              note.textContent = rdy > 0
                ? 'Showing ' + rdy + ' eligible ticket' + (rdy===1?'':'s') + ' that would run if you resume.'
                : 'Auto-dispatch is paused. Nothing currently eligible to run.';
            } else {
              note.textContent = 'Auto-dispatch is live. Eligible tickets dispatch automatically.';
            }
          }
        }
        // Rebuild the feed in place (preserve scroll)
        var y = window.scrollY;
        rebuildFeed(state.items || []);
        window.scrollTo(0, y);
      })
      .catch(function(err){ /* swallow — next poll will try again */ });
  }

  // The /kitchen/demo route renders mockup state; the live /api/kitchen/feed
  // endpoint would replace it with the empty live DB on the first tick.
  var IS_DEMO = location.pathname === '/kitchen/demo';

  // Demo cards point at IDs that don't exist in the live DB. Clicking would
  // open the detail overlay for a ghost ticket. Suppress navigation and
  // explain why with a top banner.
  if (IS_DEMO) {
    document.documentElement.setAttribute('data-demo', '1');
    document.addEventListener('click', function(e){
      var card = e.target.closest('.att-card');
      if (!card) return;
      e.preventDefault();
      var banner = document.querySelector('.att-demo-banner');
      if (banner) {
        banner.classList.add('att-demo-banner-flash');
        setTimeout(function(){ banner.classList.remove('att-demo-banner-flash'); }, 600);
      }
    });
    document.addEventListener('DOMContentLoaded', function(){
      document.body.setAttribute('data-demo', '1');
      var main = document.querySelector('.att-main .att-shell');
      if (!main) return;
      var b = document.createElement('div');
      b.className = 'att-demo-banner';
      b.innerHTML = '<strong>Demo data</strong> &mdash; cards are inert. '
                  + 'Visit <a href="/kitchen">/kitchen</a> for the live feed.';
      main.insertBefore(b, main.firstChild);
    });
  }

  function tick(){
    if (IS_DEMO || menuOpen || inFlight) return;
    refreshFromServer();
  }

  function startPolling(){
    if (IS_DEMO) return;
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(tick, POLL_MS);
  }
  document.addEventListener('visibilitychange', function(){
    if (document.hidden) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    } else {
      tick();
      startPolling();
    }
  });

  // ---- boot -------------------------------------------------------------
  function init(){
    wireChips();
    wireOverflow();
    wirePauseBtn();
    applyFilters();
    startPolling();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_attention_feed(
    state: dict,
    port: int,
    rail_js: str,
    pwa_head_tags: str,
    rail_css: str = "",
    rail_html: str = "",
) -> str:
    """Return a complete <!DOCTYPE html> ... </html> string for the kitchen
    attention feed.

    See the module docstring and the integrator's input contract for the
    `state` dict shape.
    """
    paused = bool(state.get("paused"))
    totals = state.get("totals") or {}
    projects = state.get("projects") or []
    items = state.get("items") or []

    # Default state-chip selection. Project filter is multi-select (checkboxes)
    # and defaults to all-checked, so there is no "default_project" anymore.
    default_state = "ready_to_delegate" if paused else "all"

    # ---- header / overflow -------------------------------------------------
    if paused:
        pause_status_cls = "att-pause-status paused"
        pause_dot_label = "Paused"
        pause_btn_label = "Resume auto-dispatch"
        ready_n = int(totals.get("ready_to_delegate", 0))
        if ready_n > 0:
            pause_note = (
                f"Showing {ready_n} eligible ticket"
                f"{'s' if ready_n != 1 else ''} that would run if you resume."
            )
        else:
            pause_note = "Auto-dispatch is paused. Nothing currently eligible to run."
    else:
        pause_status_cls = "att-pause-status"
        pause_dot_label = "Live"
        pause_btn_label = "Pause auto-dispatch"
        pause_note = "Auto-dispatch is live. Eligible tickets dispatch automatically."

    overflow_panel = (
        f'<div class="att-overflow-panel hidden" role="menu" aria-label="Kitchen options">'
        f'  <div class="{pause_status_cls}">'
        f'    <span class="att-pause-dot" aria-hidden="true"></span>'
        f'    <span class="att-pause-label">{_t(pause_dot_label)}</span>'
        f"  </div>"
        f'  <div class="att-pause-note">{_t(pause_note)}</div>'
        f'  <button class="att-pause-btn" type="button">{_t(pause_btn_label)}</button>'
        f"  {_project_checkboxes_html(projects, totals)}"
        f"</div>"
    )

    # Live/Paused state indicator surfaced in the header so users see it
    # at a glance — clicking opens the same overflow panel that holds the
    # pause/resume button.
    header_status_cls = "att-header-status paused" if paused else "att-header-status"
    header_status = (
        f'<button class="{header_status_cls}" type="button" '
        f'aria-label="Toggle auto-dispatch panel" '
        f'data-att-toggle-overflow="1">'
        f'<span class="att-header-status-dot" aria-hidden="true"></span>'
        f'<span class="att-header-status-label">{_t(pause_dot_label)}</span>'
        f"</button>"
    )

    header_html = (
        f'<header class="att-header" role="banner">'
        f'  <div class="att-header-spacer" aria-hidden="true"></div>'
        f'  <div class="att-header-title">Kitchen</div>'
        f'  <div class="att-header-actions">'
        f"    {header_status}"
        f'    <button class="att-overflow-btn" type="button" aria-label="More options"'
        f'            aria-haspopup="menu" aria-expanded="false">&middot;&middot;&middot;</button>'
        f"  </div>"
        f"</header>"
    )

    # ---- chip rows ---------------------------------------------------------
    state_chips = _state_chip_row_html(totals, default_state)

    # ---- feed --------------------------------------------------------------
    # Group items by time_bucket while preserving the input order
    # (already sorted newest-first by updated_at).
    groups: dict[str, list] = {key: [] for key, _label in _TIME_BUCKETS}
    for it in items:
        tb = it.get("time_bucket") or "older"
        groups.setdefault(tb, []).append(it)

    # For initial paint: a section is hidden when 0 items match the *default*
    # chip selection. We compute that filter here so first paint is correct.
    def _matches_default(it: dict) -> bool:
        # Project filter starts as all-checked, so it never excludes here.
        if default_state != "all" and it.get("bucket") != default_state:
            return False
        return True

    sections_html_parts: list[str] = []
    total_visible_default = 0
    for key, label in _TIME_BUCKETS:
        bucket_items = groups.get(key, [])
        visible_count = sum(1 for it in bucket_items if _matches_default(it))
        total_visible_default += visible_count
        # We still render ALL items in the DOM so client-side filters can
        # show them when chips change — but the section starts hidden if
        # no items match the default filter.
        hidden = visible_count == 0
        sections_html_parts.append(
            _bucket_section_html(key, label, bucket_items, hidden)
        )
    feed_inner = "".join(sections_html_parts)

    empty_hidden = total_visible_default > 0
    empty_cls = "att-empty att-hidden" if empty_hidden else "att-empty"
    empty_html = (
        f'<div class="{empty_cls}" role="status">'
        f'  <div class="att-empty-icon" aria-hidden="true">&#9788;</div>'
        f'  <div class="att-empty-msg">Nothing here.</div>'
        f"</div>"
    )

    # ---- boot script payload ----------------------------------------------
    boot_payload = (
        f"<script>"
        f"window.__ATT_PAUSED__={'true' if paused else 'false'};"
        f"window.__ATT_DEFAULT_STATE__={json.dumps(default_state)};"
        f"window.__ATT_PORT__={json.dumps(int(port))};"
        f"</script>"
    )

    # ---- HTML --------------------------------------------------------------
    theme_iife = (
        "<script>(function(){"
        "var t=localStorage.getItem('tt-theme')||'system';"
        "var dark=t==='dark'||(t==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);"
        "document.documentElement.setAttribute('data-theme',dark?'dark':'light');"
        "})();</script>"
    )

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Kitchen — Ticket Takeaway</title>
{pwa_head_tags}
{theme_iife}
<style>{rail_css}
{_CSS}</style>
</head>
<body>
{rail_html or '<div id="navRail" class="nav-rail"></div>'}
<main class="att-main" role="main">
  <div class="att-shell">
    {header_html}
    {overflow_panel}
    <div class="att-chip-row att-chip-row-states" role="tablist" aria-label="Run states">
      {state_chips}
    </div>
    <div class="att-feed">
      {feed_inner}
    </div>
    {empty_html}
  </div>
</main>
{boot_payload}
<script>{rail_js}</script>
<script>{_JS}</script>
</body>
</html>"""
