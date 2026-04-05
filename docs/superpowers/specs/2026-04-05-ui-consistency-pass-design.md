# UI Consistency Pass — Design Spec

**Date:** 2026-04-05
**Scope:** Visual consistency and rationalization of the Ticket Takeaway dashboard. No route changes, no DB schema changes, no API field renames, no framework migration.

## Context

The dashboard has outgrown its "simple generated HTML board" phase. Multiple UI patterns accumulated organically — three separate toast implementations, mixed emoji/letter readiness indicators, native browser dialogs alongside custom modals, and visually disconnected bottom lanes. This pass makes the existing integrated surface feel intentional rather than accreted, and adds light/dark/system theming.

## Design Direction

**Blended: Primer restraint for chrome + Atlassian warmth for content.**

- Chrome (header, filter bar, settings, bottom lane headers): tight spacing, flat, muted borders, no shadows
- Content (cards, overlay, badges, readiness indicators): slightly warmer, 6px radius, subtle left-border color coding, light shadow in light mode only
- Dark mode: close to current palette with slightly warmer grays (`#0c0c0e` page, `#1b1b20` cards)
- Light mode: neutral white (`#f8f9fa` page, `#ffffff` cards), subtle borders, light card shadow

## 1. Light / Dark / System Theming

### Scope

Theme applies to **all user-facing HTML surfaces**: project dashboard, project picker, project settings, and read-only file:// mode. Theme preference is presentation-only — not server state.

### Mechanics

- `<html data-theme="dark|light">` attribute controls active theme
- Default: follow `prefers-color-scheme` media query (system)
- User override: stored in `localStorage('tt-theme')`, three states: `system`, `light`, `dark`
- Theme toggle: add to settings drawer (gear panel), three-way selector
- **Flash prevention:** Theme init script runs in `<head>` before body renders (synchronous `<script>` that reads localStorage and sets `data-theme` before any paint). Not in DOMContentLoaded.
- **file:// mode:** Theme uses system preference by default. If localStorage is available (same-origin context), a saved override is honored; otherwise falls back to system. No assumption that localStorage will work in file:// mode.
- On load: check localStorage → if `system` or absent, read media query → set `data-theme`

### Color Tokens

**Dark (default, close to current):**
```
--bg-page: #0c0c0e    --bg-surface: #151518    --bg-card: #1b1b20    --bg-hover: #232329
--border-subtle: #1f1f26    --border-default: #2c2c35    --border-strong: #3c3c47
--text-primary: #eaeaed    --text-secondary: #9e9eab    --text-tertiary: #6a6a76
```

**Light:**
```
--bg-page: #f8f9fa    --bg-surface: #ffffff    --bg-card: #ffffff    --bg-hover: #f3f4f6
--border-subtle: #e5e7eb    --border-default: #d1d5db    --border-strong: #9ca3af
--text-primary: #111827    --text-secondary: #6b7280    --text-tertiary: #9ca3af
```

**Shared (same in both themes):**
Status colors, priority colors, and accent stay the same — they're designed to work on both backgrounds. Badge background opacities adjust: 12% in dark, 8% in light.

### Light Theme Quality Bar

Light mode must not rely on shadows alone for separation. Borders, tone shifts, and spacing define grouping. Avoid low-contrast gray-on-white controls. Validate in light mode:
- Status badges legible against white card backgrounds
- Lower-lane rows visually distinct from each other
- Hover/focus states clearly visible
- Modal/drawer layering (backdrop contrast)
- Read-only metadata chips not washed out

### Files Changed

- `src/generate.py`: duplicate `:root` block as `[data-theme="light"]`, add theme init script in `<head>`, add toggle to settings drawer
- `src/serve.py`: apply same theme system to project picker and settings pages

## 2. Unified Toast System

### Current State (3 separate implementations)

1. `.detail-toast` — top-right inside overlay, green, 1500ms
2. `.copied-toast` — top-right relative to card, green, brief
3. `.undo-toast` — bottom-center, transform slide-up

### Target State (1 system)

Single `#app-toast` element, fixed position bottom-center of viewport. Handles all notification types:

- **Success** (green tint): "Saved!", "Ticket created", "Moved to WIP"
- **Error** (red tint): "Failed to confirm ticket", "Failed to unlink attachment"
- **Undo** (neutral tint): "Ticket deleted — Ctrl+Z to undo", "Attachment unlinked — Ctrl+Z to undo"

### Mechanics

- `showToast(message, type='success', duration=2500)` — single global function
- Type determines left-border color: green/red/neutral
- Undo toasts include a clickable "Undo" text that triggers the undo action directly (in addition to Ctrl+Z)
- Duration: 2500ms for success, 4000ms for error, 5000ms for undo (longer to give time to act)
- Position: fixed, bottom 24px, centered horizontally
- Animation: slide up + fade in (0.2s), slide down + fade out (0.2s)

### Priority Rules

Toasts have priority tiers. A higher-priority toast displaces a lower one, but not vice versa:

| Tier | Types | Behavior |
|------|-------|----------|
| High | error, undo | Cannot be displaced by lower tiers. Must run full duration. |
| Low | success, info, copy | May replace each other. Cannot overwrite an active high-priority toast. |

A clipboard "Copied!" toast must not overwrite an active undo toast. An error toast arriving during an undo toast queues behind it and displays after the undo toast completes.

### Replaces

All 6 `alert()` error calls → `showToast(msg, 'error')`
All 3 toast implementations → single `showToast()` calls

## 3. Replace Native Dialogs

### Reversible Actions → Inline Confirm + Undo

**"Delete this draft ticket?"** and **"Unlink this attachment?"**

Inline confirm contract:
- First click arms the confirm state — button text changes to "Sure? Yes / Cancel"
- Confirm executes on explicit second action (click "Yes")
- "Cancel" or clicking elsewhere restores immediately
- Armed state auto-resets after 3 seconds if no action
- **Only one inline confirm may be armed at a time** within the same surface — arming a new one cancels any existing armed state
- On confirm: execute action, push to undo stack, show undo toast
- Ctrl+Z or clicking "Undo" in toast reverses the action

### Undo Reliability Requirement

"Undoable" means actual restoration, not just a toast message:
- Draft ticket delete: restores the ticket and its visible state (re-POST or soft state)
- Attachment unlink: restores the attachment in UI and underlying state (re-link API call)
- If restoration requires a lightweight refetch from server, that is acceptable
- **If reliable undo cannot be implemented cleanly for a given action, that action must use modal confirmation instead** — do not ship inline confirm without working undo

### Truly Destructive Actions → Custom Modal

**"Remove this project?"** (settings page)

- Custom modal matching overlay aesthetic: dark backdrop with blur, centered panel
- Clear destructive language, red-tinted confirm button
- No undo — this is permanent

### Files Changed

- `src/generate.py`: remove all `alert()` and `confirm()` calls from inline JS, add `showToast()`, add inline confirm pattern, add custom modal component
- `src/serve.py`: replace `confirm()` in settings page JS with custom modal

## 4. Readiness Indicators — Inline SVG Icons

### Current State

- Cards: Unicode emoji (📄 ☑ 💨 🔬 👁) — renders differently per OS
- Overlay header: Letters (D C S T L)

### Target State

Inline SVG sprite in generated HTML. A single `<svg>` block with `<symbol>` definitions at the top of the document, referenced everywhere via `<use href="#icon-name">`. Uses `currentColor` so icons inherit text color and respond to theming automatically. Identical rendering on Windows, Mac, and Linux. Zero external dependencies, works in file:// mode.

### Icon Set

Readiness (canonical order D C S T L):
- D (Description): file-text
- C (Criteria): check-square
- S (Smoke): flame
- T (Tests): flask-conical
- L (Learnings): eye

UI chrome:
- close (x), expand (arrow-up-right), settings (settings/gear), chevron-down, plus, trash-2, undo-2, grip-vertical (drag handle), search, sun, moon

### Accessibility

All icon-only controls must include both `title` and `aria-label`. Readiness indicators preserve `data-flag` attribute and canonical D C S T L order everywhere.

### Cost

~3-4KB added to HTML (~2% of current size). Faster than emoji (no font fallback lookup) and faster than CDN (no network request). SVG sprite is defined once; `<use>` references add ~30 bytes each.

### Files Changed

- `src/generate.py`: add SVG sprite block in `<body>` top, replace emoji in cards, replace letters in overlay, replace Unicode symbols in action buttons (gear, arrows, close, etc.)

## 5. Bottom Lane Cohesion

### Principle

Same data, same components, different density. Archive shelf below, active workspace above. Visual continuity — your eyes don't relearn anything scanning down.

### Changes

- **Same tokens**: bottom lane backgrounds, borders, hover use the same `--bg-card`, `--border-subtle`, `--bg-hover` as kanban cards
- **Same components**: priority dot, status badge, complexity badge, readiness dots all rendered identically to their kanban card counterparts (same classes, same sizing)
- **Same left-border color coding**: list rows get the same 3px left-border treatment as kanban cards (section-appropriate color)
- **Section headers**: normalize to match column headers — dot + name + count, same font weight and spacing
- **Layout order within each row**: priority dot → ID → title → status badge → complexity → readiness dots (mirrors the visual scan order of a kanban card, just horizontal)
- **Hover**: same background shift as kanban cards

### Behavior Boundary

Lower lanes share visual primitives with active-board cards but do **not** gain full interaction parity:
- No drag-and-drop (workflow buttons handle resurrection to active sections)
- No accidental behavior expansion unless explicitly specified
- Compact archival layout remains correct
- Collapsible sections with toggle arrows stay
- Denser padding stays (archival density is intentional)

Goal is visual continuity, not identical mechanics.

### Files Changed

- `src/generate.py`: update bottom section CSS and HTML rendering to use shared card component classes

## 6. Remove "Coming Soon" Placeholder

The "Full ticket form" expand button and "Coming soon" div are removed entirely from the new-ticket panel. The quick-create flow (title + section dropdown + Create button) remains as-is. Gone until it's real.

### Files Changed

- `src/generate.py`: remove `.new-ticket-expand-btn`, `#newTicketFull`, and associated JS

## 7. Fix Feedbacks Repo/Link Mismatch

Two different GitHub URLs exist:
- `src/constants.py:129`: `FEEDBACKS_REPO_URL = "https://github.com/user/feedbacks"` (placeholder — wrong)
- `src/generate.py:1651`: hardcoded `https://github.com/ytubecoder/feedbacks` (correct value, wrong location)

Fix:
1. Update `constants.py` to the correct canonical URL (`https://github.com/ytubecoder/feedbacks`)
2. Remove all hardcoded feedbacks GitHub URLs in UI code
3. Dashboard, settings, and install flows all read from the constant

### Files Changed

- `src/constants.py`: update `FEEDBACKS_REPO_URL` to correct value
- `src/generate.py`: import and use `FEEDBACKS_REPO_URL` from constants instead of hardcoded URL

## 8. Component Normalization (Polish)

These are smaller consistency fixes applied throughout:

- **Buttons**: unify border-radius (6px for chrome buttons, 6px for card action buttons), consistent padding, consistent font-weight
- **Badges**: all status badges use same border-radius (10px pill), same font-size (9px), same padding, same uppercase treatment
- **Counters**: column counts and filter counts use same style (monospace, same bg/border treatment)
- **Headers**: column headers and bottom section headers share exact same CSS class
- **Hover states**: consistent 0.15s transition timing everywhere, same `--bg-hover` target
- **Focus states**: add visible focus ring (`outline: 2px solid var(--accent); outline-offset: 2px`) to all interactive elements that currently lack it
- **Scrollbar styling**: extend the existing column scrollbar styling to any scrollable area (overlay body, bottom section bodies)

## 9. Motion

### Existing (Keep)

- Card enter/exit/moved (0.3s fade + slide)
- Priority dot pulse (2s, high priority only)
- Panel slide (0.15s)
- Content flash (0.8s on update)
- Overlay backdrop fade (0.2s)

All transitions stay at 0.15s standard.

### New (Up to 2 allowed)

- **Archive lane transition**: brief highlight when a card moves to a bottom lane (reuse `card-moved` glow, adapted for list row)
- **Confirm/undo feedback**: subtle flash on inline confirm arm/execute (reuse `content-flash`)

### Reduced Motion

Wrap all animations and transitions in `@media (prefers-reduced-motion: no-preference)`. When reduced motion is preferred, all animations are disabled and transitions are instant.

## What Is Explicitly Out of Scope

- No external icon library or CDN dependency (inline SVG sprite only)
- No route changes
- No DB schema changes
- No API field renames
- No framework migration
- No trash/bin lane (future feature, needs DB schema)
- No new ticket full form (removed placeholder, build later)
- No architecture/source-of-truth rationalization

## Files Modified (Summary)

| File | Changes |
|------|---------|
| `src/generate.py` | Theme system, SVG icon sprite, toast unification, dialog replacement, readiness icon unification, bottom lane cohesion, component normalization, coming-soon removal, feedbacks URL fix, reduced-motion wrapper |
| `src/serve.py` | Theme system on picker/settings pages, settings page modal for destructive action |
| `src/constants.py` | Update `FEEDBACKS_REPO_URL` to correct canonical URL |

## Test Stability

All existing structural selectors remain unchanged:
- `.card[data-item-id]`, `.column[data-col]`, `#ticket-detail-overlay`, `.filter-btn[data-filter]`, `.status-badge`, `.readiness-dot[data-flag]`, `#searchInput`, `.bottom-section-header`, `textarea[data-field]`
- Card variant classes: `.wip-card`, `.review-card`, `.idea-card`, `.backlog-card`, `.done-card`, `.bug-card`, `.wontdo-card`, `.icebox-card`

Inner content of elements may change (emoji → SVG), but structural selectors and `data-*` attributes are preserved. Tests that assert on inner text/glyph content will be updated where the visual cleanup requires it — no unnecessary test churn.

## Verification

### Automated

1. `python3 -m pytest tests/test_tdd_*.py -v` — business logic unchanged
2. `python3 -m pytest tests/test_smoke_*.py -v` — UI elements still render and respond
3. `python3 -m pytest tests/test_e2e_*.py -v` — full workflows still work

### Manual — Theme

4. Toggle light/dark/system theme, verify all components render correctly in both
5. Reload page after setting theme — no flash of wrong theme
6. Open in file:// mode — defaults to system theme, no errors

### Manual — Light Theme Quality Checklist

7. Status badges legible against white card backgrounds
8. Lower-lane rows visually distinct from each other
9. Hover/focus states clearly visible (not washed out)
10. Modal/drawer layering has adequate backdrop contrast
11. Read-only metadata chips not lost against white background
12. No low-contrast gray-on-white controls or metadata

### Manual — Toast & Dialogs

13. Trigger each toast type (success, error, undo), verify unified appearance and position
14. Trigger clipboard copy while undo toast is active — undo toast not displaced
15. Test inline confirm on draft delete — verify arm/cancel/auto-reset/undo cycle
16. Test modal on project remove — verify backdrop, red button, no undo

### Manual — Icons & Visual

17. Verify SVG icons render consistently on Windows, Mac, and Linux
18. Verify readiness indicators show same icons in both card and overlay views
19. Verify bottom lanes have visual continuity with kanban cards
20. Check `prefers-reduced-motion` — all animation disabled when set
