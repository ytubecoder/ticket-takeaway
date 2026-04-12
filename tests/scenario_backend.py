"""Backend protocol and implementations for the scenario runner.

A Backend abstracts browser interaction so scenarios can run against either
a Playwright-launched browser (PlaywrightBackend) or an already-running
browser connected via CDP (CDPBackend).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Backend(Protocol):
    """Protocol every scenario backend must implement."""

    def navigate(self, url: str) -> None: ...
    def reload(self) -> None: ...
    def click(self, target: dict, seed_id_map: dict) -> None: ...
    def dblclick(self, target: dict, seed_id_map: dict) -> None: ...
    def fill(self, target: dict, value: str, seed_id_map: dict) -> None: ...
    def select(self, target: dict, value: str, seed_id_map: dict) -> None: ...
    def press(self, target: dict, key: str, seed_id_map: dict) -> None: ...
    def wait_for_visible(self, target: dict, timeout: int, seed_id_map: dict) -> None: ...
    def wait_for_hidden(self, target: dict, timeout: int, seed_id_map: dict) -> None: ...
    def wait_for_text(self, text: str, timeout: int) -> None: ...
    def screenshot(self, path: str, full_page: bool = False) -> str: ...
    def wait_for_settled(self, timeout: int = 5000) -> None: ...
    def evaluate(self, js: str) -> Any: ...
    def get_text(self, target: dict, seed_id_map: dict) -> str: ...
    def close(self) -> None: ...


def resolve_target(page: Any, target: dict, seed_id_map: dict) -> Any:
    """Return a Playwright Locator (or equivalent) for a target descriptor.

    Both PlaywrightBackend and CDPBackend use this since both wrap
    Playwright page objects. seed_id_map maps ticket titles (and
    "ticket-N" positional refs) to ticket IDs.

    Supported keys: testid, title, seed_ref, css, text, role.
    """
    if "testid" in target:
        return page.get_by_test_id(target["testid"])

    if "title" in target:
        title = target["title"]
        ticket_id = seed_id_map.get(title)
        if ticket_id is None:
            raise ValueError(
                f"Title {title!r} not found in seed_id_map. "
                f"Available: {list(seed_id_map.keys())}"
            )
        if target.get("open"):
            return page.get_by_test_id(f"card-open-btn-{ticket_id}")
        return page.get_by_test_id(f"ticket-card-{ticket_id}")

    if "seed_ref" in target:
        ref = target["seed_ref"]
        try:
            index = int(ref.split("-")[-1])
        except (ValueError, IndexError):
            raise ValueError(
                f"Invalid seed_ref format: {ref!r}. Expected 'ticket-N'."
            )
        ids = list(seed_id_map.values())
        if index >= len(ids):
            raise ValueError(
                f"seed_ref index {index} out of range "
                f"(have {len(ids)} seed tickets)"
            )
        return page.get_by_test_id(f"ticket-card-{ids[index]}")

    if "css" in target:
        return page.locator(target["css"])

    if "text" in target:
        return page.get_by_text(target["text"], exact=False)

    if "role" in target:
        return page.get_by_role(target["role"], name=target.get("name", ""))

    raise ValueError(f"Unrecognised target descriptor: {target!r}")


# ---------------------------------------------------------------------------
# PlaywrightBackend
# ---------------------------------------------------------------------------

# Settlement JS: wait for no DOM mutations for 300ms
_SETTLED_JS = """
() => {
    return new Promise((resolve) => {
        let timer = null;
        const observer = new MutationObserver(() => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                observer.disconnect();
                resolve(true);
            }, 300);
        });
        observer.observe(document.body, {
            childList: true, subtree: true,
            attributes: true, characterData: true
        });
        timer = setTimeout(() => {
            observer.disconnect();
            resolve(true);
        }, 300);
    });
}
"""

_LOADING_GONE_JS = (
    "document.querySelectorAll('.loading, [aria-busy=\"true\"]').length === 0"
)


class PlaywrightBackend:
    """Backend that drives a Playwright Page directly.

    Construction: pass an already-created Page and its BrowserContext.
    The caller (ScenarioContext.get_actor_backend) is responsible for
    creating the context and page.
    """

    def __init__(self, page: Any, context: Any) -> None:
        self.page = page
        self.context = context

    def navigate(self, url: str) -> None:
        self.page.goto(url)
        self.page.wait_for_load_state("domcontentloaded")

    def reload(self) -> None:
        self.page.reload()
        self.page.wait_for_load_state("domcontentloaded")

    def click(self, target: dict, seed_id_map: dict) -> None:
        resolve_target(self.page, target, seed_id_map).click()

    def dblclick(self, target: dict, seed_id_map: dict) -> None:
        resolve_target(self.page, target, seed_id_map).dblclick()

    def fill(self, target: dict, value: str, seed_id_map: dict) -> None:
        resolve_target(self.page, target, seed_id_map).fill(value)

    def select(self, target: dict, value: str, seed_id_map: dict) -> None:
        resolve_target(self.page, target, seed_id_map).select_option(value)

    def press(self, target: dict, key: str, seed_id_map: dict) -> None:
        resolve_target(self.page, target, seed_id_map).press(key)

    def wait_for_visible(
        self, target: dict, timeout: int, seed_id_map: dict
    ) -> None:
        resolve_target(self.page, target, seed_id_map).wait_for(
            state="visible", timeout=timeout
        )

    def wait_for_hidden(
        self, target: dict, timeout: int, seed_id_map: dict
    ) -> None:
        resolve_target(self.page, target, seed_id_map).wait_for(
            state="hidden", timeout=timeout
        )

    def wait_for_text(self, text: str, timeout: int) -> None:
        self.page.get_by_text(text, exact=False).wait_for(
            state="visible", timeout=timeout
        )

    def screenshot(self, path: str, full_page: bool = False) -> str:
        self.page.screenshot(path=path, full_page=full_page)
        return path

    def wait_for_settled(self, timeout: int = 5000) -> None:
        try:
            self.page.wait_for_function(_SETTLED_JS, timeout=timeout)
        except Exception:
            self.page.wait_for_timeout(500)
        try:
            self.page.wait_for_function(_LOADING_GONE_JS, timeout=2000)
        except Exception:
            pass

    def evaluate(self, js: str) -> Any:
        return self.page.evaluate(js)

    def get_text(self, target: dict, seed_id_map: dict) -> str:
        loc = resolve_target(self.page, target, seed_id_map)
        return loc.text_content() or ""

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:
            pass
