# UI Consistency Pass — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Visual rationalization of the Ticket Takeaway dashboard — light/dark theming, unified toast system, inline SVG icons, dialog replacement, bottom lane cohesion, and component normalization.

**Architecture:** All changes are CSS/JS/HTML within the existing single-file renderer (`src/generate.py` ~5100 lines) plus two supporting files (`src/serve.py`, `src/constants.py`). No new files, no new dependencies, no DB changes. The work is organized in 3 phases: Phase 1 runs 3 tasks in parallel (non-overlapping files), Phase 2 runs 7 sequential commits on generate.py, Phase 3 verifies.

**Tech Stack:** Python 3.10+, vanilla CSS/JS, inline SVG, SQLite (unchanged)

**Spec:** `docs/superpowers/specs/2026-04-05-ui-consistency-pass-design.md`

**Branch:** `ui-consistency-pass`

---

## Phase 1 — Parallel Tasks (no file overlap)

These 3 tasks touch different files and can run simultaneously in separate worktrees.

---

### Task 1A: Fix Feedbacks URL Constant

**Files:**
- Modify: `src/constants.py:129`
- Modify: `src/generate.py:22-23` (imports), `src/generate.py:1651` (hardcoded URL)

- [ ] **Step 1: Fix the constant value**

In `src/constants.py:129`, change:
```python
FEEDBACKS_REPO_URL = "https://github.com/user/feedbacks"
```
to:
```python
FEEDBACKS_REPO_URL = "https://github.com/ytubecoder/feedbacks"
```

- [ ] **Step 2: Import the constant in generate.py**

In `src/generate.py:22-23`, add `FEEDBACKS_REPO_URL` to the existing import:
```python
from constants import (SECTION_ORDER, SECTION_SLUGS, SLUG_TO_SECTION,
                       DEFAULT_STATUS_BY_SECTION, CARD_CLASS_BY_SLUG, STATUSES,
                       FEEDBACKS_REPO_URL)
```

- [ ] **Step 3: Replace hardcoded URL in settings drawer HTML**

In `src/generate.py:1651`, change:
```python
        <a class="settings-link" href="https://github.com/ytubecoder/feedbacks" target="_blank" rel="noopener">GitHub</a>
```
to:
```python
        <a class="settings-link" href="{FEEDBACKS_REPO_URL}" target="_blank" rel="noopener">GitHub</a>
```

- [ ] **Step 4: Verify no other hardcoded feedbacks URLs exist**

Run: `grep -rn "github.com.*feedbacks" src/`
Expected: Only `constants.py:129` should contain the URL.

- [ ] **Step 5: Run TDD tests to confirm no breakage**

Run: `python3 -m pytest tests/test_tdd_*.py -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add src/constants.py src/generate.py
git commit -m "fix: feedbacks URL — single source of truth in constants.py"
```

---

### Task 1B: Theme + Modal on serve.py Pages

**Files:**
- Modify: `src/serve.py:1160-1274` (settings page), `src/serve.py:1277-1425` (project picker)

This task adds light/dark/system theming and replaces native dialogs on the two serve.py-rendered pages. These pages are self-contained HTML — they do NOT share CSS with generate.py.

- [ ] **Step 1: Add theme CSS variables + head script to settings page**

In `src/serve.py`, find `_render_project_settings()` (line 1160). Replace the `<style>` section's `:root` block and add a `<script>` in `<head>` before the style tag.

Change `<html lang="en">` to `<html lang="en" data-theme="dark">`.

Add synchronous theme script in `<head>` before `<style>`:
```html
<script>
(function(){
  var s=localStorage.getItem('tt-theme');
  if(s==='light')document.documentElement.setAttribute('data-theme','light');
  else if(s==='dark')document.documentElement.setAttribute('data-theme','dark');
  else document.documentElement.setAttribute('data-theme',
    window.matchMedia('(prefers-color-scheme:light)').matches?'light':'dark');
})();
</script>
```

Replace `:root` with dual-theme variables:
```css
:root, [data-theme="dark"] {
  --bg-page: #0c0c0e; --bg-surface: #151518; --bg-card: #1b1b20; --bg-hover: #232329;
  --border-subtle: #1f1f26; --border-default: #2c2c35; --border-strong: #3c3c47;
  --text-primary: #eaeaed; --text-secondary: #9e9eab; --text-tertiary: #6a6a76;
  --accent: #3b82f6;
}
[data-theme="light"] {
  --bg-page: #f8f9fa; --bg-surface: #ffffff; --bg-card: #ffffff; --bg-hover: #f3f4f6;
  --border-subtle: #e5e7eb; --border-default: #d1d5db; --border-strong: #9ca3af;
  --text-primary: #111827; --text-secondary: #6b7280; --text-tertiary: #9ca3af;
  --accent: #2563eb;
}
```

Keep all existing component CSS rules after the variables unchanged.

- [ ] **Step 2: Replace confirm() with custom modal on settings page**

In `src/serve.py`, find the `remove-btn` click handler (lines 1262-1269). Replace the `confirm()` and `alert()` with a custom modal.

Add modal HTML before `</body>`:

```html
<div id="confirm-modal" style="display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px);align-items:center;justify-content:center;">
  <div style="background:var(--bg-card);border:1px solid var(--border-default);border-radius:12px;padding:24px;max-width:400px;width:90vw;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
    <h3 style="font-size:14px;font-weight:600;margin-bottom:8px;">Remove Project</h3>
    <p style="font-size:13px;color:var(--text-secondary);margin-bottom:20px;">Remove this project from the registry? Tickets and files will not be deleted.</p>
    <div style="display:flex;gap:8px;justify-content:flex-end;">
      <button id="modal-cancel" style="font-size:12px;padding:6px 16px;border-radius:6px;border:1px solid var(--border-default);background:none;color:var(--text-secondary);cursor:pointer;font-family:inherit;">Cancel</button>
      <button id="modal-confirm" style="font-size:12px;padding:6px 16px;border-radius:6px;border:none;background:rgba(239,68,68,0.15);color:#ef4444;cursor:pointer;font-weight:600;font-family:inherit;">Remove</button>
    </div>
  </div>
</div>
```

Replace the JS click handler:
```javascript
  var modal = document.getElementById('confirm-modal');
  var modalCancel = document.getElementById('modal-cancel');
  var modalConfirm = document.getElementById('modal-confirm');
  document.getElementById('remove-btn').addEventListener('click', function() {
    modal.style.display = 'flex';
  });
  modalCancel.addEventListener('click', function() { modal.style.display = 'none'; });
  modal.addEventListener('click', function(e) { if (e.target === modal) modal.style.display = 'none'; });
  modalConfirm.addEventListener('click', function() {
    modal.style.display = 'none';
    fetch('/api/projects/{pid}', { method: 'DELETE' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.ok) window.location.href = '/';
      else { msg.textContent = data.error || 'Failed to remove'; msg.className = 'msg err'; msg.style.display = 'block'; }
    });
  });
```

- [ ] **Step 3: Add theme CSS + head script to project picker page**

In `src/serve.py`, find `_render_project_picker()` (line 1277). Apply the same pattern:

1. Change `<html lang="en">` to `<html lang="en" data-theme="dark">`
2. Add the same `<script>` theme init block in `<head>` before `<style>`
3. Replace `:root` variables with the same dark/light variable blocks

- [ ] **Step 4: Verify settings and picker pages render**

Start server and manually verify both pages render in dark and respond to localStorage theme setting.

- [ ] **Step 5: Commit**

```bash
git add src/serve.py
git commit -m "feat: light/dark theming + custom modal on serve.py pages"
```

---

### Task 1C: Remove "Coming Soon" Placeholder

**Files:**
- Modify: `src/generate.py:1621-1624` (HTML), `src/generate.py:2800-2837` (JS), CSS block

- [ ] **Step 1: Remove the HTML elements**

In `src/generate.py`, remove lines 1621-1624:
```html
    <button class="new-ticket-expand-btn" id="newTicketExpandBtn"><span class="arrow">&#9654;</span> Full ticket form</button>
    <div class="new-ticket-full" id="newTicketFull" style="display:none">
      <div class="coming-soon">Coming soon</div>
    </div>
```

- [ ] **Step 2: Remove JS references**

Remove `var expandBtn` and `var fullPanel` declarations (~line 2800-2801) and the `if (expandBtn)` block (~lines 2831-2837).

- [ ] **Step 3: Remove associated CSS**

Search for `.new-ticket-expand-btn` and `.coming-soon` CSS rules and remove them.

- [ ] **Step 4: Run smoke tests**

Run: `python3 -m pytest tests/test_smoke_ui.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add src/generate.py
git commit -m "fix: remove Coming Soon placeholder from new ticket panel"
```

---

## Phase 2 — Sequential on generate.py

After Phase 1 merges, these 7 commits run sequentially. Each builds on the previous.

---

### Task 2A: Theme Infrastructure (CSS Variables + Head Script + Toggle)

**Files:**
- Modify: `src/generate.py:593-616` (head + CSS variables), `src/generate.py:1628-1656` (settings drawer HTML), settings panel JS

- [ ] **Step 1: Add synchronous theme init script in `<head>`**

In `src/generate.py`, find the `<title>` tag (line 601). Add immediately after, before `<style>`:

```html
<script>
(function(){
  var s=localStorage.getItem('tt-theme');
  if(s==='light')document.documentElement.setAttribute('data-theme','light');
  else if(s==='dark')document.documentElement.setAttribute('data-theme','dark');
  else document.documentElement.setAttribute('data-theme',
    window.matchMedia('(prefers-color-scheme:light)').matches?'light':'dark');
})();
</script>
```

Change the `<html>` tag to `<html lang="en" data-theme="dark">`.

- [ ] **Step 2: Add light theme CSS variables**

Update `:root` to ``:root, [data-theme="dark"]` and add `[data-theme="light"]` block after it. Use the blended palette from the spec (see Task 1B Step 1 for the exact values — identical variable names, light-appropriate values).

- [ ] **Step 3: Add theme toggle to settings drawer**

Add a new "Appearance" settings section before the Feedbacks section in the drawer HTML (~line 1634):

```html
    <div class="settings-section">
      <div class="settings-section-title">Appearance</div>
      <div class="settings-row">
        <label>Theme</label>
        <div class="theme-toggle" id="themeToggle">
          <button class="theme-opt" data-theme="light" title="Light" aria-label="Light theme">&#9788;</button>
          <button class="theme-opt" data-theme="system" title="System" aria-label="System theme">&#9684;</button>
          <button class="theme-opt active" data-theme="dark" title="Dark" aria-label="Dark theme">&#9790;</button>
        </div>
      </div>
    </div>
```

- [ ] **Step 4: Add theme toggle CSS**

```css
.theme-toggle { display: inline-flex; gap: 2px; background: var(--bg-page); border: 1px solid var(--border-default); border-radius: 6px; padding: 2px; }
.theme-opt { font-size: 14px; padding: 3px 10px; border: none; border-radius: 4px; background: none; color: var(--text-tertiary); cursor: pointer; transition: all 0.15s; font-family: var(--font-sans); }
.theme-opt:hover { color: var(--text-secondary); }
.theme-opt.active { background: var(--bg-hover); color: var(--text-primary); }
```

- [ ] **Step 5: Add theme toggle JS in settings panel script**

In the settings panel JS (~line 4130), add theme toggle handler that reads `localStorage('tt-theme')`, sets active button, and on click updates localStorage + `data-theme` attribute (with system = media query resolution).

- [ ] **Step 6: Test theme switching manually**

Verify dark, light, and system modes. Verify no flash on reload.

- [ ] **Step 7: Commit**

```bash
git add src/generate.py
git commit -m "feat: light/dark/system theming with flash prevention"
```

---

### Task 2B: SVG Icon Sprite

**Files:**
- Modify: `src/generate.py` — add icon dict + helper (~line 30), update `_render_readiness_row()` (~line 4764), update `_render_action_buttons()` (~line 4784), update overlay header (~line 2867), update misc Unicode symbols

- [ ] **Step 1: Add SVG icon dictionary and helper function at module level**

After the constants section (~line 30), add `SVG_ICONS` dict (Lucide-style, 24x24 viewBox, stroke-based paths) and `_svg_icon(name, size, cls)` helper that returns an inline `<svg>` with `currentColor` stroke, `fill="none"`, `stroke-width="2"`, `stroke-linecap="round"`, `stroke-linejoin="round"`.

Icons needed: `file-text`, `check-square`, `flame`, `flask-conical`, `eye`, `x`, `arrow-up-right`, `settings`, `chevron-down`, `plus`, `trash-2`, `undo-2`, `grip-vertical`, `search`, `sun`, `moon`, `arrow-right`, `play`, `check`, `snowflake`, `arrow-left`, `monitor`.

Use inline SVG per-instance (not `<symbol>`/`<use>` sprite) — avoids cross-origin issues in file:// mode.

- [ ] **Step 2: Update `_render_readiness_row()` to use SVG**

Replace emoji HTML entities with `_svg_icon()` calls. Map: D=file-text, C=check-square, S=flame, T=flask-conical, L=eye. Add `aria-label` to each dot.

- [ ] **Step 3: Update overlay header readiness buttons**

Replace the hardcoded letter buttons (lines 2867-2873) with SVG icon buttons. Build the HTML in Python using `_svg_icon()` before the f-string template, then interpolate.

- [ ] **Step 4: Update action buttons**

In `_render_action_buttons()`, replace Unicode arrows/symbols with SVG icons: arrow-right for "Backlog", play for "Start", check for "Done", snowflake for "Icebox", check for "Accept", arrow-left for "Back to WIP".

- [ ] **Step 5: Replace other Unicode symbols**

- Settings gear `&#9881;` / `⚙` → `_svg_icon("settings", 14)`
- Close buttons `&times;` → `_svg_icon("x", 14)`
- Card open button `&#8599;` → `_svg_icon("arrow-up-right", 12)`

- [ ] **Step 6: Add CSS for SVG icon sizing**

```css
.readiness-dot svg { width: 12px; height: 12px; flex-shrink: 0; }
.action-btn svg { width: 12px; height: 12px; vertical-align: -2px; margin-right: 2px; }
.settings-toggle svg, .detail-close svg, .settings-drawer-close svg { width: 14px; height: 14px; }
.card-open-btn svg { width: 12px; height: 12px; }
```

- [ ] **Step 7: Run tests**

Run: `python3 -m pytest tests/test_smoke_ui.py -v`
Expected: All pass (structural selectors `.readiness-dot[data-flag]`, `.action-btn`, `.card-open-btn` preserved).

- [ ] **Step 8: Commit**

```bash
git add src/generate.py
git commit -m "feat: inline SVG icons replacing Unicode emoji — consistent cross-platform"
```

---

### Task 2C: Unified Toast System

**Files:**
- Modify: `src/generate.py` — CSS (replace 3 toast styles), HTML (add `#app-toast`), JS (replace 3 toast functions + all alert() calls)

- [ ] **Step 1: Replace toast CSS**

Remove the 3 separate toast CSS rules (`#undo-toast`, `.copied-toast`, `.detail-toast`). Add unified `#app-toast` styles: fixed bottom-center, left-border color by type (green=success, red=error, neutral=undo), slide-up animation, pointer-events on visible. Add `.toast-undo-btn` style for clickable undo link.

- [ ] **Step 2: Add toast HTML element**

Add `<div id="app-toast" role="status" aria-live="polite"><span id="app-toast-msg"></span></div>` in the HTML body. Remove the dynamic undo-toast `createElement` in JS (~line 2225-2228). Remove `.detail-toast` element from overlay header (~line 2874). Remove `.copied-toast` elements from `_render_single_card()` and `_render_list_rows()`.

- [ ] **Step 3: Implement unified `showAppToast()` JS function**

Replace `showToast()` and `showUndoToast()` with `showAppToast(message, type, duration, undoFn)`:
- Priority tiers: error/undo = 2, success/info/copy = 1. Higher priority cannot be displaced by lower.
- Type determines CSS class: `toast-error`, `toast-undo`, or default (success).
- Undo toasts: build undo button using DOM methods (createElement, not string concatenation — to avoid XSS). Set `onclick` to call the provided `undoFn`.
- Duration: 2500ms success, 4000ms error, 5000ms undo.
- Expose as `window.showAppToast` for cross-IIFE access.

Keep backwards-compatible wrappers:
```javascript
function showToast(el, text) { showAppToast(text || 'Saved!', 'success'); }
function showUndoToast(text) { showAppToast(text, 'success'); }
```

- [ ] **Step 4: Update `pushUndo()` to use undo toast**

Change `pushUndo()` to call `showAppToast(description + ' (Ctrl+Z to undo)', 'undo', 5000, function() { performUndo(); })`.

- [ ] **Step 5: Replace detail overlay `toast()` function**

Change to: `function toast(msg) { showAppToast(msg, 'success'); }`

- [ ] **Step 6: Replace all `alert()` calls with `showAppToast(msg, 'error')`**

All 7 instances in generate.py (lines 4079, 4094, 4389, 4460, 4463, 4482, 4485).

- [ ] **Step 7: Run tests**

Run: `python3 -m pytest tests/ -v`
Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add src/generate.py
git commit -m "feat: unified toast system with priority tiers — replaces 3 implementations + all alert() calls"
```

---

### Task 2D: Dialog Replacement (Inline Confirm + Undo)

**Files:**
- Modify: `src/generate.py` — JS in scripts 4 (draft reject) and 6 (attachment unlink)

- [ ] **Step 1: Add `inlineConfirm()` helper function**

Add to the main JS section. Contract:
- Takes a button element and options `{ onConfirm, confirmLabel }`.
- Only one armed at a time — arming a new one disarms any existing.
- Changes button content using DOM methods (createElement for "Yes"/"Cancel" spans, not string concatenation).
- Auto-disarms after 3 seconds.
- "Yes" click calls `onConfirm()`, "Cancel" or timeout restores original button content.

Expose as `window.inlineConfirm`.

- [ ] **Step 2: Replace draft reject `confirm()` with inline confirm**

In script 4 (~line 4085), replace the `if (!confirm(...)) return;` pattern. Use `inlineConfirm(rejectBtn, { ... })`. The `onConfirm` callback does the DELETE fetch. On success, show undo toast. Undo path: call a `/restore` endpoint if available, otherwise use modal confirmation instead (check if restore endpoint exists in serve.py first — if not, use a custom modal matching the serve.py pattern from Task 1B).

- [ ] **Step 3: Replace attachment unlink `confirm()` with inline confirm**

In script 6 (~line 4384), replace the `if (!confirm(...)) return;` pattern. Use `inlineConfirm(unlinkBtn, { ... })`. Undo path: re-POST the attachment link.

- [ ] **Step 4: Verify no remaining `confirm()` or `alert()` calls**

Run: `grep -n "confirm\b\|alert(" src/generate.py | grep -v "//\|Confirm\|modal\|inline\|gate\|detail-gate"`
Expected: No bare native dialog calls.

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/ -v`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add src/generate.py
git commit -m "feat: inline confirm with undo for reversible actions — no native dialogs"
```

---

### Task 2E: Bottom Lane Cohesion

**Files:**
- Modify: `src/generate.py` — CSS (bottom section styles ~lines 876-980), `_render_list_rows()` (~line 4802)

- [ ] **Step 1: Update bottom section CSS**

Update `.list-row` to use card tokens (`--bg-card`, `--border-subtle`, `--bg-hover`), 6px border-radius, and section-based left-border color (3px solid, matching kanban card pattern). Update `.bottom-section-header` to match `.column-header` styling (same font-size, weight, spacing, dot + name + count pattern).

- [ ] **Step 2: Add readiness dots to list rows**

In `_render_list_rows()`, call `_render_readiness_row(t)` for parent rows and include the output in the list row HTML (after `.list-row-main`, before detail). Also add the card-open-btn with SVG icon.

- [ ] **Step 3: Add CSS for readiness dots in list rows**

Slightly smaller sizing for compact rows:
```css
.list-row .readiness-row { padding: 2px 10px 6px; display: flex; gap: 3px; }
.list-row .readiness-dot { width: 14px; height: 14px; }
.list-row .readiness-dot svg { width: 10px; height: 10px; }
```

- [ ] **Step 4: Run smoke tests**

Run: `python3 -m pytest tests/test_smoke_ui.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add src/generate.py
git commit -m "feat: bottom lane visual cohesion — shared tokens, readiness dots, left-border color"
```

---

### Task 2F: Component Normalization + Focus States

**Files:**
- Modify: `src/generate.py` — CSS block only

- [ ] **Step 1: Add global focus-visible ring**

```css
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
```

- [ ] **Step 2: Normalize scrollbar styling across all scrollable areas**

Extend existing column scrollbar rules to `.detail-body` and `.bottom-section-body`.

- [ ] **Step 3: Verify consistent transition timing**

Audit CSS for any transitions not using 0.15s (except keyframe animations). Normalize outliers.

- [ ] **Step 4: Commit**

```bash
git add src/generate.py
git commit -m "fix: component normalization — focus rings, scrollbars, consistent transitions"
```

---

### Task 2G: Motion Guardrails (prefers-reduced-motion)

**Files:**
- Modify: `src/generate.py` — CSS block

- [ ] **Step 1: Add reduced-motion media query at end of CSS**

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

Uses 0.01ms (not 0s) to avoid breaking JS `transitionend` handlers.

- [ ] **Step 2: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add src/generate.py
git commit -m "feat: respect prefers-reduced-motion — disable all animation when requested"
```

---

## Phase 3 — Integration Verification

---

### Task 3A: Full Verification Pass

- [ ] **Step 1: Run complete test suite**

```bash
python3 -m pytest tests/test_tdd_*.py tests/test_smoke_*.py tests/test_e2e_*.py -v
```

Fix any assertions broken by inner content changes (emoji to SVG).

- [ ] **Step 2: Manual theme verification**

Start server, verify in browser:
- [ ] Dark mode renders correctly
- [ ] Light mode renders correctly
- [ ] System mode follows OS preference
- [ ] No theme flash on reload
- [ ] Project picker has theme support
- [ ] Settings page has theme support
- [ ] file:// mode defaults to system theme

- [ ] **Step 3: Light theme quality checklist**

In light mode, verify:
- [ ] Status badges legible against white backgrounds
- [ ] Lower-lane rows visually distinct
- [ ] Hover/focus states clearly visible
- [ ] Modal/drawer backdrop contrast adequate
- [ ] Metadata chips not washed out

- [ ] **Step 4: Toast verification**

- [ ] Success toast (green) works
- [ ] Error toast (red) works
- [ ] Undo toast with clickable undo works
- [ ] Clipboard copy doesn't displace active undo toast

- [ ] **Step 5: Icon verification**

- [ ] Cards show SVG readiness dots
- [ ] Overlay header shows same SVG icons
- [ ] Action buttons show SVG icons
- [ ] Icons inherit text color in both themes

- [ ] **Step 6: Bottom lane verification**

- [ ] Readiness dots visible
- [ ] Left-border colors match section
- [ ] Same component rendering as kanban cards

- [ ] **Step 7: No remaining native dialogs**

```bash
grep -n "confirm\b\|alert(" src/generate.py src/serve.py | grep -v "//\|Confirm\|modal\|inline\|gate\|detail-gate"
```

- [ ] **Step 8: Deploy runtime files**

```bash
cp src/generate.py ~/.claude/ticket-takeaway/generate.py
cp src/generate.py ~/.claude/dashboard/generate.py
cp src/serve.py ~/.claude/ticket-takeaway/serve.py
cp src/constants.py ~/.claude/ticket-takeaway/constants.py
```

- [ ] **Step 9: Final commit if test fixups needed**

```bash
git add -A
git commit -m "fix: test adjustments for UI consistency pass"
```

- [ ] **Step 10: Merge to main when verified**

```bash
git checkout main
git merge ui-consistency-pass
```
