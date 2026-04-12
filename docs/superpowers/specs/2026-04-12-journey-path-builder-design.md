# Journey Path Builder — Design Spec

## Problem

The journey system has a step editor that works at the HTML element level (action: click, target: testid). This is correct for CI automation but unusable for humans. Users want to define click paths by picking screens and interactions, not by knowing testid values.

Additionally, running a journey requires pytest + Playwright CLI knowledge, and screenshots aren't displayed in the UI.

## Solution

A **Path Builder** UI in the journeys page that:

1. Lets the user pick a screen from a list
2. Shows what's interactive on that screen (auto-discovered by a scraper)
3. User picks an interaction → it's added to the journey
4. System infers the next screen and offers its interactions
5. Running executes via headless Playwright and displays screenshots inline

## Architecture

### 1. Page Scraper (`src/page_scraper.py`)

A module that loads a page via Playwright and extracts all interactive elements.

**Input:** URL of a page served by serve.py  
**Output:** List of interactive elements with metadata

```python
@dataclass
class PageElement:
    tag: str              # button, a, input, select
    testid: str           # data-testid value if present
    text: str             # visible text / label
    role: str             # aria role or inferred role
    element_type: str     # "button", "link", "text-input", "select", "checkbox"
    name: str             # human-readable name derived from text/label/title/placeholder
    css_selector: str     # fallback selector
    is_navigation: bool   # True if clicking likely changes the screen

@dataclass  
class PageScan:
    url: str
    title: str
    screen_name: str      # derived from URL path
    elements: list[PageElement]
    screenshot_base64: str  # thumbnail of the page
    scanned_at: str
```

**Discovery logic:**
- Queries all `button`, `a[href]`, `input`, `select`, `textarea`, `[role="button"]`, `[onclick]`
- For each: extract `data-testid`, `textContent`, `title`, `placeholder`, `aria-label`
- Filter out hidden/invisible elements (`display:none`, `visibility:hidden`, zero dimensions)
- Classify as navigation if: `<a>` with href, or button with text like "Settings", "Back", "Board"
- Generate human-readable `name` from best available text

**Screen name derivation from URL:**
- `/` or `/index.html` → "Board"
- `/settings` → "Settings"  
- `/journeys` → "Journeys"
- Unknown → use page `<title>`

### 2. Screen Registry (API + cache)

`GET /{pid}/api/screens` — returns known screens with their interactive elements.

On first call (or when cache is stale), the server:
1. Spawns Playwright headless
2. Navigates to each known route (`/`, `/settings`, `/journeys`)
3. Runs the scraper on each
4. Caches the result (in-memory, invalidated on regenerate)

Returns:
```json
{
  "screens": [
    {
      "id": "board",
      "name": "Board",
      "url": "/",
      "elements": [
        {"name": "Settings button", "testid": "settings-toggle", "type": "button", "is_navigation": true},
        {"name": "New ticket button", "testid": "new-ticket-btn", "type": "button"},
        {"name": "Search input", "testid": "search-input", "type": "text-input"},
        ...
      ],
      "thumbnail": "data:image/png;base64,..."
    },
    ...
  ]
}
```

**Detail Overlay** is a special case — it's not a separate URL but appears when clicking a card. The scraper handles this by:
- Opening the board, clicking the first card to open the overlay
- Scanning the overlay's interactive elements
- Registering it as a virtual screen "Detail Overlay"

### 3. Path Builder UI

Replaces the current step editor as the **default** journey authoring mode. The existing element-level editor remains accessible via a "Raw Steps" toggle.

**Layout:**

```
[Screen Selector]  →  [Interaction Picker]  →  [Journey Path]

┌──────────────────┐  ┌────────────────────┐  ┌────────────────────┐
│ Pick a screen:   │  │ What to do:        │  │ Your path:         │
│                  │  │                    │  │                    │
│  ● Board      →  │  │  ○ Click "New"     │  │ 1. Board           │
│  ○ Settings      │  │  ○ Click "Settings"│  │    → screenshot    │
│  ○ Detail        │  │  ○ Search "test"   │  │ 2. Click "New"     │
│  ○ Journeys      │  │  ○ Screenshot      │  │ 3. Board           │
│                  │  │                    │  │    → screenshot    │
│  [Scan pages]    │  │  [+ Add to path]   │  │                    │
│                  │  │                    │  │ [▶ Run]            │
└──────────────────┘  └────────────────────┘  └────────────────────┘
```

**Flow:**
1. User clicks "Scan pages" (or auto-scans on first open) — calls `/api/screens`
2. Picks a screen → right panel shows its interactions grouped by type:
   - **Navigate** (buttons/links that change screen)
   - **Click** (buttons that perform actions on this screen)
   - **Fill** (inputs — shows a text field to enter value)
   - **Screenshot** (always available — captures this screen)
3. Picking an interaction + clicking "Add" appends to the path
4. If the interaction is navigational, the screen selector auto-advances to the target screen
5. "Screenshot" is offered at every screen — adds a capture step

**Path display:**
- Vertical list showing: screen name, interaction, and thumbnail previews for screenshot steps
- Drag to reorder
- X to remove steps
- Each step shows a small icon for its type (camera for screenshot, pointer for click, keyboard for fill)

### 4. Journey Runner with Screenshots

`POST /{pid}/api/journeys/{id}/run` — enhanced to:

1. Compile journey to manifest (existing)
2. Launch Playwright headless (existing infrastructure via scenario_runner)
3. Execute steps, capturing screenshots at every `capture` step
4. Store screenshots in `.artifacts/journeys/{journey_id}/{run_id}/`
5. Store screenshot paths in `journey_step_results.screenshot_path`
6. Return run results with screenshot URLs

**Screenshot serving:**
`GET /{pid}/api/journeys/{id}/runs/{run_id}/screenshots/{filename}` — serves captured PNGs.

**UI integration:**
- After a run completes, the journey detail view shows:
  - Pass/fail status per step (existing timeline)
  - Screenshot thumbnails inline below each capture step
  - Click thumbnail → full-size lightbox
- The Graph view shows real thumbnails on capture nodes (replacing the camera icon placeholder)

### 5. Auto-screenshot insertion

When the path builder adds a navigation step (moving to a new screen), it automatically inserts a `capture` step after. This means:
- "Go to Board" → Board + screenshot
- "Click Settings" → Settings + screenshot
- "Open ticket detail" → Detail Overlay + screenshot

The user sees "screenshot" as a natural part of visiting each screen, not something they have to manually add.

## Files to Create/Modify

| File | Change |
|------|--------|
| `src/page_scraper.py` (new) | PageElement, PageScan dataclasses, scrape_page(), scan_screens() |
| `src/serve.py` | Add `/api/screens` endpoint, enhance `/api/journeys/{id}/run` for screenshots, add screenshot serving endpoint, new path builder HTML/JS in `_render_journeys_page()` |
| `src/journeys.py` | Add `build_steps_from_path()` — converts screen-level path to element-level steps |

## What Does NOT Change

- The low-level step schema (journey_steps table) — path builder generates these underneath
- The scenario runner (tests/scenario_runner.py) — still used for execution
- The compile_to_manifest flow — path builder steps compile the same way
- pytest discovery — auto-exported JSON still works
- The "Raw Steps" editor — remains as an advanced toggle

## Verification

1. Open journeys page → click "New Journey" → see path builder (not raw step editor)
2. Click "Scan pages" → see Board, Settings, Detail Overlay with their interactions
3. Pick "Board" → pick "Click Settings button" → Add → auto-advances to Settings screen
4. Pick "Screenshot" → Add → run the journey
5. After run: see screenshot thumbnails inline in the journey detail
6. Toggle to "Graph" view → see thumbnail on the Settings node
7. Toggle to "Raw Steps" → see the generated element-level steps
