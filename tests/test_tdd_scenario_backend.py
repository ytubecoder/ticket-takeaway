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


def test_resolve_target_css():
    """CSS targets should route to page.locator()."""
    from scenario_backend import resolve_target

    class FakePage:
        def locator(self, selector):
            return ("locator", selector)

    page = FakePage()
    result = resolve_target(page, {"css": ".my-class"}, {})
    assert result == ("locator", ".my-class")


def test_resolve_target_testid():
    """testid targets should route to get_by_test_id()."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_test_id(self, tid):
            return ("testid", tid)

    result = resolve_target(FakePage(), {"testid": "submit"}, {})
    assert result == ("testid", "submit")


def test_resolve_target_title_via_seed_map():
    """title targets should look up ticket id in seed_id_map."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_test_id(self, tid):
            return ("testid", tid)

    seed_map = {"My Ticket": "B-42"}
    result = resolve_target(FakePage(), {"title": "My Ticket"}, seed_map)
    assert result == ("testid", "ticket-card-B-42")


def test_resolve_target_title_with_open_flag():
    """title + open:true should resolve to the card open button."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_test_id(self, tid):
            return ("testid", tid)

    seed_map = {"My Ticket": "B-42"}
    result = resolve_target(
        FakePage(), {"title": "My Ticket", "open": True}, seed_map
    )
    assert result == ("testid", "card-open-btn-B-42")


def test_resolve_target_seed_ref():
    """seed_ref ticket-0 should index into seed map values."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_test_id(self, tid):
            return ("testid", tid)

    seed_map = {"First": "B-01", "Second": "B-02"}
    result = resolve_target(FakePage(), {"seed_ref": "ticket-1"}, seed_map)
    assert result == ("testid", "ticket-card-B-02")


def test_resolve_target_text():
    """text targets should use get_by_text()."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_text(self, text, exact):
            return ("text", text, exact)

    result = resolve_target(FakePage(), {"text": "Save"}, {})
    assert result == ("text", "Save", False)


def test_resolve_target_role():
    """role targets should use get_by_role()."""
    from scenario_backend import resolve_target

    class FakePage:
        def get_by_role(self, role, name):
            return ("role", role, name)

    result = resolve_target(
        FakePage(), {"role": "button", "name": "Cancel"}, {}
    )
    assert result == ("role", "button", "Cancel")


def test_resolve_target_unknown_raises():
    """Unknown target descriptors should raise ValueError."""
    from scenario_backend import resolve_target

    with pytest.raises(ValueError, match="Unrecognised target"):
        resolve_target(None, {"weird": "thing"}, {})


def test_resolve_target_title_missing_raises():
    """Missing title in seed map should raise ValueError."""
    from scenario_backend import resolve_target

    with pytest.raises(ValueError, match="not found in seed_id_map"):
        resolve_target(None, {"title": "Nope"}, {})


def test_playwright_backend_satisfies_protocol():
    """PlaywrightBackend must satisfy the Backend protocol."""
    from scenario_backend import Backend, PlaywrightBackend

    # Create a minimal fake page/context to instantiate
    class FakePage:
        def goto(self, url): pass
        def reload(self): pass
        def screenshot(self, path, full_page=False): pass
        def wait_for_function(self, *a, **kw): pass
        def wait_for_timeout(self, ms): pass

    class FakeCtx:
        def close(self): pass

    backend = PlaywrightBackend(page=FakePage(), context=FakeCtx())
    assert isinstance(backend, Backend)


def test_cdp_backend_satisfies_protocol():
    """CDPBackend must satisfy the Backend protocol."""
    from scenario_backend import Backend, CDPBackend

    class FakePage:
        def goto(self, url): pass
        def reload(self): pass
        def screenshot(self, path, full_page=False): pass

    class FakeCtx:
        def close(self): pass

    backend = CDPBackend(page=FakePage(), context=FakeCtx())
    assert isinstance(backend, Backend)


def test_connect_cdp_backend_raises_on_unreachable():
    """connect_cdp_backend should raise a clear error if no browser is listening."""
    from scenario_backend import connect_cdp_backend

    with pytest.raises(ConnectionError, match="9999"):
        connect_cdp_backend("http://localhost:9999", timeout_ms=500)


def test_scenario_context_creates_playwright_backend():
    """ScenarioContext should create PlaywrightBackend when backend='playwright'."""
    from scenario_backend import PlaywrightBackend
    from scenario_runner import ScenarioContext

    class FakePage:
        pass

    class FakeBrowserCtx:
        def new_page(self):
            return FakePage()
        def close(self):
            pass

    class FakeBrowser:
        def new_context(self):
            return FakeBrowserCtx()

    ctx = ScenarioContext(
        base_url="http://localhost:8000",
        browser=FakeBrowser(),
        output_dir="/tmp",
        manifest={"id": "test"},
        backend_type="playwright",
    )
    backend = ctx.get_actor_backend("default")
    assert isinstance(backend, PlaywrightBackend)
    # Same actor should return same backend
    assert ctx.get_actor_backend("default") is backend
    # Different actor should create a new backend
    other = ctx.get_actor_backend("other")
    assert other is not backend


def test_scenario_context_close_all_closes_backends():
    """close_all() should close every actor backend."""
    from scenario_runner import ScenarioContext

    closed = []

    class FakePage:
        pass

    class FakeBrowserCtx:
        def new_page(self):
            return FakePage()
        def close(self):
            closed.append(True)

    class FakeBrowser:
        def new_context(self):
            return FakeBrowserCtx()

    ctx = ScenarioContext(
        base_url="http://localhost:8000",
        browser=FakeBrowser(),
        output_dir="/tmp",
        manifest={"id": "test"},
        backend_type="playwright",
    )
    ctx.get_actor_backend("a")
    ctx.get_actor_backend("b")
    ctx.close_all()
    assert len(closed) == 2
