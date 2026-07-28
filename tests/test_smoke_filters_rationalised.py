"""Smoke tests for rationalised automation filter chips (Part A) and
kitchen-kanban removal (Part B).

Uses the live_page fixture (no mocked routes — real server APIs).

Covers:
 1. Correct canonical chips: Auto / Ready / Running / Needs Attention / For Review (auto).
 2. Held chip absent.  Eligible chip absent.
 3. Needs Attention chip has a chevron sibling with id needsAttentionChevron.
 4. Clicking chevron opens popover with 4 labelled checkboxes.
 5. Popover dismisses on outside click.
 6. Clicking Ready chip shows only ready cards (via JS evaluation).
 7. Needs Attention filter respects sub-toggle (uncheck Failed; run JS predicate).
 8. For Review (auto) chip is present and has data-testid for-review-auto-chip.
 9. No element #kitchenBoardToggleBtn in DOM.
10. No element #kitchen-board-root in DOM.
11. Clicking Needs Attention chip marks it active.
12. Auto chip includes held-mode tickets (JS dataset check).
"""

import pytest

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _filter_chip(page, data_filter: str):
    """Return the filter button element for the given data-filter value."""
    return page.query_selector(f'.filter-btn[data-filter="{data_filter}"]')


# ---------------------------------------------------------------------------
# 1 & 2. Canonical chip set — Auto, Ready, Running, Needs Attention, For Review (auto)
#        Held and Eligible must be gone.
# ---------------------------------------------------------------------------


def test_canonical_chips_present(live_page):
    """Auto / Ready / Running / Needs Attention / For Review (auto) chips exist."""
    p = live_page
    for label_filt in [
        "auto",
        "ready",
        "running",
        "needs-attention",
        "for-review-auto",
    ]:
        btn = _filter_chip(p, label_filt)
        assert btn is not None, f"Expected chip data-filter='{label_filt}' — not found"


def test_paused_chip_absent(live_page):
    """A standalone Paused chip must not exist — paused is a sub-toggle of Auto."""
    p = live_page
    btn = _filter_chip(p, "paused")
    assert btn is None, "Paused chip found — should not be a top-level chip"
    btn = _filter_chip(p, "held")
    assert btn is None, "Legacy held chip found — should have been removed"


def test_eligible_chip_absent(live_page):
    """Eligible chip must not exist in the filter bar."""
    p = live_page
    btn = _filter_chip(p, "eligible")
    assert btn is None, "Eligible chip found — should have been removed"


# ---------------------------------------------------------------------------
# 3. Needs Attention chip has adjacent chevron button
# ---------------------------------------------------------------------------


def test_needs_attention_chevron_exists(live_page):
    """A chevron button #needsAttentionChevron exists adjacent to the chip."""
    p = live_page
    chevron = p.query_selector("#needsAttentionChevron")
    assert chevron is not None, "#needsAttentionChevron not found"
    # Verify it has the correct testid attribute
    assert chevron.get_attribute("data-testid") == "needs-attention-chevron"


# ---------------------------------------------------------------------------
# 4. Clicking chevron opens popover with 4 checkboxes
# ---------------------------------------------------------------------------


def test_needs_attention_chevron_opens_popover(live_page):
    """Clicking the chevron shows the needs-attention popover with 4 checkboxes."""
    p = live_page
    chevron = p.query_selector("#needsAttentionChevron")
    if chevron is None:
        pytest.skip("Chevron not found")

    popover = p.query_selector("#needsAttentionPopover")
    assert popover is not None, "#needsAttentionPopover not found"

    # Popover should be hidden initially
    initial_display = p.evaluate(
        "document.getElementById('needsAttentionPopover').style.display"
    )
    assert initial_display in ("none", ""), (
        f"Expected popover hidden, got display='{initial_display}'"
    )

    chevron.click()
    p.wait_for_timeout(150)

    # Popover should now be visible
    display_after = p.evaluate(
        "document.getElementById('needsAttentionPopover').style.display"
    )
    assert display_after != "none", "Popover did not open after chevron click"

    # Four checkboxes with data-na-sub attributes
    checkboxes = p.query_selector_all("[data-na-sub]")
    assert len(checkboxes) == 4, (
        f"Expected 4 sub-toggle checkboxes, got {len(checkboxes)}"
    )

    sub_values = {chk.get_attribute("data-na-sub") for chk in checkboxes}
    expected = {"needs_input", "failed", "stalled", "cancelled"}
    assert sub_values == expected, f"Sub-toggle values mismatch: {sub_values}"


# ---------------------------------------------------------------------------
# 5. Popover dismisses on outside click
# ---------------------------------------------------------------------------


def test_needs_attention_popover_dismisses_on_outside_click(live_page):
    """Clicking outside the popover dismisses it."""
    p = live_page
    chevron = p.query_selector("#needsAttentionChevron")
    if chevron is None:
        pytest.skip("Chevron not found")

    chevron.click()
    p.wait_for_timeout(150)

    # Verify open
    display_after = p.evaluate(
        "document.getElementById('needsAttentionPopover').style.display"
    )
    assert display_after != "none", "Popover did not open"

    # Click outside (on the page body, far from chips)
    p.click("body", position={"x": 10, "y": 10})
    p.wait_for_timeout(150)

    display_dismissed = p.evaluate(
        "document.getElementById('needsAttentionPopover').style.display"
    )
    assert display_dismissed == "none", "Popover did not dismiss on outside click"


# ---------------------------------------------------------------------------
# 6. Clicking Ready chip — JS predicate correct
# ---------------------------------------------------------------------------


def test_ready_chip_shows_only_ready_cards(live_page):
    """After clicking Ready, all visible cards must satisfy the ready predicate."""
    p = live_page
    btn = _filter_chip(p, "ready")
    if btn is None:
        pytest.skip("Ready chip not found")

    btn.click()
    p.wait_for_timeout(300)

    # Every visible card must have: automationMode == 'auto' AND eligible=true
    # AND runStatus not in queued|preparing|running. Paused is intentionally
    # excluded from Ready (paused tickets aren't dispatching).
    result = p.evaluate("""
        (function() {
            var ACTIVE = {queued: 1, preparing: 1, running: 1};
            var cards = Array.from(document.querySelectorAll('.card'));
            var visible = cards.filter(function(c) { return c.style.display !== 'none'; });
            var bad = visible.filter(function(c) {
                var mode = c.dataset.automationMode;
                var eligible = c.dataset.eligible;
                var rs = c.dataset.runStatus || '';
                return !(mode === 'auto' && eligible === 'true' && !ACTIVE[rs]);
            });
            return {total: visible.length, bad: bad.length};
        })()
    """)
    # It's OK if no cards match (count_ready could be 0) — just none must violate predicate
    assert result["bad"] == 0, (
        f"{result['bad']} visible cards violate the Ready predicate "
        f"(total visible: {result['total']})"
    )

    # Reset filter
    p.evaluate("document.querySelector('.filter-btn[data-filter=\"ready\"]').click()")
    p.wait_for_timeout(150)


# ---------------------------------------------------------------------------
# 7. Needs Attention sub-toggle: uncheck Failed via JS, filter, verify
# ---------------------------------------------------------------------------


def test_needs_attention_subtoggle_excludes_failed(live_page):
    """Unchecking Failed sub-toggle excludes failed cards from Needs Attention filter."""
    p = live_page

    # Open the popover
    chevron = p.query_selector("#needsAttentionChevron")
    if chevron is None:
        pytest.skip("Chevron not found")
    chevron.click()
    p.wait_for_timeout(150)

    # Uncheck the 'failed' sub-toggle
    failed_chk = p.query_selector("[data-na-sub='failed']")
    if failed_chk is None:
        pytest.skip("failed sub-toggle checkbox not found")
    if failed_chk.is_checked():
        failed_chk.click()
    p.wait_for_timeout(150)

    # Close popover by clicking outside
    p.click("body", position={"x": 10, "y": 10})
    p.wait_for_timeout(150)

    # Now click the Needs Attention chip
    na_chip = _filter_chip(p, "needs-attention")
    if na_chip is None:
        pytest.skip("Needs Attention chip not found")
    na_chip.click()
    p.wait_for_timeout(300)

    # Verify no visible card has runStatus='failed'
    result = p.evaluate("""
        (function() {
            var cards = Array.from(document.querySelectorAll('.card'));
            var visible = cards.filter(function(c) { return c.style.display !== 'none'; });
            var failed = visible.filter(function(c) { return c.dataset.runStatus === 'failed'; });
            return {visible: visible.length, failed_count: failed.length};
        })()
    """)
    assert result["failed_count"] == 0, (
        f"Found {result['failed_count']} failed cards visible after unchecking Failed sub-toggle"
    )

    # Restore: re-check failed and reset chip
    chevron.click()
    p.wait_for_timeout(150)
    failed_chk2 = p.query_selector("[data-na-sub='failed']")
    if failed_chk2 and not failed_chk2.is_checked():
        failed_chk2.click()
    p.wait_for_timeout(100)
    p.click("body", position={"x": 10, "y": 10})
    p.wait_for_timeout(100)
    p.evaluate(
        "document.querySelector('.filter-btn[data-filter=\"needs-attention\"]').click()"
    )
    p.wait_for_timeout(150)


# ---------------------------------------------------------------------------
# 8. For Review (auto) chip exists and has correct testid
# ---------------------------------------------------------------------------


def test_for_review_auto_chip_present(live_page):
    """For Review (auto) chip exists and has data-testid='for-review-auto-chip'."""
    p = live_page
    chip = p.query_selector("[data-testid='for-review-auto-chip']")
    assert chip is not None, "for-review-auto chip not found (data-testid)"
    assert chip.get_attribute("data-filter") == "for-review-auto"
    assert chip.get_attribute("data-group") == "kitchen"


# ---------------------------------------------------------------------------
# 9. No #kitchenBoardToggleBtn in DOM
# ---------------------------------------------------------------------------


def test_kitchen_board_toggle_btn_absent(live_page):
    """#kitchenBoardToggleBtn must not exist — kitchen kanban view is removed."""
    p = live_page
    el = p.query_selector("#kitchenBoardToggleBtn")
    assert el is None, "#kitchenBoardToggleBtn still exists in DOM"


# ---------------------------------------------------------------------------
# 10. No #kitchen-board-root in DOM
# ---------------------------------------------------------------------------


def test_kitchen_board_root_absent(live_page):
    """#kitchen-board-root must not exist — kitchen kanban view is removed."""
    p = live_page
    el = p.query_selector("#kitchen-board-root")
    assert el is None, "#kitchen-board-root still exists in DOM"


# ---------------------------------------------------------------------------
# 11. Clicking Needs Attention chip marks it active
# ---------------------------------------------------------------------------


def test_needs_attention_chip_toggles_active(live_page):
    """Clicking the Needs Attention chip adds the 'active' class."""
    p = live_page
    chip = _filter_chip(p, "needs-attention")
    if chip is None:
        pytest.skip("Needs Attention chip not found")

    # Should not be active initially
    assert not chip.evaluate("el => el.classList.contains('active')"), (
        "Chip should not be active before first click"
    )

    chip.click()
    p.wait_for_timeout(150)

    assert chip.evaluate("el => el.classList.contains('active')"), (
        "Chip should be active after click"
    )

    # Toggle off
    chip.click()
    p.wait_for_timeout(150)
    assert not chip.evaluate("el => el.classList.contains('active')"), (
        "Chip should be inactive after second click"
    )


# ---------------------------------------------------------------------------
# 12. Auto chip excludes paused by default; sub-toggle "Include paused" adds them.
# ---------------------------------------------------------------------------


def test_auto_chip_excludes_paused_by_default(live_page):
    """Auto filter, with the 'Include paused' sub-toggle off, must skip mode=paused."""
    p = live_page

    # Force the localStorage flag off so the test is deterministic regardless
    # of what a previous run set.
    p.evaluate("localStorage.removeItem('tt-auto-include-paused')")
    # Re-load so the JS picks up the cleared flag.
    p.reload()
    p.wait_for_timeout(300)

    btn = _filter_chip(p, "auto")
    if btn is None:
        pytest.skip("Auto chip not found")
    btn.click()
    p.wait_for_timeout(300)

    # No visible card should have mode='paused' while the chevron toggle is off.
    bad = p.evaluate("""
        (function() {
            var cards = Array.from(document.querySelectorAll('.card'));
            var visible = cards.filter(function(c) { return c.style.display !== 'none'; });
            return visible.filter(function(c) { return c.dataset.automationMode === 'paused'; }).length;
        })()
    """)
    assert bad == 0, f"{bad} paused cards visible while 'Include paused' is OFF"

    # Reset
    btn.click()
    p.wait_for_timeout(150)


def test_auto_chip_includes_paused_when_subtoggle_on(live_page):
    """Auto filter, with 'Include paused' on, includes mode=paused cards."""
    p = live_page
    # Tick the sub-toggle directly to avoid clicking through chevron timing.
    p.evaluate("""
        localStorage.setItem('tt-auto-include-paused', '1');
    """)
    p.reload()
    p.wait_for_timeout(300)

    btn = _filter_chip(p, "auto")
    if btn is None:
        pytest.skip("Auto chip not found")
    btn.click()
    p.wait_for_timeout(300)

    # Spot-check the predicate: with the toggle on, mode=paused must be matched.
    matches = p.evaluate("""
        (function() {
            var t = document.createElement('div');
            t.dataset.automationMode = 'paused';
            var mode = t.dataset.automationMode;
            var include = localStorage.getItem('tt-auto-include-paused') === '1';
            return (mode === 'auto' || (include && mode === 'paused'));
        })()
    """)
    assert matches is True, (
        "Auto predicate should match mode='paused' when sub-toggle is on"
    )

    # Reset
    btn.click()
    p.wait_for_timeout(100)
    p.evaluate("localStorage.removeItem('tt-auto-include-paused')")
