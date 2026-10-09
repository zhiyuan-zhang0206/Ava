"""Selecting an existing tab grants affinity without automatic cleanup custody."""

from __future__ import annotations

import time
from typing import Literal

import pytest

from services.desktop.browser import page_lifecycle
from services.desktop.browser.tests.test_browser_mcp_daemon import _daemon, _expire, _new_daemon


@pytest.mark.parametrize("cleanup", ["release", "idle", "dead"])
async def test_selected_user_page_survives_automatic_cleanup(
    cleanup: Literal["release", "idle", "dead"], monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, upstream = _daemon()
    upstream.pages[99] = "http://localhost:9999/user-tab"
    await daemon.call_tool_for_agent("select_page", {"pageId": 99}, 7)
    assert daemon.pages.get_agent_page(7, daemon.generation) == 99
    assert 99 not in daemon.pages.ttl_deadlines
    daemon.pages.last_use[7] = time.monotonic() - page_lifecycle._TAB_IDLE_TIMEOUT_S - 1
    upstream.calls.clear()
    probes: list[tuple[str, int]] = []

    async def dead_port(host: str, port: int) -> bool:
        probes.append((host, port))
        return False

    monkeypatch.setattr(page_lifecycle, "port_listening", dead_port)
    if cleanup == "release":
        assert await page_lifecycle.release_agent_page(daemon, 7) is None
        assert daemon.pages.get_agent_page(7, daemon.generation) is None
        assert 7 not in daemon.pages.last_use
    elif cleanup == "idle":
        assert await page_lifecycle.reap_idle_agent_pages(daemon) == 0
    else:
        await page_lifecycle.reap_dead_agent_pages(daemon)

    assert upstream.pages == {99: "http://localhost:9999/user-tab"}
    assert not [call for call in upstream.calls if call[0] == "close_page"]
    assert probes == []


@pytest.mark.parametrize("cleanup", ["release", "idle", "dead"])
async def test_selected_reused_page_id_does_not_inherit_previous_generation_custody(
    cleanup: Literal["release", "idle", "dead"], monkeypatch: pytest.MonkeyPatch
) -> None:
    previous, _ = _daemon()
    await previous.call_tool_for_agent("new_page", {"url": "http://localhost:9999/old"}, 7)
    # A fresh upstream reuses id 1 for a user's existing page. Selecting it
    # creates current affinity while the creation record remains from before.
    daemon, upstream = _daemon()
    daemon = _new_daemon(upstream, previous.pages)
    upstream.pages[1] = "http://localhost:9999/user-tab"
    await daemon.call_tool_for_agent("select_page", {"pageId": 1}, 7)
    assert daemon.pages.get_agent_page(7, daemon.generation) == 1
    assert daemon.pages.ttl_deadlines[1][1] != daemon.generation
    daemon.pages.last_use[7] = time.monotonic() - page_lifecycle._TAB_IDLE_TIMEOUT_S - 1
    upstream.calls.clear()

    async def dead_port(_host: str, _port: int) -> bool:
        pytest.fail("A borrowed page must not be probed")

    monkeypatch.setattr(page_lifecycle, "port_listening", dead_port)
    if cleanup == "release":
        assert await page_lifecycle.release_agent_page(daemon, 7) is None
    elif cleanup == "idle":
        assert await page_lifecycle.reap_idle_agent_pages(daemon) == 0
    else:
        await page_lifecycle.reap_dead_agent_pages(daemon)

    assert upstream.pages == {1: "http://localhost:9999/user-tab"}
    assert not [call for call in upstream.calls if call[0] == "close_page"]


@pytest.mark.parametrize("change", ["drop", "generation"])
async def test_dead_sweep_rechecks_creation_record_after_port_probe(
    change: Literal["drop", "generation"], monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, upstream = _daemon()
    await daemon.call_tool_for_agent("new_page", {"url": "http://localhost:9999/page"}, 7)
    upstream.calls.clear()
    probes: list[tuple[str, int]] = []

    async def changed_during_probe(host: str, port: int) -> bool:
        probes.append((host, port))
        if change == "drop":
            daemon.pages.drop_page_ttl(1)
        else:
            deadline, _ = daemon.pages.ttl_deadlines[1]
            daemon.pages.ttl_deadlines[1] = (deadline, daemon.pages.new_generation())
        return False

    monkeypatch.setattr(page_lifecycle, "port_listening", changed_during_probe)
    await page_lifecycle.reap_dead_agent_pages(daemon)

    assert probes == [("localhost", 9999)]
    assert upstream.pages == {1: "http://localhost:9999/page"}
    assert not [call for call in upstream.calls if call[0] == "close_page"]


async def test_owned_page_keeps_creation_deadline_after_switching_to_user_tab() -> None:
    daemon, upstream = _daemon()
    await daemon.call_tool_for_agent("new_page", {"url": "https://example.com/owned"}, 7)
    upstream.pages[99] = "https://example.com/user-tab"
    deadline = daemon.pages.ttl_deadlines[1]
    await daemon.call_tool_for_agent("select_page", {"pageId": 99}, 7)
    assert daemon.pages.ttl_deadlines[1] == deadline
    assert await page_lifecycle.release_agent_page(daemon, 7) is None
    assert 1 in upstream.pages

    _expire(daemon, 1)
    assert await page_lifecycle.reap_expired_pages(daemon) == 1
    assert upstream.pages == {99: "https://example.com/user-tab"}
