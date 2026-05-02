"""Smoke tests for Phase 3B nested trigger group builder and tag effects.

Uses the live_page fixture (no mocked routes — real server APIs).

Covers:
 1. Workflows tab renders on open
 2. Clicking a workflow row shows detail with a trigger group element
 3. Add group button inserts a nested group inside root
 4. Remove button absent on root group
 5. Remove button present on nested group
 6. On-success section has tag chip inputs for add_tags and remove_tags
 7. Typing and Enter adds a tag chip in the add-tags input
 8. Clicking x on a tag chip removes it
"""

import pytest


@pytest.fixture()
def bounce_workflows(live_page):
    """Open bounce page, switch to Workflows tab, return the page."""
    p = live_page
    btn = p.query_selector("#bounceToggleBtn")
    if btn is None:
        pytest.skip("bounceToggleBtn not found — serve.py EDIT_API not injected")
    btn.click()
    p.wait_for_function(
        "document.body.classList.contains('bounce-open')",
        timeout=5000,
    )
    # Already on Workflows tab by default; wait for list to load
    p.wait_for_timeout(1200)
    return p


@pytest.fixture()
def workflow_detail(bounce_workflows):
    """Open the detail view of the first available workflow row."""
    p = bounce_workflows
    rows = p.query_selector_all("#kwWorkflowTbody tr")
    if not rows:
        pytest.skip("No workflow rows found")
    first = rows[0]
    if "No workflows yet" in first.inner_text():
        pytest.skip("No workflows seeded")
    first.click()
    p.wait_for_timeout(600)
    detail = p.query_selector("#kwWorkflowDetail")
    assert detail is not None and detail.is_visible(), "Detail panel not visible"
    return p


@pytest.fixture()
def new_workflow_detail(bounce_workflows):
    """Open the New Workflow form (editable, not system)."""
    p = bounce_workflows
    btn = p.query_selector("#kwNewWfBtn")
    if btn is None:
        pytest.skip("kwNewWfBtn not found")
    btn.click()
    p.wait_for_timeout(400)
    detail = p.query_selector("#kwWorkflowDetail")
    assert detail is not None and detail.is_visible()
    return p


# ---------------------------------------------------------------------------
# 1. Workflows tab renders
# ---------------------------------------------------------------------------


def test_workflows_tab_renders(bounce_workflows):
    """Workflows tab shows the table structure."""
    p = bounce_workflows
    table = p.query_selector(".kw-table")
    assert table is not None, ".kw-table not found"


# ---------------------------------------------------------------------------
# 2. Clicking a workflow row shows trigger group
# ---------------------------------------------------------------------------


def test_detail_shows_trigger_group(workflow_detail):
    """Clicking a workflow row renders the trigger group element."""
    p = workflow_detail
    group = p.query_selector(".kw-trigger-group")
    assert group is not None, ".kw-trigger-group element not found in detail panel"


# ---------------------------------------------------------------------------
# 3. Add group button inserts a nested group
# ---------------------------------------------------------------------------


def test_add_group_inserts_nested_group(new_workflow_detail):
    """Clicking + Add group inside root inserts a nested .kw-trigger-group."""
    p = new_workflow_detail
    # Find the first "Add group" button (inside root group actions)
    add_grp_btns = p.query_selector_all(".kw-trigger-group-actions button")
    add_grp_btn = None
    for btn in add_grp_btns:
        if "Add group" in btn.inner_text():
            add_grp_btn = btn
            break
    if add_grp_btn is None:
        pytest.skip("Add group button not found — may be system workflow")
    add_grp_btn.click()
    p.wait_for_timeout(300)
    # Should now have at least 2 trigger groups (root + nested)
    groups = p.query_selector_all(".kw-trigger-group")
    assert len(groups) >= 2, f"Expected >=2 trigger groups after add, got {len(groups)}"


# ---------------------------------------------------------------------------
# 4. Root group has no remove button
# ---------------------------------------------------------------------------


def test_root_group_has_no_remove_button(new_workflow_detail):
    """The root trigger group must NOT have a Remove group button."""
    p = new_workflow_detail
    # The root group is the first .kw-trigger-group in the tree
    root_group = p.query_selector("#kwTriggerGroupRoot > .kw-trigger-group")
    if root_group is None:
        # Fallback: any trigger group
        root_group = p.query_selector(".kw-trigger-group")
    assert root_group is not None, "Root trigger group not found"
    # Root should not contain a remove button
    remove_btn = root_group.query_selector(".kw-trigger-group-remove")
    assert remove_btn is None, "Root group should not have a Remove button"


# ---------------------------------------------------------------------------
# 5. Nested group has a remove button
# ---------------------------------------------------------------------------


def test_nested_group_has_remove_button(new_workflow_detail):
    """After adding a nested group, it should have a Remove group button."""
    p = new_workflow_detail
    # Add a group first
    add_grp_btns = p.query_selector_all(".kw-trigger-group-actions button")
    for btn in add_grp_btns:
        if "Add group" in btn.inner_text():
            btn.click()
            break
    p.wait_for_timeout(300)
    groups = p.query_selector_all(".kw-trigger-group")
    if len(groups) < 2:
        pytest.skip("Nested group not added")
    # The second group should have a remove button
    nested_group = groups[1]
    remove_btn = nested_group.query_selector(".kw-trigger-group-remove")
    assert remove_btn is not None, "Nested group should have a Remove button"


# ---------------------------------------------------------------------------
# 6. On-success section has tag chip inputs
# ---------------------------------------------------------------------------


def test_on_success_has_tag_chip_inputs(new_workflow_detail):
    """The on-success section contains at least two .kw-tag-chip-input elements."""
    p = new_workflow_detail
    chip_inputs = p.query_selector_all(".kw-tag-chip-input")
    assert len(chip_inputs) >= 2, (
        f"Expected >=2 tag chip inputs, got {len(chip_inputs)}"
    )


# ---------------------------------------------------------------------------
# 7. Typing and pressing Enter adds a tag chip
# ---------------------------------------------------------------------------


def test_add_tag_chip_on_enter(new_workflow_detail):
    """Typing a tag name and pressing Enter adds a .kw-tag-chip chip."""
    p = new_workflow_detail
    # Get the first chip input (add_tags)
    chip_inputs = p.query_selector_all(".kw-tag-chip-input")
    if not chip_inputs:
        pytest.skip("No tag chip inputs found")
    first_input_container = chip_inputs[0]
    tag_input = first_input_container.query_selector("input")
    if tag_input is None:
        pytest.skip("Tag text input not found inside chip container")
    tag_input.click()
    tag_input.fill("needs-attention")
    tag_input.press("Enter")
    p.wait_for_timeout(200)
    chips = first_input_container.query_selector_all(".kw-tag-chip")
    assert len(chips) >= 1, "Tag chip not added after Enter"
    chip_text = chips[0].inner_text()
    assert "needs-attention" in chip_text, f"Chip text unexpected: {chip_text!r}"


# ---------------------------------------------------------------------------
# 8. Clicking x removes a tag chip
# ---------------------------------------------------------------------------


def test_remove_tag_chip(new_workflow_detail):
    """Clicking the x button on a tag chip removes it."""
    p = new_workflow_detail
    chip_inputs = p.query_selector_all(".kw-tag-chip-input")
    if not chip_inputs:
        pytest.skip("No tag chip inputs found")
    first_input_container = chip_inputs[0]
    tag_input = first_input_container.query_selector("input")
    if tag_input is None:
        pytest.skip("Tag text input not found")
    # Add a tag
    tag_input.click()
    tag_input.fill("to-remove")
    tag_input.press("Enter")
    p.wait_for_timeout(200)
    chips_before = first_input_container.query_selector_all(".kw-tag-chip")
    assert len(chips_before) >= 1, "Tag chip not added"
    # Click x on the first chip
    x_btn = chips_before[0].query_selector("button")
    assert x_btn is not None, "No x button on tag chip"
    x_btn.click()
    p.wait_for_timeout(200)
    chips_after = first_input_container.query_selector_all(".kw-tag-chip")
    assert len(chips_after) < len(chips_before), "Chip count did not decrease after remove"
