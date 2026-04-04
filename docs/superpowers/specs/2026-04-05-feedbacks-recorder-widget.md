# Feedbacks Recorder Widget — Integration Brief

**Date:** 2026-04-05
**From:** ticket-takeaway (consumer)
**To:** feedbacks team (provider)
**Status:** Proposed

## Problem

Ticket-takeaway opens the feedbacks app via URL (`http://localhost:8080/?ticket=B-24&callback=...&mode=recorder`) when a user clicks "Record" on a ticket. Currently this lands on the full feedbacks home page with no awareness of the URL params beyond `?ticket=`. The user has to manually navigate to start recording, and there's no way to return control to ticket-takeaway when done.

## What We Need

### 1. Compact Recorder Widget (`?mode=recorder`)

When feedbacks opens with `?mode=recorder`, show a minimal popup-style UI instead of the full app. This is meant to be opened in a small window or popup from another app.

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

### 2. Callback on Session Complete (`?callback=URL`)

When a recording session is saved, POST the session metadata to the callback URL.

**POST payload:**
```json
{
  "ticket_id": "B-24",
  "session_name": "feedbacks-B-24-20260405-143022",
  "session_path": "/home/user/projects/feedbacks/sessions/feedbacks-B-24-20260405-143022",
  "summary": "User pointed at the settings drawer and noted the status dot doesn't update",
  "image_count": 5,
  "duration": "1m 23s",
  "stt_count": 3
}
```

**Timing:** Fire the callback after the session is fully saved (files written, summary generated if applicable). Don't wait for AI summary if it takes too long — send what's available immediately and optionally send a second callback when summary is ready.

**Error handling:** If the callback URL is unreachable, save the session normally anyway. Don't block the user. Log the failure to console.

### 3. Return to Referrer (`?referrer=URL` or auto-close)

After the session is saved and callback fired:
- If opened as a popup (`window.opener` exists): close the window
- If opened as a tab: redirect to `?referrer` URL if provided, otherwise show a "Session saved, you can close this tab" message

### 4. Output Directory from URL (`?output_dir=PATH`)

Currently `FEEDBACKS_OUTPUT_DIR` is set via environment variable at server start. For per-ticket recording, we need to set it per-session:

- If `?ticket=X` is provided and `FEEDBACKS_OUTPUT_DIR` is set, save to `{FEEDBACKS_OUTPUT_DIR}/feedbacks-{ticket}-{timestamp}/`
- This already partially works (ticket ID is in the folder name). Just confirm it respects `FEEDBACKS_OUTPUT_DIR` for the base path.

## What We Don't Need

- No changes to the existing full-app UI — `?mode=recorder` is additive
- No changes to the MCP server
- No changes to whisper integration (recorder widget uses the same transcription path)
- No authentication — this is all localhost

## Existing Integration Points (Already Working)

These are already implemented and working — no changes needed:

| Feature | Status |
|---------|--------|
| `?ticket=X` pre-fills ticket ID | Working |
| Session folder includes ticket ID | Working |
| `GET /config` returns output dir | Working |
| `GET /sessions` lists all sessions | Working |
| `POST /save` saves session data | Working |
| Live push events via `/live-push` | Working |
| MCP tools for session access | Working |

## Integration Flow (End-to-End)

```
ticket-takeaway                          feedbacks
     |                                       |
     |  1. User clicks "Record" on ticket    |
     |                                       |
     |  2. Opens popup:                      |
     |     ?ticket=B-24                      |
     |     &mode=recorder                    |
     |     &callback=http://localhost:8787/  |
     |      ticket-takeaway/api/feedbacks/   |
     |      callback                         |
     |                                       |
     |                          3. Compact recorder UI loads
     |                          4. User records session
     |                          5. User clicks Stop
     |                          6. Session saves to disk
     |                                       |
     |  7. POST callback with metadata  <----|
     |                                       |
     |  8. ticket-takeaway creates           |
     |     attachment record in DB      9. Popup closes
     |                                       |
     |  10. Attachments list refreshes       |
     |      showing the new session          |
```

## Priority

This is the last piece needed for the record flow (B-29) to work end-to-end. Everything else on the ticket-takeaway side is built and waiting. The recorder widget is the gating dependency.

## Questions for Feedbacks Team

1. **Auto-start vs manual start** — Should the recorder auto-start capture when opened in `?mode=recorder`, or show a Start button? Auto-start is smoother but might surprise users.
2. **Summary timing** — Do you want to generate the AI summary synchronously before firing the callback, or fire immediately with basic metadata and send a second callback when summary is ready?
3. **Window sizing** — Any constraints on minimum window size for the capture overlay to work? We'll open it as `window.open(url, '_blank', 'width=350,height=250')`.
