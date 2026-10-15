from __future__ import annotations

from typing import Literal

import pytest

from services.desktop.browser import page_lifecycle
from services.desktop.browser.tests.test_browser_mcp_daemon import (
    browser_daemon_with_fake_upstream,
    expire,
)


@pytest.mark.parametrize("cleanup", ["release", "expiry"])
async def test_selected_user_page_survives_automatic_cleanup(
    cleanup: Literal["release", "expiry"],
) -> None:
    daemon, upstream = browser_daemon_with_fake_upstream()
    upstream.pages[99] = "http://localhost:9999/user-tab"
    await daemon.call_tool_for_agent("select_page", {"pageId": 99}, 7)
    assert daemon.pages.get_agent_page(7) == 99
    assert 99 not in daemon.pages.ttl_deadlines
    upstream.calls.clear()
    if cleanup == "release":
        assert await page_lifecycle.release_agent_page(daemon, 7) is None
        assert daemon.pages.get_agent_page(7) is None
    else:
        assert await page_lifecycle.reap_expired_pages(daemon) == 0

    assert upstream.pages == {99: "http://localhost:9999/user-tab"}
    assert not [call for call in upstream.calls if call[0] == "close_page"]


async def test_owned_page_keeps_creation_deadline_after_switching_to_user_tab() -> None:
    daemon, upstream = browser_daemon_with_fake_upstream()
    await daemon.call_tool_for_agent("new_page", {"url": "https://example.com/owned"}, 7)
    upstream.pages[99] = "https://example.com/user-tab"
    deadline = daemon.pages.ttl_deadlines[1]
    await daemon.call_tool_for_agent("select_page", {"pageId": 99}, 7)
    assert daemon.pages.ttl_deadlines[1] == deadline

    expire(daemon, 1)
    assert await page_lifecycle.reap_expired_pages(daemon) == 1
    assert upstream.pages == {99: "https://example.com/user-tab"}


async def test_release_after_switching_to_user_tab_closes_owned_page_only() -> None:
    daemon, upstream = browser_daemon_with_fake_upstream()
    await daemon.call_tool_for_agent("new_page", {"url": "https://example.com/owned"}, 7)
    upstream.pages[99] = "https://example.com/user-tab"
    await daemon.call_tool_for_agent("select_page", {"pageId": 99}, 7)
    assert await page_lifecycle.release_agent_page(daemon, 7) == 1
    assert daemon.pages.get_agent_page(7) is None
    assert upstream.pages == {99: "https://example.com/user-tab"}
