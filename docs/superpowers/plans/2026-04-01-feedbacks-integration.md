# Feedbacks Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship feedbacks skill from the feedbacks repo and create a ticket-takeaway wrapper skill that layers SDLC context on top, then update `/review` to use it.

**Architecture:** Two repos, each ships a skill to `~/.claude/skills/feedbacks/`. The feedbacks repo ships the base skill (setup, start, analyze). The ticket-takeaway repo ships a superset wrapper that includes all base functionality plus ticket-takeaway context awareness. `/review` is updated to reference the wrapper for its feedbacks steps.

**Tech Stack:** Claude Code skills (markdown), Python (install.py), Bash

**Spec:** `docs/superpowers/specs/2026-04-01-feedbacks-integration-design.md`

---

## File Structure

### feedbacks repo (`~/projects/feedbacks`)

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `skills/feedbacks/SKILL.md` | Base feedbacks skill (setup, start, analyze) — moved from `~/.claude/skills/feedbacks/SKILL.md` |
| Modify | `README.md` | Add Claude Code skill install instructions |

### ticket-takeaway repo (`~/projects/ticket-takeaway`)

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `src/skills/feedbacks/SKILL.md` | Wrapper skill — superset of base, adds ticket-takeaway context awareness |
| Modify | `src/skills/review/SKILL.md` | Formalize feedbacks steps to reference wrapper skill |
| Modify | `install.py` | Deploy feedbacks skill to `~/.claude/skills/feedbacks/` |
| Modify | `INSTALL.md` | Add feedbacks skill to deployment map |

---

### Task 1: Move feedbacks skill into feedbacks repo

**Repo:** `~/projects/feedbacks`

**Files:**
- Create: `skills/feedbacks/SKILL.md`

- [ ] **Step 1: Create the skills directory and copy the skill**

```bash
mkdir -p ~/projects/feedbacks/skills/feedbacks
cp ~/.claude/skills/feedbacks/SKILL.md ~/projects/feedbacks/skills/feedbacks/SKILL.md
```

- [ ] **Step 2: Verify the file content**

```bash
head -10 ~/projects/feedbacks/skills/feedbacks/SKILL.md
```

Expected: The YAML frontmatter starting with `---`, `name: feedbacks`, `description: All-in-one skill...`

- [ ] **Step 3: Commit in feedbacks repo**

```bash
cd ~/projects/feedbacks
git add skills/feedbacks/SKILL.md
git commit -m "feat: ship feedbacks skill from repo

Move the /feedbacks skill (setup, start, analyze) from a manually-installed
location into the repo so it ships as part of the package."
```

---

### Task 2: Update feedbacks README with skill install instructions

**Repo:** `~/projects/feedbacks`

**Files:**
- Modify: `README.md` (lines 11-24, Quick Start section)

- [ ] **Step 1: Add skill installation to Quick Start**

In `README.md`, after the existing "One command (with Claude Code)" section (lines 13-24), replace the manual skill copy hint with a proper install section. Find this block:

```markdown
To use from any project:
```bash
cp -r .claude/skills/feedbacks ~/.claude/skills/
```
```

Replace with:

```markdown
### Install the Claude Code skill

```bash
cp -r ~/projects/feedbacks/skills/feedbacks ~/.claude/skills/feedbacks
```

This installs the `/feedbacks` command globally for Claude Code. After install, `/feedbacks` works from any project directory.
```

- [ ] **Step 2: Verify the edit**

```bash
grep -A 4 "Install the Claude Code skill" ~/projects/feedbacks/README.md
```

Expected: The new install section with the `cp -r` command pointing to `~/projects/feedbacks/skills/feedbacks`.

- [ ] **Step 3: Commit in feedbacks repo**

```bash
cd ~/projects/feedbacks
git add README.md
git commit -m "docs: add skill install instructions to README"
```

---

### Task 3: Create the wrapper skill in ticket-takeaway

**Repo:** `~/projects/ticket-takeaway`

**Files:**
- Create: `src/skills/feedbacks/SKILL.md`

This is the core of the integration. The wrapper is a **complete replacement** — it contains all the base skill content plus ticket-takeaway additions.

- [ ] **Step 1: Create the wrapper skill**

Create `src/skills/feedbacks/SKILL.md` with the following content. The skill has the same three modes (setup, start, analyze) as the base, plus ticket-takeaway context awareness:

```markdown
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

   **Screen:** {what app/page is shown — e.g., "YouTube video player", "Settings > Billing page", "VS Code editor with server.py open"}
   **URL:** {visible URL from browser address bar, or "not visible" if browser chrome is offscreen}
   **Page title:** {tab title or page heading if readable}
   **Cursor:** {where the cursor is — e.g., "hovering over the Subscribe button", "in the search input field", "not visible"}
   **Marker {N}:** {what the marker is pointing at — e.g., "the 'Save' button in the toolbar", "the third pricing card", "a validation error message below the email field"}
   **Interaction:** {what the user did — click, drag-select, hover. Derive from marker type: red circle = click, red rectangle = drag selection}
   **Visible state:** {anything notable about the current UI state — e.g., "modal is open", "dropdown is expanded", "form has validation errors", "loading spinner visible", "dark mode active"}

   **User said:** "{transcript text}"

   **Interpretation:** {one sentence combining what the user pointed at with what they said — e.g., "User clicked the Save button and noted it doesn't provide visual feedback on success"}
   ```

   **Field rules:**
   - **Screen**: Identify the app from visual cues (favicon, logo, URL, layout). Be specific: "Stripe Dashboard > Customers list" not just "a dashboard".
   - **URL**: Read it literally from the address bar. Include query params if visible. Write "not visible" if the address bar is cropped or offscreen — don't guess.
   - **Cursor**: Describe position relative to UI elements, not pixel coordinates. "On the dropdown arrow next to the user avatar" is useful. "(450, 320)" is not.
   - **Marker**: Describe what the marker is *on top of*, not the marker itself. "The red 'Delete' button" not "a red circle".
   - **Visible state**: Only note what's relevant. A normal page load needs no comment. An error toast, a half-loaded spinner, a disabled button — those matter.
   - **Interpretation**: This is the key output. Fuse the visual evidence (marker position, UI state) with the verbal evidence (transcript). One clear sentence.

   If a screenshot has no marker and no transcript (auto-captured context frame), describe it briefly:
   ```
   ### Screenshot N · {timestamp}
   **Screen:** {app/page}
   **Context frame** — no user interaction. {Brief note of what's visible, e.g., "Page fully loaded, no errors."}
   ```

7. After presenting all sections, provide a **summary analysis**:
   - **Feedback points**: Each issue the user raised, with screenshot number, marker, and one-line description
   - **Navigation path**: The sequence of screens/pages the user visited (reconstructed from URLs and page titles across screenshots)
   - **UI/UX issues**: Problems visible in the screenshots that the user may or may not have mentioned
   - **Suggested action items**: Concrete fixes or investigations, each linked to a specific screenshot

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
```

- [ ] **Step 2: Verify the skill file exists and has correct frontmatter**

```bash
head -8 ~/projects/ticket-takeaway/src/skills/feedbacks/SKILL.md
```

Expected:
```
---
name: feedbacks
description: Screen+voice capture for visual feedback...
user_invocable: true
arguments:
  - name: command
    description: "Optional: 'setup', 'start', 'start {ticket-id}', 'analyze'...
    required: false
---
```

- [ ] **Step 3: Commit**

```bash
cd ~/projects/ticket-takeaway
git add src/skills/feedbacks/SKILL.md
git commit -m "feat: add feedbacks wrapper skill with ticket-takeaway context

Wrapper is a superset of the base feedbacks skill. Adds:
- Ticket-linked output dirs (/feedbacks start {ticket-id})
- Context push after analysis (session path + summary)"
```

---

### Task 4: Update `/review` skill to formalize feedbacks integration

**Repo:** `~/projects/ticket-takeaway`

**Files:**
- Modify: `src/skills/review/SKILL.md` (steps 4a and 1b)

The existing `/review` skill already documents feedbacks integration. This task formalizes the wording to reference the wrapper skill consistently.

- [ ] **Step 1: Update step 4a (presenting tickets)**

In `src/skills/review/SKILL.md`, find this block (lines 57-61):

```markdown
**Check for feedbacks sessions** (if feedbacks is installed):
- Look for `.feedbacks/{ticket-id}/` in the project root
- If session directories exist, run `/feedbacks analyze` on the latest one
- Present the analysis as additional review context alongside the ticket details
- If no `.feedbacks/` directory or feedbacks not installed, skip silently
```

Replace with:

```markdown
**Check for feedbacks sessions:**
```bash
ls -dt {project_root}/.feedbacks/{ticket-id}/feedbacks-*/session.md 2>/dev/null | head -1
```
- If a session directory is found, invoke `/feedbacks analyze {session_path}` on the latest one
- Present the analysis as additional review context alongside the ticket details
- If no `.feedbacks/{ticket-id}/` directory exists, skip silently — do not mention feedbacks
```

- [ ] **Step 2: Update step 1b (offering visual feedback)**

In `src/skills/review/SKILL.md`, find this block (lines 128-141):

```markdown
### 1b. Offer Visual Feedback Capture (Optional)

Check if feedbacks is available:
```bash
ls /home/user/projects/feedbacks/start.sh 2>/dev/null
```

If available **and the user hasn't already provided a detailed description**, offer:
> "Want to record visual feedback with `/feedbacks`? You can point at the UI and narrate the issue."

- If yes: invoke `/feedbacks start` — the feedbacks skill auto-detects the ticket context and saves to `.feedbacks/{ticket-id}/`
- When the user returns after their session, run `/feedbacks analyze` on the latest session in `.feedbacks/{ticket-id}/`
- Use the analysis findings (screenshots, marker references, action items) to enrich the bug sub-ticket description and acceptance criteria in step 2
- If feedbacks is not installed, skip this step entirely — do not mention it
```

Replace with:

```markdown
### 1b. Offer Visual Feedback Capture (Optional)

Check if feedbacks is available:
```bash
ls /home/user/projects/feedbacks/start.sh 2>/dev/null
```

If not found, skip this step entirely — do not mention feedbacks.

If available **and the user hasn't already provided a detailed description**, offer:
> "Want to record visual feedback? You can point at the UI and narrate the issue."

If yes:
1. Invoke `/feedbacks start {ticket-id}` — this sets `FEEDBACKS_OUTPUT_DIR={project_root}/.feedbacks/{ticket-id}/` and opens the capture app with the ticket pre-filled
2. Wait for the user to return after their capture session
3. Invoke `/feedbacks analyze` on the latest session in `.feedbacks/{ticket-id}/`
4. Use the analysis findings (screenshots, action items, interpretation notes) to enrich the bug sub-ticket:
   - Add specific UI references from the analysis to the bug description
   - Derive acceptance criteria from the suggested action items
   - Reference screenshot numbers in the description for traceability
```

- [ ] **Step 3: Verify the changes**

```bash
grep -n "feedbacks" ~/projects/ticket-takeaway/src/skills/review/SKILL.md
```

Expected: References to feedbacks at step 4a and step 1b, using `/feedbacks start {ticket-id}` and `/feedbacks analyze` invocations.

- [ ] **Step 4: Commit**

```bash
cd ~/projects/ticket-takeaway
git add src/skills/review/SKILL.md
git commit -m "feat: formalize feedbacks integration in /review skill

Step 4a checks .feedbacks/{ticket-id}/ for prior sessions.
Step 1b offers /feedbacks start {ticket-id} for visual capture."
```

---

### Task 5: Update install.py to deploy the wrapper skill

**Repo:** `~/projects/ticket-takeaway`

**Files:**
- Modify: `install.py` (the skills list in `install_system_files()`)

- [ ] **Step 1: Add feedbacks to the skills deployment list**

In `install.py`, find this block (around line 36):

```python
    # Skills
    skills = [
        ("ticket-takeaway", "src/skills/ticket-takeaway/SKILL.md"),
        ("review", "src/skills/review/SKILL.md"),
        ("spec", "src/skills/spec/SKILL.md"),
        ("accept", "src/skills/accept/SKILL.md"),
    ]
```

Replace with:

```python
    # Skills
    skills = [
        ("ticket-takeaway", "src/skills/ticket-takeaway/SKILL.md"),
        ("review", "src/skills/review/SKILL.md"),
        ("spec", "src/skills/spec/SKILL.md"),
        ("accept", "src/skills/accept/SKILL.md"),
        ("feedbacks", "src/skills/feedbacks/SKILL.md"),
    ]
```

- [ ] **Step 2: Verify the change**

```bash
grep -A 7 "# Skills" ~/projects/ticket-takeaway/install.py | head -10
```

Expected: The skills list now includes `("feedbacks", "src/skills/feedbacks/SKILL.md")` as the last entry.

- [ ] **Step 3: Test the installer**

```bash
cd ~/projects/ticket-takeaway
python3 install.py 2>&1 | grep -i feedbacks
```

Expected: Output includes `Skill: /feedbacks: /home/user/.claude/skills/feedbacks/SKILL.md`

- [ ] **Step 4: Verify the deployed skill is the wrapper (not the old base)**

```bash
head -3 ~/.claude/skills/feedbacks/SKILL.md
```

Expected: The wrapper's description mentioning "ticket-takeaway" context, not the old base description.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/ticket-takeaway
git add install.py
git commit -m "feat: deploy feedbacks wrapper skill via installer"
```

---

### Task 6: Update INSTALL.md deployment map

**Repo:** `~/projects/ticket-takeaway`

**Files:**
- Modify: `INSTALL.md` (deployment map table)

- [ ] **Step 1: Add feedbacks to the deployment map**

In `INSTALL.md`, find the skills section of the deployment map table (around lines 46-49):

```markdown
| `src/skills/spec/SKILL.md` | `~/.claude/skills/spec/SKILL.md` | `/spec` skill |
| `src/skills/accept/SKILL.md` | `~/.claude/skills/accept/SKILL.md` | `/accept` skill |
```

After the accept row, add:

```markdown
| `src/skills/feedbacks/SKILL.md` | `~/.claude/skills/feedbacks/SKILL.md` | `/feedbacks` wrapper skill (superset of base feedbacks skill) |
```

- [ ] **Step 2: Verify the table**

```bash
grep feedbacks ~/projects/ticket-takeaway/INSTALL.md
```

Expected: One row in the deployment map with `src/skills/feedbacks/SKILL.md` → `~/.claude/skills/feedbacks/SKILL.md`.

- [ ] **Step 3: Commit**

```bash
cd ~/projects/ticket-takeaway
git add INSTALL.md
git commit -m "docs: add feedbacks skill to deployment map"
```

---

### Task 7: End-to-end verification

Verify the full integration works as expected.

- [ ] **Step 1: Verify feedbacks repo has the base skill**

```bash
ls ~/projects/feedbacks/skills/feedbacks/SKILL.md
head -3 ~/projects/feedbacks/skills/feedbacks/SKILL.md
```

Expected: File exists, frontmatter shows `name: feedbacks`.

- [ ] **Step 2: Verify ticket-takeaway deployed the wrapper**

```bash
head -3 ~/.claude/skills/feedbacks/SKILL.md
```

Expected: The wrapper description mentioning "Screen+voice capture" and "ticket-takeaway".

- [ ] **Step 3: Verify `/review` references feedbacks correctly**

```bash
grep -c "feedbacks" ~/projects/ticket-takeaway/src/skills/review/SKILL.md
```

Expected: Multiple matches (step 4a and step 1b).

- [ ] **Step 4: Verify install.py deploys all skills**

```bash
cd ~/projects/ticket-takeaway && python3 install.py 2>&1 | grep "Skill:"
```

Expected: Five skills listed including `/feedbacks`.

- [ ] **Step 5: Test the auto-detect flow**

Run `/feedbacks` from the ticket-takeaway project. Since feedbacks is installed and whisper is built, it should detect the running state and either start the server or go to analyze mode. Verify it doesn't crash or show "not installed."

- [ ] **Step 6: Test ticket-linked start**

Run `/feedbacks start B-01` from a project with a `PRODUCT_BACKLOG.md`. Verify the output mentions:
- `FEEDBACKS_OUTPUT_DIR` set to `.feedbacks/B-01/`
- URL includes `?ticket=B-01`
