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
