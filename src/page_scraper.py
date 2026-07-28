"""
Page scraper module — discovers interactive elements on dashboard pages via Playwright.

Pure functions (classify_element, derive_screen_name, derive_element_name,
infer_is_navigation, scans_to_json) are TDD-tested. Playwright functions
(scrape_page, scan_all_screens) require a running server for integration testing.
"""

from __future__ import annotations

import base64
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PageElement:
    tag: str  # button, a, input, select, etc.
    testid: str  # data-testid value if present
    text: str  # visible text / label
    role: str  # aria role or inferred role
    element_type: str  # "button", "link", "text-input", "select", "checkbox"
    name: str  # human-readable name
    css_selector: str  # fallback selector
    is_navigation: bool  # True if clicking likely changes screen


@dataclass
class PageScan:
    url: str
    title: str
    screen_name: str  # derived from URL path
    elements: list[PageElement]
    screenshot_base64: str
    scanned_at: str


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

# Input types that map to text-input
_TEXT_INPUT_TYPES = frozenset(
    {
        "text",
        "search",
        "email",
        "password",
        "url",
        "tel",
        "number",
        "date",
        "datetime-local",
        "month",
        "week",
        "time",
        "color",
        "",
    }
)

# Input types that map to checkbox
_CHECK_INPUT_TYPES = frozenset({"checkbox", "radio"})

# Navigation testids
_NAV_TESTIDS = frozenset(
    {
        "settings-toggle",
        "journeys-btn",
        "journeys-back",
        "settings-back",
        "detail-close",
    }
)

# Navigation text patterns (case-insensitive)
_NAV_TEXT_PATTERNS = re.compile(r"^(back|board|settings|journeys|home)$", re.IGNORECASE)


def classify_element(tag: str, input_type: str, role: str, href: str) -> str:
    """Return element_type from tag/input_type/role/href."""
    # Role overrides
    if role == "button":
        return "button"
    if role == "link":
        return "link"

    tag_lower = tag.lower()

    if tag_lower == "button":
        return "button"
    if tag_lower == "a":
        return "link" if href else "button"
    if tag_lower == "select":
        return "select"
    if tag_lower == "textarea":
        return "text-input"
    if tag_lower == "input":
        it = (input_type or "").lower()
        if it in _CHECK_INPUT_TYPES:
            return "checkbox"
        return "text-input"

    # Fallback for div/span with onclick etc.
    return "button"


def derive_screen_name(path: str, page_title: str) -> str:
    """Derive a human-readable screen name from URL path."""
    # Strip trailing slash for matching
    p = path.rstrip("/")

    # Bare root → Project Picker
    if p == "" or p == "/":
        return "Project Picker"

    # Get the last segment
    segments = [s for s in p.split("/") if s]
    last = segments[-1] if segments else ""

    # Remove .html extension
    last_clean = re.sub(r"\.html$", "", last, flags=re.IGNORECASE)

    mapping = {
        "settings": "Settings",
        "journeys": "Journeys",
        "index": "Board",
    }

    if last_clean.lower() in mapping:
        return mapping[last_clean.lower()]

    # If there's exactly one segment (project root: /proj-name), it's the Board
    if len(segments) == 1:
        return "Board"

    # Fallback to page title
    return page_title


def derive_element_name(
    testid: str, text: str, placeholder: str, title: str, aria_label: str
) -> str:
    """Derive a human-readable element name. Priority: aria_label > text (<60) > title > placeholder > testid (humanized)."""
    if aria_label:
        return aria_label
    if text and len(text) < 60:
        return text
    if title:
        return title
    if placeholder:
        return placeholder
    if testid:
        # Humanize: "new-ticket-btn" → "New ticket btn"
        return testid.replace("-", " ").replace("_", " ").capitalize()
    return "Unknown element"


def infer_is_navigation(tag: str, href: str, text: str, testid: str) -> bool:
    """Whether clicking this element likely navigates to a different screen."""
    # Known navigation testids
    if testid in _NAV_TESTIDS:
        return True

    # Navigation text patterns
    if text and _NAV_TEXT_PATTERNS.match(text.strip()):
        return True

    # Links with real hrefs (not hash or javascript:)
    if tag.lower() == "a" and href:
        return not (href.startswith(("#", "javascript:")))

    return False


def scans_to_json(scans: list[PageScan]) -> list[dict[str, Any]]:
    """Serialize a list of PageScan to JSON-compatible dicts."""
    result = []
    for scan in scans:
        d = asdict(scan)
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Playwright functions (integration — not TDD-tested)
# ---------------------------------------------------------------------------

_INTERACTIVE_SELECTOR = (
    "button, a[href], input, select, textarea, "
    "[role='button'], [onclick], [data-testid]"
)

_EXTRACT_JS = f"""
() => {{
    const sel = `{_INTERACTIVE_SELECTOR}`;
    const els = document.querySelectorAll(sel);
    const results = [];
    for (const el of els) {{
        // Skip hidden elements
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;

        const tag = el.tagName.toLowerCase();
        const testid = el.getAttribute('data-testid') || '';
        const text = (el.innerText || '').trim().substring(0, 200);
        const role = el.getAttribute('role') || '';
        const inputType = el.getAttribute('type') || '';
        const href = el.getAttribute('href') || '';
        const placeholder = el.getAttribute('placeholder') || '';
        const title = el.getAttribute('title') || '';
        const ariaLabel = el.getAttribute('aria-label') || '';

        // Build a CSS selector
        let css = tag;
        if (el.id) css = tag + '#' + el.id;
        else if (testid) css = tag + '[data-testid="' + testid + '"]';
        else if (el.className && typeof el.className === 'string') {{
            const cls = el.className.trim().split(/\\s+/).slice(0, 2).join('.');
            if (cls) css = tag + '.' + cls;
        }}

        results.push({{
            tag, testid, text, role, inputType, href, placeholder, title, ariaLabel, css
        }});
    }}
    return results;
}}
"""


async def scrape_page(page, url: str) -> PageScan:
    """Navigate to URL and extract all interactive elements + screenshot."""
    await page.goto(url, wait_until="networkidle")
    await page.wait_for_timeout(500)  # let JS settle

    title = await page.title()
    path = url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url
    path = "/" + path if not path.startswith("/") else path

    raw_elements = await page.evaluate(_EXTRACT_JS)

    elements = []
    for raw in raw_elements:
        etype = classify_element(raw["tag"], raw["inputType"], raw["role"], raw["href"])
        ename = derive_element_name(
            raw["testid"],
            raw["text"],
            raw["placeholder"],
            raw["title"],
            raw["ariaLabel"],
        )
        is_nav = infer_is_navigation(
            raw["tag"], raw["href"], raw["text"], raw["testid"]
        )

        elements.append(
            PageElement(
                tag=raw["tag"],
                testid=raw["testid"],
                text=raw["text"],
                role=raw["role"],
                element_type=etype,
                name=ename,
                css_selector=raw["css"],
                is_navigation=is_nav,
            )
        )

    # Screenshot
    screenshot_bytes = await page.screenshot(full_page=True)
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

    screen_name = derive_screen_name(path, title)

    return PageScan(
        url=url,
        title=title,
        screen_name=screen_name,
        elements=elements,
        screenshot_base64=screenshot_b64,
        scanned_at=datetime.now(timezone.utc).isoformat(),
    )


async def scan_all_screens(base_url: str, browser) -> list[PageScan]:
    """Scan Board, Settings, and Detail Overlay screens."""
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()

    scans = []

    # Board
    board_url = base_url.rstrip("/") + "/"
    board_scan = await scrape_page(page, board_url)
    scans.append(board_scan)

    # Settings
    settings_url = base_url.rstrip("/") + "/settings"
    settings_scan = await scrape_page(page, settings_url)
    scans.append(settings_scan)

    # Detail Overlay — go back to board, click first card's open button
    await page.goto(board_url, wait_until="networkidle")
    await page.wait_for_timeout(500)
    open_btn = page.locator("[data-testid='card-open-btn']").first
    if await open_btn.count() > 0:
        await open_btn.click()
        await page.wait_for_timeout(500)
        detail_scan = await scrape_page(page, board_url + "#detail")
        detail_scan.screen_name = "Detail Overlay"
        scans.append(detail_scan)

    await context.close()
    return scans
