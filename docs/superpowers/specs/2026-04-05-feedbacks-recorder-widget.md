# Feedbacks Recorder Widget — Integration Brief

**Date:** 2026-04-05
**From:** ticket-takeaway (consumer)
**To:** feedbacks team (provider)
**Status:** Proposed

## Context

Ticket-takeaway has a "Record" button on each ticket that opens the feedbacks app via URL (`http://localhost:8080/?ticket=B-24&mode=recorder`). The goal is a smooth record-and-return flow: user clicks Record, captures feedback, and the session automatically links back to the ticket.

**We handle session detection on our side** — ticket-takeaway watches the output directory for new session folders and reads `meta.json` when it appears. No callback or webhook is needed from feedbacks. This brief only covers what we need from the feedbacks UI.

## What We Need

### 1. Compact Recorder Widget (`?mode=recorder`)

When feedbacks opens with `?mode=recorder`, show a minimal popup-style UI instead of the full app. This is meant to be opened in a small popup window from another app.

**Controls needed:**
- **Start** button — begins screen capture + transcription
- **Pause/Resume** button — pauses recording without ending the session
- **Stop** button — ends recording, triggers save
- Session timer showing elapsed time
- Visual indicator that recording is active (e.g., pulsing red dot)

**Behavior:**
- Auto-starts recording on load (no extra click needed) OR shows a single "Start Recording" button — your call on UX
- Window should be small/compact (think ~300x200px content area)
- No navigation, no session list, no settings — just the recording controls
- Uses the same capture + transcription path as the full app (no new backend work)

### 2. Auto-Close on Save

After the session is saved:
- If opened as a popup (`window.opener` exists): close the window automatically
- If opened as a tab: show a brief "Session saved" message, then close after 2-3 seconds (or let the user close manually)

That's it. No callback POST, no redirect URL, no new API endpoints.

## What We Don't Need

- **No callback/webhook** — we detect new sessions by watching the output directory for new `meta.json` files
- **No new API endpoints** — existing `/save`, `/config`, `/sessions` are sufficient
- **No changes to the full-app UI** — `?mode=recorder` is additive
- **No changes to the MCP server**
- **No changes to whisper integration**
- **No changes to session save format** — current `meta.json` structure is perfect
- **No authentication** — this is all localhost

## How We Detect Sessions (Our Side, FYI)

You don't need to do anything for this — just documenting so you know how it works:

1. User clicks "Record" on ticket B-24 in ticket-takeaway
2. We snapshot the current contents of your output directory
3. Feedbacks opens in a popup with `?ticket=B-24&mode=recorder`
4. User records and clicks Stop — feedbacks saves as normal
5. We poll the output directory every 2-3s for new subdirectories
6. New directory appears → we check for `meta.json` inside it
7. `meta.json` exists → session is complete (we verified this is the last file written in `_handle_save`)
8. We read `meta.json`, match `ticketId` to the ticket, create an attachment record

This relies on the existing save behavior where `meta.json` is written after all other files. If that write order ever changes, let us know.

## Existing Integration Points (Already Working)

| Feature | Status |
|---------|--------|
| `?ticket=X` pre-fills ticket ID | Working |
| Session folder named `feedbacks-{ticket}-{timestamp}` | Working |
| `meta.json` includes `ticketId` field | Working |
| `meta.json` written last in save sequence | Working |
| `GET /config` returns output directory | Working |
| `FEEDBACKS_OUTPUT_DIR` env var respected | Working |

## Integration Flow (End-to-End)

```
ticket-takeaway                          feedbacks
     |                                       |
     |  1. User clicks "Record" on B-24     |
     |  2. Snapshot output dir contents      |
     |  3. Open popup:                       |
     |     ?ticket=B-24&mode=recorder        |
     |                                       |
     |                          4. Compact recorder UI loads
     |                          5. User records session
     |                          6. User clicks Stop
     |                          7. Session saves to disk
     |                             (meta.json written last)
     |                          8. Popup auto-closes
     |                                       |
     |  9. File watcher detects new dir      |
     | 10. Reads meta.json, matches ticket   |
     | 11. Creates attachment in DB           |
     | 12. Attachments list refreshes        |
```

## Summary of Ask

| Item | Effort | Priority |
|------|--------|----------|
| `?mode=recorder` compact UI | Medium | Required |
| Auto-close popup on save | Small | Required |
| (Everything else) | Zero | We handle it |

## Decisions

1. **Manual start with opt-in auto-start** — First open shows a "Start Recording" button. Include a "Start automatically next time" checkbox. If checked, persist the preference (localStorage is fine) and auto-start on future opens.
2. **Window size** — `window.open(url, '_blank', 'width=440,height=310')`. Design the compact UI to fit comfortably in that space.
