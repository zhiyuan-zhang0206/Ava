from __future__ import annotations

import asyncio
import time

import pytest

from base.config import settings
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.desktop.browser import page_lifecycle
from services.desktop.browser.mcp_daemon import text_of
from services.desktop.browser.page_lifecycle import parse_page_listing
from services.desktop.browser.tests.test_browser_mcp_daemon import (
    TitledListing,
    browser_daemon_with_fake_upstream,
    expire,
    new_daemon,
    ttl_remaining,
)


def test_parse_page_listing_tolerates_titled_and_suffix_shapes() -> None:
    assert parse_page_listing(
        "  29: Discord (https://discord.com/app) [selected] isolatedContext=discord-reg-7"
    ) == {29: "https://discord.com/app"}
    assert parse_page_listing("  4: My Page (https://example.com/x)") == {
        4: "https://example.com/x"
    }
    assert parse_page_listing("  5: https://x isolatedContext=reg-7") == {5: "https://x"}
    assert parse_page_listing("  6: My Page (draft) (https://url)") == {6: "https://url"}
    assert parse_page_listing("  7: https://en.wikipedia.org/wiki/Foo_(bar)") == {
        7: "https://en.wikipedia.org/wiki/Foo_(bar)"
    }
    assert parse_page_listing("  8: Wiki Page (https://en.wikipedia.org/wiki/Foo_(bar))") == {
        8: "https://en.wikipedia.org/wiki/Foo_(bar)"
    }


async def test_new_page_registers_ttl_on_both_creation_paths() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    default = settings.daemon.chrome_page_default_ttl_seconds
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/a"}, 7)
    assert 0 < ttl_remaining(d, 1) <= default
    await d.call_tool_for_agent("navigate_page", {"url": "https://example.com/b"}, 8)
    assert 0 < ttl_remaining(d, 2) <= default


async def test_page_ttl_default_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.daemon, "chrome_page_default_ttl_seconds", 120.0)
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    assert 100 < ttl_remaining(d, 1) <= 120


async def test_reap_expired_pages_closes_only_expired_stack_pages() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/old"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/fresh"}, 8)
    up.pages[99] = "https://user.example.com/mine"
    up.calls.clear()
    expire(d, 1)

    closed = await page_lifecycle.reap_expired_pages(d)

    assert closed == 1
    assert up.pages == {
        2: "https://example.com/fresh",
        99: "https://user.example.com/mine",
    }
    assert [(c[0], c[1]) for c in up.calls if c[0] == "close_page"] == [
        ("close_page", {"pageId": 1})
    ]
    assert d.pages.get_agent_page(7) is None
    assert d.pages.get_agent_page(8) == 2
    assert 1 not in d.pages.ttl_deadlines
    assert 2 in d.pages.ttl_deadlines


async def test_reap_expired_pages_closes_titled_isolated_page() -> None:
    up = TitledListing()
    d = new_daemon(up)
    await d.call_tool_for_agent("new_page", {"url": "https://discord.com/app"}, 7)
    up.isolated[1] = "discord-reg-7"
    up.calls.clear()
    expire(d, 1)

    closed = await page_lifecycle.reap_expired_pages(d)

    assert closed == 1
    assert up.pages == {}
    assert [(c[0], c[1]) for c in up.calls if c[0] == "close_page"] == [
        ("close_page", {"pageId": 1})
    ]
    assert 1 not in d.pages.ttl_deadlines
    assert d.pages.get_agent_page(7) is None


async def test_reap_expired_pages_never_touches_unregistered_page() -> None:
    d, up = browser_daemon_with_fake_upstream()
    up.pages[99] = "https://user.example.com/mine"
    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert up.pages == {99: "https://user.example.com/mine"}
    assert not [c for c in up.calls if c[0] == "close_page"]


async def test_reap_expired_pages_drops_slot_when_page_already_gone() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    del up.pages[1]
    up.calls.clear()
    expire(d, 1)

    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert not [c for c in up.calls if c[0] == "close_page"]
    assert d.pages.ttl_deadlines == {}


async def test_reap_expired_pages_idempotent() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    up.calls.clear()
    expire(d, 1)

    await page_lifecycle.reap_expired_pages(d)
    await page_lifecycle.reap_expired_pages(d)

    assert [(c[0], c[1]) for c in up.calls if c[0] == "close_page"] == [
        ("close_page", {"pageId": 1})
    ]


async def test_renew_page_extends_deadline_and_sweep_keeps_it() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    d.pages.ttl_deadlines[1] = time.monotonic() + 1.0
    up.calls.clear()

    res = await d.call_tool_for_agent("renew_page", {"ttl": 3600}, 7)

    assert not res.is_error
    assert "renewed" in text_of(res)
    assert 3500 < ttl_remaining(d, 1) <= 3600
    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert up.pages == {1: "x"}


async def test_renew_page_defaults_to_configured_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.daemon, "chrome_page_default_ttl_seconds", 120.0)
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)

    res = await d.call_tool_for_agent("renew_page", {}, 7)

    assert not res.is_error
    assert 100 < ttl_remaining(d, 1) <= 120


async def test_renew_page_rejected_after_deadline() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    expire(d, 1)

    res = await d.call_tool_for_agent("renew_page", {"ttl": 60}, 7)

    assert res.is_error
    assert "cannot be renewed" in text_of(res)
    assert await page_lifecycle.reap_expired_pages(d) == 1


async def test_renew_page_rejects_bad_arguments() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    for bad in (0, -5, float("inf"), "60", True):
        res = await d.call_tool_for_agent("renew_page", {"ttl": bad}, 7)
        assert res.is_error, bad
    res = await d.call_tool_for_agent("renew_page", {"ttl": 90_000}, 7)
    assert res.is_error
    assert "at most 86400" in text_of(res)
    res = await d.call_tool_for_agent("renew_page", {"bogus": 1}, 7)
    assert res.is_error
    assert "unexpected argument" in text_of(res)


async def test_renew_page_needs_current_page() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    res = await d.call_tool_for_agent("renew_page", {"ttl": 60}, 7)
    assert res.is_error
    assert "no current page" in text_of(res)


async def test_renew_page_rejects_unmanaged_page() -> None:
    d, up = browser_daemon_with_fake_upstream()
    up.pages[99] = "https://user.example.com/mine"
    d.pages.set_agent_page(7, 99)

    res = await d.call_tool_for_agent("renew_page", {"ttl": 60}, 7)

    assert res.is_error
    assert "no page TTL" in text_of(res)
    assert 99 not in d.pages.ttl_deadlines


async def test_ttl_expiry_surfaces_native_no_page_red_green() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/app"}, 7)
    ok = await d.call_tool_for_agent("take_snapshot", {}, 7)
    assert not ok.is_error
    expire(d, 1)
    assert await page_lifecycle.reap_expired_pages(d) == 1

    after = await d.call_tool_for_agent("take_snapshot", {}, 7)

    assert after.is_error
    assert "No page selected" in text_of(after)
    assert "expired" not in text_of(after).lower()

    nav = await d.call_tool_for_agent("navigate_page", {"url": "https://example.com/app"}, 7)
    assert not nav.is_error
    assert up.pages == {2: "https://example.com/app"}
    assert d.pages.get_agent_page(7) == 2
    assert 2 in d.pages.ttl_deadlines
    assert 1 not in d.pages.ttl_deadlines


async def test_expired_page_reaper_leaves_a_dead_localhost_page_until_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(page_lifecycle, "PAGE_SWEEP_INTERVAL_S", 0.01)
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:9/dev"}, 7)
    up.calls.clear()
    stop = asyncio.Event()
    reaper = asyncio.create_task(page_lifecycle.expired_page_reaper(d, stop))
    try:
        await asyncio.sleep(0.2)
        assert up.pages == {1: "http://localhost:9/dev"}
        assert not [call for call in up.calls if call[0] == "close_page"]

        expire(d, 1)
        for _ in range(100):
            if not up.pages:
                break
            await asyncio.sleep(0.01)
        assert up.pages == {}
        assert d.pages.ttl_deadlines == {}
    finally:
        stop.set()
        await reaper


async def test_close_page_drops_ttl_slot() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    res = await d.call_tool_for_agent("close_page", {"pageId": 1}, 7)
    assert not res.is_error
    assert d.pages.ttl_deadlines == {}


async def test_release_agent_page_drops_ttl_slot() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    await page_lifecycle.release_agent_page(d, 7)
    assert d.pages.ttl_deadlines == {}


async def test_renew_page_requires_agent_identity_on_legacy_path() -> None:
    d, up = browser_daemon_with_fake_upstream()
    res, page = await d.call_tool("renew_page", {"ttl": 60}, None)
    assert res.is_error
    assert "requires an agent identity" in text_of(res)
    assert page is None
    assert not up.calls


async def test_list_tools_appends_renew_page() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    tools = await d.list_tools()
    names = [t["name"] for t in tools]
    assert "take_snapshot" in names
    renew = [t for t in tools if t["name"] == "renew_page"]
    assert len(renew) == 1
    assert renew[0]["inputSchema"]["properties"]["ttl"]["type"] == "number"


async def test_renew_page_emits_audit_event(emitted_events: EmittedEvents) -> None:
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    emitted_events.clear()

    res = await d.call_tool_for_agent("renew_page", {"ttl": 60}, 7)

    assert not res.is_error
    emitted = list(emitted_events)
    assert (emitted[0].category, emitted[0].event_name) == ("telemetry", "chrome_page_ttl_renewed")
    assert emitted[0].agent_id == 7
    assert emitted[0].attributes["ttl_s"] == 60
    assert isinstance(emitted[0].attributes["ttl_s"], int)

    res2 = await d.call_tool_for_agent("renew_page", {"ttl": 90.9}, 7)
    assert not res2.is_error
    emitted = list(emitted_events)
    assert emitted[1].attributes["ttl_s"] == 91
    assert isinstance(emitted[1].attributes["ttl_s"], int)


async def test_expiry_emits_audit_event(emitted_events: EmittedEvents) -> None:
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/old"}, 7)
    expire(d, 1)
    emitted_events.clear()

    assert await page_lifecycle.reap_expired_pages(d) == 1

    emitted = list(emitted_events)
    assert (emitted[0].category, emitted[0].event_name) == ("log", "chrome_page_ttl_expired")
    assert emitted[0].attributes["page_id"] == 1
    assert emitted[0].agent_id == 7
