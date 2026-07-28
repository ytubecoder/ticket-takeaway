"""TDD tests for page_scraper pure functions."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_page_element_fields():
    from page_scraper import PageElement

    el = PageElement(
        tag="button",
        testid="new-ticket-btn",
        text="+ New",
        role="button",
        element_type="button",
        name="New ticket button",
        css_selector="button#newTicketBtn",
        is_navigation=False,
    )
    assert el.testid == "new-ticket-btn"
    assert el.element_type == "button"


def test_page_scan_fields():
    from page_scraper import PageScan

    scan = PageScan(
        url="http://localhost:8787/test/",
        title="Test Board",
        screen_name="Board",
        elements=[],
        screenshot_base64="",
        scanned_at="2026-04-12T00:00:00",
    )
    assert scan.screen_name == "Board"


# --- classify_element ---


def test_classify_element_button():
    from page_scraper import classify_element

    assert classify_element(tag="button", input_type="", role="", href="") == "button"


def test_classify_element_link():
    from page_scraper import classify_element

    assert classify_element(tag="a", input_type="", role="", href="/settings") == "link"


def test_classify_element_text_input():
    from page_scraper import classify_element

    assert (
        classify_element(tag="input", input_type="text", role="", href="")
        == "text-input"
    )


def test_classify_element_select():
    from page_scraper import classify_element

    assert classify_element(tag="select", input_type="", role="", href="") == "select"


def test_classify_element_checkbox():
    from page_scraper import classify_element

    assert (
        classify_element(tag="input", input_type="checkbox", role="", href="")
        == "checkbox"
    )


def test_classify_element_role_button():
    from page_scraper import classify_element

    assert (
        classify_element(tag="div", input_type="", role="button", href="") == "button"
    )


def test_classify_element_textarea():
    from page_scraper import classify_element

    assert (
        classify_element(tag="textarea", input_type="", role="", href="")
        == "text-input"
    )


def test_classify_element_input_email():
    from page_scraper import classify_element

    assert (
        classify_element(tag="input", input_type="email", role="", href="")
        == "text-input"
    )


def test_classify_element_input_search():
    from page_scraper import classify_element

    assert (
        classify_element(tag="input", input_type="search", role="", href="")
        == "text-input"
    )


def test_classify_element_input_radio():
    from page_scraper import classify_element

    assert (
        classify_element(tag="input", input_type="radio", role="", href="")
        == "checkbox"
    )


def test_classify_element_anchor_no_href():
    from page_scraper import classify_element

    assert classify_element(tag="a", input_type="", role="", href="") == "button"


def test_classify_element_div_role_link():
    from page_scraper import classify_element

    assert classify_element(tag="div", input_type="", role="link", href="") == "link"


# --- derive_screen_name ---


def test_derive_screen_name_root():
    from page_scraper import derive_screen_name

    assert derive_screen_name("/test-proj/", "Test — Dashboard") == "Board"


def test_derive_screen_name_settings():
    from page_scraper import derive_screen_name

    assert derive_screen_name("/test-proj/settings", "Settings") == "Settings"


def test_derive_screen_name_journeys():
    from page_scraper import derive_screen_name

    assert derive_screen_name("/test-proj/journeys", "Journeys") == "Journeys"


def test_derive_screen_name_picker():
    from page_scraper import derive_screen_name

    assert derive_screen_name("/", "Ticket Takeaway") == "Project Picker"


def test_derive_screen_name_unknown():
    from page_scraper import derive_screen_name

    assert derive_screen_name("/test-proj/unknown", "Some Page") == "Some Page"


def test_derive_screen_name_index_html():
    from page_scraper import derive_screen_name

    assert derive_screen_name("/proj/index.html", "Dashboard") == "Board"


# --- derive_element_name ---


def test_derive_element_name_from_testid():
    from page_scraper import derive_element_name

    assert (
        derive_element_name(
            testid="new-ticket-btn", text="", placeholder="", title="", aria_label=""
        )
        == "New ticket btn"
    )


def test_derive_element_name_from_text():
    from page_scraper import derive_element_name

    assert (
        derive_element_name(
            testid="", text="Save Changes", placeholder="", title="", aria_label=""
        )
        == "Save Changes"
    )


def test_derive_element_name_from_placeholder():
    from page_scraper import derive_element_name

    assert (
        derive_element_name(
            testid="", text="", placeholder="Search items...", title="", aria_label=""
        )
        == "Search items..."
    )


def test_derive_element_name_from_aria_label():
    from page_scraper import derive_element_name

    assert (
        derive_element_name(
            testid="x", text="y", placeholder="z", title="t", aria_label="Close dialog"
        )
        == "Close dialog"
    )


def test_derive_element_name_from_title():
    from page_scraper import derive_element_name

    assert (
        derive_element_name(
            testid="", text="", placeholder="", title="Toggle theme", aria_label=""
        )
        == "Toggle theme"
    )


def test_derive_element_name_long_text_falls_through():
    from page_scraper import derive_element_name

    long_text = "A" * 80
    assert (
        derive_element_name(
            testid="fallback-id",
            text=long_text,
            placeholder="",
            title="",
            aria_label="",
        )
        == "Fallback id"
    )


def test_derive_element_name_empty():
    from page_scraper import derive_element_name

    assert (
        derive_element_name(testid="", text="", placeholder="", title="", aria_label="")
        == "Unknown element"
    )


# --- infer_is_navigation ---


def test_infer_navigation_link():
    from page_scraper import infer_is_navigation

    assert infer_is_navigation(tag="a", href="/settings", text="", testid="") is True


def test_infer_navigation_back_button():
    from page_scraper import infer_is_navigation

    assert infer_is_navigation(tag="button", href="", text="Back", testid="") is True


def test_infer_navigation_settings_toggle():
    from page_scraper import infer_is_navigation

    assert (
        infer_is_navigation(tag="button", href="", text="", testid="settings-toggle")
        is True
    )


def test_infer_navigation_journeys_btn():
    from page_scraper import infer_is_navigation

    assert (
        infer_is_navigation(tag="button", href="", text="", testid="journeys-btn")
        is True
    )


def test_infer_navigation_regular_button():
    from page_scraper import infer_is_navigation

    assert (
        infer_is_navigation(tag="button", href="", text="Submit", testid="submit-btn")
        is False
    )


def test_infer_navigation_detail_close():
    from page_scraper import infer_is_navigation

    assert (
        infer_is_navigation(tag="button", href="", text="", testid="detail-close")
        is True
    )


def test_infer_navigation_journeys_back():
    from page_scraper import infer_is_navigation

    assert (
        infer_is_navigation(tag="button", href="", text="", testid="journeys-back")
        is True
    )


def test_infer_navigation_settings_back():
    from page_scraper import infer_is_navigation

    assert (
        infer_is_navigation(tag="button", href="", text="", testid="settings-back")
        is True
    )


def test_infer_navigation_text_home():
    from page_scraper import infer_is_navigation

    assert infer_is_navigation(tag="button", href="", text="Home", testid="") is True


def test_infer_navigation_hash_href():
    from page_scraper import infer_is_navigation

    assert infer_is_navigation(tag="a", href="#section", text="", testid="") is False


def test_infer_navigation_javascript_href():
    from page_scraper import infer_is_navigation

    assert (
        infer_is_navigation(tag="a", href="javascript:void(0)", text="", testid="")
        is False
    )


# --- scans_to_json ---


def test_scans_to_json_basic():
    from page_scraper import PageElement, PageScan, scans_to_json

    el = PageElement(
        tag="button",
        testid="btn",
        text="Click",
        role="button",
        element_type="button",
        name="Click",
        css_selector="button.btn",
        is_navigation=False,
    )
    scan = PageScan(
        url="http://localhost/",
        title="Board",
        screen_name="Board",
        elements=[el],
        screenshot_base64="abc123",
        scanned_at="2026-04-12T00:00:00",
    )
    result = scans_to_json([scan])
    assert len(result) == 1
    assert result[0]["screen_name"] == "Board"
    assert len(result[0]["elements"]) == 1
    assert result[0]["elements"][0]["tag"] == "button"
    assert result[0]["screenshot_base64"] == "abc123"


def test_scans_to_json_empty():
    from page_scraper import scans_to_json

    assert scans_to_json([]) == []
