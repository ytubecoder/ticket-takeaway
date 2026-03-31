---
name: feedbacks
description: Screen+voice capture for visual feedback. Setup whisper.cpp, launch the capture app, or analyze a session. When ticket-takeaway is installed, adds SDLC context (ticket-linked output, context push after analysis).
user_invocable: true
arguments:
  - name: command
    description: "Optional: 'setup', 'start', 'start {ticket-id}', 'analyze', or a path to a session. Omit to auto-detect."
    required: false
---

# Feedbacks — Capture + Ticket-Takeaway Integration

This skill handles everything: first-time setup, launching the capture app, and analyzing sessions.
It works from **any project directory** — the feedbacks tool lives at `/home/user/projects/feedbacks`.

When ticket-takeaway is installed, this skill adds:
- Ticket-linked output directories (`/feedbacks start {ticket-id}`)
- Context push after analysis (session path + summary available to the agent)

**FEEDBACKS_HOME:** `/home/user/projects/feedbacks`

## Determine what to do

Check `$ARGUMENTS.command`:

- If `setup` → go to **Setup**
- If `start` (with or without ticket-id) → go to **Start**
- If `analyze` or a file/directory path → go to **Analyze**
- If omitted → **Auto-detect**:

### Auto-detect logic

1. Check if FEEDBACKS_HOME exists: `ls /home/user/projects/feedbacks/start.sh`
   - If not → tell the user: "Feedbacks is not installed. Install from https://github.com/ytubecoder/feedbacks for screen+voice capture."
2. Check if `whisper.cpp/build/bin/whisper-server` exists in FEEDBACKS_HOME
   - If not → tell the user: "First time? Running setup." → go to **Setup**
3. Check if whisper-server is already running: `curl -sf http://localhost:8081/health`
   - If not running → go to **Start**
   - If running → go to **Analyze** (app is already up, user probably has a session to review)

---

## Setup

Install whisper.cpp and download a model. Run these commands:

```bash
cd /home/user/projects/feedbacks

# Clone and build whisper.cpp
git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp
cmake -B build
cmake --build build -j --config Release
cd ..

# Download the base English model
sh whisper.cpp/models/download-ggml-model.sh base.en
```

Run each step, check for errors between steps. If `cmake` or build tools are missing, install them:
```bash
sudo apt update && sudo apt install -y build-essential cmake
```

After setup completes, tell the user:
> Setup complete! Run `/feedbacks` again to start the capture app.

---

## Start

Launch the capture app and whisper server.

1. First, check if ports 8080/8081 are already in use:
   ```bash
   curl -sf http://localhost:8081/health && echo "Whisper already running" || echo "Whisper not running"
   curl -sf http://localhost:8080/ && echo "App already running" || echo "App not running"
   ```

2. **Determine output directory** based on context:

   **If a ticket-id was provided** (e.g., `/feedbacks start B-05`):
   - Resolve the current project root (check for `PRODUCT_BACKLOG.md` in cwd or parents)
   - Set output dir to: `{project_root}/.feedbacks/{ticket-id}/`
   - Include `?ticket={ticket-id}` in the URL

   **If no ticket-id:**
   - If `FEEDBACKS_OUTPUT_DIR` is already set in the environment, use it
   - Otherwise, use the feedbacks default (sessions save to `~/projects/feedbacks/sessions/`)

3. If not running, start the server:
   ```bash
   FEEDBACKS_OUTPUT_DIR=/path/to/output cd /home/user/projects/feedbacks && ./start.sh
   ```
   Run this in the background so the user can continue using Claude Code.

4. Tell the user:

   **With ticket context:**
   > Feedbacks is running at **http://localhost:8080/?ticket={ticket-id}**
   > Sessions will save to: `{project_root}/.feedbacks/{ticket-id}/`
   > Open it in Chrome, capture your session, then come back.

   **Without ticket context:**
   > Feedbacks is running at **http://localhost:8080**
   > Sessions will save to the default location.
   > Open it in Chrome, capture your session, then run `/feedbacks` to analyze it.

---

## Analyze

Ingest and analyze a captured feedback session.

### Finding the session

If a path was provided in `$ARGUMENTS.command`, use it directly. Otherwise:

1. **Check server output directory first** — query the running server for its config:
   ```bash
   curl -sf http://localhost:8080/config
   ```
   If it returns an `outputDir`, use Glob to find the latest `feedbacks-*/session.md` in that directory.
   Sessions are saved as extracted directories (not ZIPs) with this structure:
   ```
   {outputDir}/feedbacks-{timestamp}/
     session.md
     player.html
     images/001.png, 002.png, ...
   ```

2. **Check project-specific `.feedbacks/` directory** — if running from a project context:
   ```bash
   ls -dt {project_root}/.feedbacks/*/feedbacks-*/session.md 2>/dev/null | head -1
   ```

3. **Fallback to Downloads** — check the user's download directory for ZIP files:
   - Check `~/.claude/memory/feedbacks_download_dir.md` for saved download path
   - Use Glob to find the latest `feedbacks-*.zip`
   - If no memory exists, ask the user for their download directory and save it to memory
   - If the path points to a `.zip` file, extract it:
     ```bash
     unzip -o <path-to-zip> -d /tmp/feedbacks-session
     ```
     Then use the extracted directory.

### Processing the session

1. Read `session.md` from the session directory
2. Parse each section — they follow this pattern:
   ```
   ## TIMESTAMP
   ![Screenshot N](./images/NNN.png)
   **[Marker N — user clicked at (x, y)]**
   > Transcript text...
   ```
3. **Coherence pass**: The transcript was progressively transcribed in ~10s chunks. Quickly scan it for:
   - Obvious chunk-boundary artifacts (cut-off sentences between sections)
   - Repeated words at boundaries
   - If you spot issues, silently smooth them in your interpretation — don't flag minor STT artifacts to the user
4. For each section, read the referenced screenshot image using the Read tool (it supports images)
5. **Correlate markers with speech**: When the transcript says "this", "here", "that area" etc., map those deictic references to the numbered markers visible in the screenshot. The marker number tells you exactly what the user was pointing at.
6. **Describe each screenshot** using this structured format. Extract as much context as possible from the image itself — the user's voice only tells half the story.

   For each screenshot, produce:

   ```
   ### Screenshot N · {timestamp}

   **Screen:** {what app/page is shown}
   **URL:** {visible URL from browser address bar, or "not visible"}
   **Page title:** {tab title or page heading if readable}
   **Cursor:** {where the cursor is}
   **Marker {N}:** {what the marker is pointing at}
   **Interaction:** {what the user did — click, drag-select, hover}
   **Visible state:** {anything notable about the current UI state}

   **User said:** "{transcript text}"

   **Interpretation:** {one sentence combining what the user pointed at with what they said}
   ```

   **Field rules:**
   - **Screen**: Identify the app from visual cues. Be specific.
   - **URL**: Read literally from address bar. "not visible" if cropped.
   - **Cursor**: Describe relative to UI elements, not pixel coordinates.
   - **Marker**: Describe what marker is *on top of*, not the marker itself.
   - **Visible state**: Only note what's relevant.
   - **Interpretation**: Fuse visual + verbal evidence. One clear sentence.

   If a screenshot has no marker and no transcript (auto-captured context frame):
   ```
   ### Screenshot N · {timestamp}
   **Screen:** {app/page}
   **Context frame** — no user interaction. {Brief note.}
   ```

7. After presenting all sections, provide a **summary analysis**:
   - **Feedback points**: Each issue raised, with screenshot number, marker, one-line description
   - **Navigation path**: Sequence of screens/pages visited
   - **UI/UX issues**: Problems visible that user may or may not have mentioned
   - **Suggested action items**: Concrete fixes, each linked to a specific screenshot

### Context push (ticket-takeaway integration)

After the analysis is complete, push session context for the agent:

1. Determine the session path (the directory that was analyzed)
2. Check if `summary.json` exists in the session — if yes, read the summary. Otherwise, use the summary from the analysis above.
3. Output to the agent context:

   > **Feedback session:** `{session_path}`
   > **Summary:** {summary text}

   This is informational. The agent can act on it as appropriate — create a ticket, link to an existing one, or just note it. Do not prompt for ticket creation.

### Important

- Read images with their full path: `<session-dir>/images/NNN.png`
- The transcript comes from Whisper (local or cloud) and may have minor errors — interpret charitably
- Screenshots contain numbered red circle markers or red selection boxes — these show exactly where the user clicked/selected
- Focus on understanding the user's intent by combining the visual markers with the spoken context
