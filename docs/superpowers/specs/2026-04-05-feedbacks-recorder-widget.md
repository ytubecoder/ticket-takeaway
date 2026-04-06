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

### 3. Auto-Start via URL Param (`?autostart=1`)

Ticket-takeaway manages an "Auto-start recording" user preference in its settings. When enabled, we append `&autostart=1` to the recorder URL. Feedbacks should:

- If `?autostart=1` is present: skip the Start button and begin capture immediately on page load
- If absent: show the Start button as normal (default behavior)

This is controlled entirely from our side — no need for feedbacks to store this preference. Just read the URL param and act on it.

### 4. Transcription Status Indicator

During recording, the user needs visual confirmation that their speech is being captured. Whisper processes audio in ~10s chunks and the transcript appears in the timeline — but there's no clear indicator that transcription is actively happening.

**What we need:**

A visible status indicator in the recorder UI that shows transcription activity:

- **Idle** — no speech detected, no indicator (or subtle "Listening..." text)
- **Transcribing** — a chunk of audio has been sent to whisper and is being processed. Show a visual indicator (e.g., animated dots, a small waveform, or text like "Transcribing...") so the user knows their speech was captured
- **Done** — the chunk was transcribed successfully. Brief flash of confirmation (e.g., checkmark or the first few words appearing)

**Why this matters:**

The user is recording feedback while looking at another window (the one being captured). They can't see the timeline building up. Without a transcription indicator, they have no way to know if whisper is actually processing their speech — and losing a spoken segment without knowing is a bad experience. The indicator gives confidence that "yes, the system heard me."

**Scope:**

This should work in both the full app and the `?mode=recorder` compact widget. In the compact widget it's especially important since the user can't see the timeline at all — the indicator is their only feedback that speech capture is working.

**Implementation suggestion (your call):**

When a chunk is sent to `/transcribe`, show "Transcribing..." near the recording timer. When the response comes back, briefly flash the first few words of the transcript (e.g., "✓ The settings drawer...") then fade back to the recording state. This reuses the existing whisper request/response cycle — no new backend work needed.

## Summary of Ask

| Item | Effort | Priority |
|------|--------|----------|
| `?mode=recorder` compact UI | Medium | Required |
| Auto-close popup on save | Small | Required |
| `?autostart=1` support | Small | Required |
| Transcription status indicator | Medium | Required |
| (Everything else) | Zero | We handle it |

## Decisions

1. **Auto-start controlled by URL param** — ticket-takeaway sends `&autostart=1` when the user has the setting enabled. Feedbacks reads the param and skips the Start button if present. Default (no param) shows Start button.
2. **Window size** — `window.open(url, '_blank', 'width=550,height=420')`. Design the compact UI to fit comfortably in that space.
