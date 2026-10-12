"""Browser mcp daemon cases: parse page listing tolerates titled and suffix."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from base.config import settings
from services.desktop.browser import page_lifecycle
from services.desktop.browser.mcp_daemon import _text_of
from services.desktop.browser.page_lifecycle import parse_page_listing
from services.desktop.browser.tests.test_browser_mcp_daemon import (
    _daemon,
    _expire,
    _new_daemon,
    _record_emits,
    _TitledListing,
    _ttl_remaining,
)


def test_parse_page_listing_tolerates_titled_and_suffix_shapes() -> None:
    """chrome-devtools-mcp renders a titled page as `<id>: <title> (<url>)`,
    optionally followed by ` [selected]` and ` key=value` suffixes (an
    isolated-context tab appends ` isolatedContext=<name>`). Missing those
    shapes read a live page as already-closed -- the sweeps then dropped its
    slot without closing the tab (#3570)."""
    assert parse_page_listing(
        "  29: Discord (https://discord.com/app) [selected] isolatedContext=discord-reg-7"
    ) == {29: "https://discord.com/app"}
    assert parse_page_listing("  4: My Page (https://example.com/x)") == {
        4: "https://example.com/x"
    }
    # An untitled page with the isolated suffix parses too.
    assert parse_page_listing("  5: https://x isolatedContext=reg-7") == {5: "https://x"}
    # A title containing parens: the URL is the last `(…)` group.
    assert parse_page_listing("  6: My Page (draft) (https://url)") == {6: "https://url"}
    # A URL that itself contains parens stays whole for the untitled shape ...
    assert parse_page_listing("  7: https://en.wikipedia.org/wiki/Foo_(bar)") == {
        7: "https://en.wikipedia.org/wiki/Foo_(bar)"
    }
    # ... and for a titled one (the `(` inside the URL is not a label paren).
    assert parse_page_listing("  8: Wiki Page (https://en.wikipedia.org/wiki/Foo_(bar))") == {
        8: "https://en.wikipedia.org/wiki/Foo_(bar)"
    }


async def test_new_page_registers_ttl_on_both_creation_paths() -> None:
    """Coverage is every page this stack creates: an explicit new_page AND the
    auto-created page on a page-less first navigate both get a deadline."""
    d, _up = _daemon()
    default = settings.daemon.chrome_page_default_ttl_seconds
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/a"}, 7)
    assert 0 < _ttl_remaining(d, 1) <= default
    # the auto-created page on a cold-start navigate is the other half
    await d.call_tool_for_agent("navigate_page", {"url": "https://example.com/b"}, 8)
    assert 0 < _ttl_remaining(d, 2) <= default


async def test_page_ttl_default_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cluster knob sets the registration deadline."""
    monkeypatch.setattr(settings.daemon, "chrome_page_default_ttl_seconds", 120.0)
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    assert 100 < _ttl_remaining(d, 1) <= 120


async def test_reap_expired_pages_closes_only_expired_stack_pages() -> None:
    """The TTL sweep closes the expired stack page and only it: a fresh stack
    page and the user's own (never-registered) tab are untouched; the expiry
    clears the owner's affinity slot and drops the TTL slot."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/old"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/fresh"}, 8)
    up.pages[99] = "https://user.example.com/mine"  # the user's own tab
    up.calls.clear()
    _expire(d, 1)

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
    """End-to-end incident shape: an expired page whose listing line reads
    `1: Page 1 (url) [selected] isolatedContext=reg-7` must be CLOSED by the
    TTL sweep. The titled/suffixed line used to parse to no URL, and the sweep
    dropped the slot as "already closed" without ever closing the tab
    (2026-09-15 19:04 shape, #3570)."""
    up = _TitledListing()
    d = _new_daemon(up)
    await d.call_tool_for_agent("new_page", {"url": "https://discord.com/app"}, 7)
    up.isolated[1] = "discord-reg-7"
    up.calls.clear()
    _expire(d, 1)

    closed = await page_lifecycle.reap_expired_pages(d)

    assert closed == 1
    assert up.pages == {}
    assert [(c[0], c[1]) for c in up.calls if c[0] == "close_page"] == [
        ("close_page", {"pageId": 1})
    ]
    assert 1 not in d.pages.ttl_deadlines
    assert d.pages.get_agent_page(7) is None


async def test_reap_expired_pages_never_touches_unregistered_page() -> None:
    """A page this stack never created (a user tab, or a tab predating the
    TTL) has no slot and is never a candidate, however old."""
    d, up = _daemon()
    up.pages[99] = "https://user.example.com/mine"
    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert up.pages == {99: "https://user.example.com/mine"}
    assert not [c for c in up.calls if c[0] == "close_page"]


async def test_reap_expired_pages_drops_slot_when_page_already_gone() -> None:
    """CAS: a slot whose page is already closed (close_page / release / a
    manual tab close) is dropped with no close attempt, and a second pass is
    a no-op."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    del up.pages[1]  # closed underneath the daemon
    up.calls.clear()
    _expire(d, 1)

    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert not [c for c in up.calls if c[0] == "close_page"]
    assert d.pages.ttl_deadlines == {}


async def test_reap_expired_pages_idempotent() -> None:
    """A second pass has nothing left to close — slot dropped, page gone."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    up.calls.clear()
    _expire(d, 1)

    await page_lifecycle.reap_expired_pages(d)
    await page_lifecycle.reap_expired_pages(d)

    assert [(c[0], c[1]) for c in up.calls if c[0] == "close_page"] == [
        ("close_page", {"pageId": 1})
    ]


async def test_renew_page_extends_deadline_and_sweep_keeps_it() -> None:
    """renew_page moves the deadline to now + ttl (never stacked), and a page
    renewed before its old deadline survives the next sweep."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    # near deadline (a renewal must see it)
    d.pages.ttl_deadlines[1] = time.monotonic() + 1.0
    up.calls.clear()

    res = await d.call_tool_for_agent("renew_page", {"ttl": 3600}, 7)

    assert not res.is_error
    assert "renewed" in _text_of(res)
    assert 3500 < _ttl_remaining(d, 1) <= 3600
    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert up.pages == {1: "x"}


async def test_renew_page_defaults_to_configured_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.daemon, "chrome_page_default_ttl_seconds", 120.0)
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)

    res = await d.call_tool_for_agent("renew_page", {}, 7)

    assert not res.is_error
    assert 100 < _ttl_remaining(d, 1) <= 120


async def test_renew_page_rejected_after_deadline() -> None:
    """No renewable expired state (the shell-TTL ruling): a passed deadline
    loses the renewal cleanly, and the sweep owns the page."""
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    _expire(d, 1)

    res = await d.call_tool_for_agent("renew_page", {"ttl": 60}, 7)

    assert res.is_error
    assert "cannot be renewed" in _text_of(res)
    assert await page_lifecycle.reap_expired_pages(d) == 1


async def test_renew_page_rejects_bad_arguments() -> None:
    """ttl is validated at the edge: finite > 0, capped at 24h; unknown
    arguments are refused rather than silently ignored."""
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    for bad in (0, -5, float("inf"), "60", True):
        res = await d.call_tool_for_agent("renew_page", {"ttl": bad}, 7)
        assert res.is_error, bad
    res = await d.call_tool_for_agent("renew_page", {"ttl": 90_000}, 7)
    assert res.is_error
    assert "at most 86400" in _text_of(res)
    res = await d.call_tool_for_agent("renew_page", {"bogus": 1}, 7)
    assert res.is_error
    assert "unexpected argument" in _text_of(res)


async def test_renew_page_needs_current_page() -> None:
    d, _up = _daemon()
    res = await d.call_tool_for_agent("renew_page", {"ttl": 60}, 7)
    assert res.is_error
    assert "no current page" in _text_of(res)


async def test_renew_page_rejects_unmanaged_page() -> None:
    """A page the stack never created (a user tab the agent merely selected)
    has no TTL slot and no renewal path."""
    d, up = _daemon()
    up.pages[99] = "https://user.example.com/mine"
    d.pages.set_agent_page(7, 99)  # as if select_page had adopted the user's tab

    res = await d.call_tool_for_agent("renew_page", {"ttl": 60}, 7)

    assert res.is_error
    assert "no page TTL" in _text_of(res)
    assert 99 not in d.pages.ttl_deadlines


async def test_ttl_expiry_surfaces_native_no_page_red_green() -> None:
    """Red-green: the page works before its deadline; after expiry the next
    call takes the existing no-page path (no invented "page expired" error);
    a fresh navigate cold-starts a new page carrying its own TTL."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/app"}, 7)
    ok = await d.call_tool_for_agent("take_snapshot", {}, 7)
    assert not ok.is_error  # green: usable before the deadline
    _expire(d, 1)
    assert await page_lifecycle.reap_expired_pages(d) == 1

    after = await d.call_tool_for_agent("take_snapshot", {}, 7)

    assert after.is_error
    assert "No page selected" in _text_of(after)  # the existing no-page path
    assert "expired" not in _text_of(after).lower()  # never an invented wrapper

    nav = await d.call_tool_for_agent("navigate_page", {"url": "https://example.com/app"}, 7)
    assert not nav.is_error
    assert up.pages == {2: "https://example.com/app"}  # the expired tab is gone
    assert d.pages.get_agent_page(7) == 2
    assert 2 in d.pages.ttl_deadlines  # the new page has its own TTL
    assert 1 not in d.pages.ttl_deadlines


async def test_expired_page_reaper_leaves_a_dead_localhost_page_until_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TTL deadline is the only cleanup for a page nobody releases.

    An agent killed before its exit hook leaves a tab on a dead localhost port and
    never touches the browser again. No sweep probes the port or the owner's last
    use: the tab stays open across passes until its deadline, then the expiry
    sweep closes it.
    """
    monkeypatch.setattr(page_lifecycle, "_PAGE_SWEEP_INTERVAL_S", 0.01)
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:9/dev"}, 7)
    up.calls.clear()
    stop = asyncio.Event()
    reaper = asyncio.create_task(page_lifecycle.expired_page_reaper(d, stop))
    try:
        await asyncio.sleep(0.2)  # many passes, nothing listens on port 9
        assert up.pages == {1: "http://localhost:9/dev"}
        assert not [call for call in up.calls if call[0] == "close_page"]

        _expire(d, 1)
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
    """A clean close_page forgets the page's TTL with it."""
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    res = await d.call_tool_for_agent("close_page", {"pageId": 1}, 7)
    assert not res.is_error
    assert d.pages.ttl_deadlines == {}


async def test_release_agent_page_drops_ttl_slot() -> None:
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    await page_lifecycle.release_agent_page(d, 7)
    assert d.pages.ttl_deadlines == {}


async def test_renew_page_requires_agent_identity_on_legacy_path() -> None:
    """A connection without an agent id (legacy wrapper) has no page to
    renew; the call is refused locally, never forwarded upstream."""
    d, up = _daemon()
    res, page = await d.call_tool("renew_page", {"ttl": 60}, None)
    assert res.is_error
    assert "requires an agent identity" in _text_of(res)
    assert page is None
    assert not up.calls


async def test_list_tools_appends_renew_page() -> None:
    """The upstream passthrough stays intact and the daemon-owned renew_page
    is appended once, with its schema."""
    d, _up = _daemon()
    tools = await d.list_tools()
    names = [t["name"] for t in tools]
    assert "take_snapshot" in names
    renew = [t for t in tools if t["name"] == "renew_page"]
    assert len(renew) == 1
    assert renew[0]["inputSchema"]["properties"]["ttl"]["type"] == "number"


async def test_renew_page_emits_audit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every renewal is auditable through the event pipeline, carrying the TTL
    as a whole-second int (task #4011's cast: a float here would flip the
    emitted metric family to a histogram)."""
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    emitted: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(page_lifecycle.telemetry, "emit", _record_emits(emitted))

    res = await d.call_tool_for_agent("renew_page", {"ttl": 60}, 7)

    assert not res.is_error
    assert emitted[0][0][:2] == ("telemetry", "chrome_page_ttl_renewed")
    assert emitted[0][1]["agent_id"] == 7
    assert emitted[0][1]["attributes"]["ttl_s"] == 60
    assert isinstance(emitted[0][1]["attributes"]["ttl_s"], int)

    # A float caller still lands an int: locks the cast, not just the value.
    res2 = await d.call_tool_for_agent("renew_page", {"ttl": 90.9}, 7)
    assert not res2.is_error
    assert emitted[1][1]["attributes"]["ttl_s"] == 91
    assert isinstance(emitted[1][1]["attributes"]["ttl_s"], int)


async def test_expiry_emits_audit_event(monkeypatch: pytest.MonkeyPatch) -> None:
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/old"}, 7)
    _expire(d, 1)
    emitted: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(page_lifecycle.telemetry, "emit", _record_emits(emitted))

    assert await page_lifecycle.reap_expired_pages(d) == 1

    assert emitted[0][0][:2] == ("log", "chrome_page_ttl_expired")
    assert emitted[0][1]["attributes"]["page_id"] == 1
    assert emitted[0][1]["agent_id"] == 7
