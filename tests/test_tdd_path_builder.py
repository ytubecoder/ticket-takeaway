import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_navigate_to_board_produces_open_step():
    from journeys import build_steps_from_path

    path = [{"screen": "Board", "interaction": None}]
    steps = build_steps_from_path(path, "user")
    assert len(steps) >= 1
    assert steps[0]["action"] == "open"
    assert steps[0]["value"] == ""


def test_navigate_to_settings_produces_open_step():
    from journeys import build_steps_from_path

    path = [{"screen": "Settings", "interaction": None}]
    steps = build_steps_from_path(path, "user")
    assert steps[0]["action"] == "open"
    assert steps[0]["value"] == "/settings"


def test_click_interaction_produces_click_step():
    from journeys import build_steps_from_path

    path = [
        {"screen": "Board", "interaction": None},
        {
            "screen": "Board",
            "interaction": {
                "type": "button",
                "testid": "new-ticket-btn",
                "name": "New",
            },
        },
    ]
    steps = build_steps_from_path(path, "user")
    click_steps = [s for s in steps if s["action"] == "click"]
    assert len(click_steps) == 1
    assert click_steps[0]["target"]["testid"] == "new-ticket-btn"


def test_fill_interaction_produces_fill_step():
    from journeys import build_steps_from_path

    path = [
        {"screen": "Board", "interaction": None},
        {
            "screen": "Board",
            "interaction": {
                "type": "text-input",
                "testid": "search-input",
                "name": "Search",
                "fill_value": "test query",
            },
        },
    ]
    steps = build_steps_from_path(path, "user")
    fill_steps = [s for s in steps if s["action"] == "fill"]
    assert len(fill_steps) == 1
    assert fill_steps[0]["target"]["testid"] == "search-input"
    assert fill_steps[0]["value"] == "test query"


def test_screen_change_inserts_auto_capture():
    from journeys import build_steps_from_path

    path = [
        {"screen": "Board", "interaction": None},
        {"screen": "Settings", "interaction": None},
    ]
    steps = build_steps_from_path(path, "user")
    captures = [s for s in steps if s["action"] == "capture"]
    assert len(captures) >= 1


def test_explicit_screenshot_step():
    from journeys import build_steps_from_path

    path = [
        {"screen": "Board", "interaction": None},
        {
            "screen": "Board",
            "interaction": {"type": "screenshot", "name": "Board overview"},
        },
    ]
    steps = build_steps_from_path(path, "user")
    captures = [s for s in steps if s["action"] == "capture"]
    assert len(captures) >= 1


def test_navigation_click_opens_target_screen():
    from journeys import build_steps_from_path

    path = [
        {"screen": "Board", "interaction": None},
        {
            "screen": "Board",
            "interaction": {
                "type": "button",
                "testid": "settings-toggle",
                "name": "Settings",
                "navigates_to": "Settings",
            },
        },
    ]
    steps = build_steps_from_path(path, "user")
    actions = [s["action"] for s in steps]
    assert "click" in actions
    assert actions.count("open") == 1  # only the initial board open


def test_all_steps_have_actor():
    from journeys import build_steps_from_path

    path = [
        {"screen": "Board", "interaction": None},
        {
            "screen": "Board",
            "interaction": {
                "type": "button",
                "testid": "new-ticket-btn",
                "name": "New",
            },
        },
    ]
    steps = build_steps_from_path(path, "reviewer")
    for step in steps:
        assert step["actor"] == "reviewer"
