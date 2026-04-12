"""TDD tests for scenario Backend protocol (no live browser required)."""
from __future__ import annotations

import pytest


def test_backend_protocol_defines_required_methods():
    """Backend protocol must define all action methods."""
    from scenario_backend import Backend

    required = [
        "navigate", "reload", "click", "dblclick", "fill", "select", "press",
        "wait_for_visible", "wait_for_hidden", "wait_for_text",
        "screenshot", "wait_for_settled", "evaluate", "get_text", "close",
    ]
    for name in required:
        assert hasattr(Backend, name), f"Backend missing method: {name}"
