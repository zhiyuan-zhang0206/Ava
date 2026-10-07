"""Browser mcp daemon cases: parse page listing tolerates titled and suffix."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from base.config import settings
from services.desktop.browser import page_lifecycle
from services.desktop.browser.mcp_daemon import _text_of
from services.desktop.browser.page_lifecycle import (
    local_host_port,
    parse_page_listing,
    port_listening,
    reap_dead_agent_pages,
)
from services.desktop.browser.tests.test_browser_mcp_daemon import (
    _daemon,
    _expire,
    _new_daemon,
    _record_emits,
    _stamp_idle,
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


def test_local_host_port_only_matches_local_http() -> None:
    assert local_host_port("http://localhost:3112/x") == ("localhost", 3112)
    assert local_host_port("http://127.0.0.1/") == ("127.0.0.1", 80)
    assert local_host_port("https://localhost") == ("localhost", 443)
    assert local_host_port("https://github.com/ava/ava") is None
    assert local_host_port("chrome-error://chromewebdata/") is None
    assert local_host_port("file:///tmp/x") is None
    assert local_host_port("http://localhost:abc") is None


async def test_port_listening_dead_refused_live_accepts() -> None:
    """A refused port is dead; a live listener is alive (probe connects and
    closes, sends no bytes)."""
    server = await asyncio.start_server(lambda _r, w: w.close(), host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]  # type: ignore[index]
    try:
        assert await port_listening("127.0.0.1", port) is True
    finally:
        server.close()
        await server.wait_closed()
    # The listener is gone now — the port refuses.
    assert await port_listening("127.0.0.1", port) is False


async def test_reap_dead_agent_pages_closes_dead_local_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reaper closes agent-owned pages whose URL is localhost with no
    listener, keeps pages on a live port, and never inspects non-local URLs
    (or user tabs — only affinity slots are candidates)."""
    probes: list[tuple[str, int]] = []

    async def fake_probe(host: str, port: int) -> bool:
        probes.append((host, port))
        return (host, port) != ("localhost", 3111)  # 3111 dead; everything else alive

    monkeypatch.setattr("services.desktop.browser.page_lifecycle.port_listening", fake_probe)

    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:3111/next"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:3112/live"}, 8)
    await d.call_tool_for_agent("new_page", {"url": "https://github.com/ava/ava"}, 9)
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:3101/dead2"}, 10)
    up.calls.clear()

    await reap_dead_agent_pages(d)

    # 7's dead tab closed + slot cleared; 8 (live port), 9 (foreign host) and
    # 10 (live port 3101 per the probe) are untouched.
    assert 1 not in up.pages
    assert up.pages == {
        2: "http://localhost:3112/live",
        3: "https://github.com/ava/ava",
        4: "http://localhost:3101/dead2",
    }
    assert d.pages.get_agent_page(7, d.generation) is None
    assert d.pages.get_agent_page(8, d.generation) == 2
    assert d.pages.get_agent_page(9, d.generation) == 3
    assert d.pages.get_agent_page(10, d.generation) == 4
    # exactly the local candidates were probed; the foreign URL was never touched
    assert ("localhost", 3111) in probes and ("localhost", 3112) in probes
    assert ("localhost", 3101) in probes
    assert ("github.com", 443) not in probes
    closed = [c[:2] for c in up.calls if c[0] == "close_page"]
    assert closed == [("close_page", {"pageId": 1})]


async def test_reap_dead_agent_pages_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second pass has nothing left to close — the slots are cleared, the
    dead pages are gone from the listing."""

    async def fake_probe(host: str, port: int) -> bool:
        return False  # everything dead

    monkeypatch.setattr("services.desktop.browser.page_lifecycle.port_listening", fake_probe)

    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:3111/a"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:3112/b"}, 8)
    up.calls.clear()

    await reap_dead_agent_pages(d)
    await reap_dead_agent_pages(d)

    assert [c[:2] for c in up.calls if c[0] == "close_page"] == [
        ("close_page", {"pageId": 1}),
        ("close_page", {"pageId": 2}),
    ]
    assert up.pages == {}
    # slots cleared to None (same shape as an agent closing its own page) —
    # nothing left to sweep
    assert {agent: d.pages.get_agent_page(agent, d.generation) for agent in (7, 8)} == {
        7: None,
        8: None,
    }


async def test_reap_dead_agent_pages_never_touches_slotless_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tab with NO affinity slot — the user's own tab, or an agent that never
    went through the bridge — must be immune to the reaper even on a dead
    localhost port. The registry is the ONLY thing that ever makes a page a
    candidate (mutation guard: a reaper that swept all pages instead of slots
    must fail this test)."""

    async def fake_probe(host: str, port: int) -> bool:
        return False  # everything dead

    monkeypatch.setattr("services.desktop.browser.page_lifecycle.port_listening", fake_probe)

    d, up = _daemon()
    # a slotless tab seeded directly (never created via call_tool_for_agent)
    up.pages[99] = "http://localhost:9999/user-own-tab"
    up.calls.clear()

    await reap_dead_agent_pages(d)

    # the tab survives and no close_page ever reached the upstream
    assert up.pages == {99: "http://localhost:9999/user-own-tab"}
    assert up.calls == [("list_pages", {}, None)]
    assert d.pages.affinity == {}


async def test_port_listening_stalled_connect_reads_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect that neither completes nor refuses within the probe bound is
    treated as ALIVE — the docstring's headline promise: never close a live tab
    on a probe that could not decide (mutation guard for the timeout branch)."""

    async def _stall(*_args: object, **_kwargs: object) -> object:
        await asyncio.sleep(60)

    monkeypatch.setattr("services.desktop.browser.page_lifecycle.asyncio.open_connection", _stall)
    monkeypatch.setattr("services.desktop.browser.page_lifecycle._PORT_PROBE_TIMEOUT_S", 0.05)
    assert await port_listening("localhost", 9999) is True


async def test_reap_dead_agent_pages_midpass_navigation_skips_live_repurpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent navigates its page to a LIVE target while the pass is probing
    it: the close phase re-reads the listing under the lock, sees the URL has
    changed, and must NOT close the page (mutation guard for the re-list URL
    check)."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:3111/old"}, 7)  # page 1
    up.calls.clear()

    async def fake_probe(host: str, port: int) -> bool:
        # simulate the agent repurposing its page mid-pass (same page id)
        await d.call_tool_for_agent("navigate_page", {"url": "http://localhost:3311/live"}, 7)
        return False  # the OLD port is dead

    monkeypatch.setattr("services.desktop.browser.page_lifecycle.port_listening", fake_probe)

    await reap_dead_agent_pages(d)

    # the re-list saw the new (live) URL: the page is left alone
    assert up.pages == {1: "http://localhost:3311/live"}
    closes = [c[:2] for c in up.calls if c[0] == "close_page"]
    assert closes == []
    assert d.pages.get_agent_page(7, d.generation) == 1


async def test_reap_dead_agent_pages_midpass_slot_move_keeps_live_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent opens a NEW page (slot moves) while the pass probes the old
    dead one: the dead page is closed, but the slot — now naming the live new
    page — is preserved (mutation guard for the conditional slot clear)."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:3111/old"}, 7)  # page 1
    up.calls.clear()

    async def fake_probe(host: str, port: int) -> bool:
        # the agent's slot moves to a fresh page mid-pass
        await d.call_tool_for_agent("new_page", {"url": "http://localhost:3311/live"}, 7)
        return False  # the old port is dead

    monkeypatch.setattr("services.desktop.browser.page_lifecycle.port_listening", fake_probe)

    await reap_dead_agent_pages(d)

    # old dead page closed; the new live page and its slot survive
    assert 1 not in up.pages
    assert up.pages == {2: "http://localhost:3311/live"}
    closes = [c[:2] for c in up.calls if c[0] == "close_page"]
    assert closes == [("close_page", {"pageId": 1})]
    assert d.pages.get_agent_page(7, d.generation) == 2


async def test_call_tool_for_agent_stamps_last_use() -> None:
    """Every agent-keyed call moves the idle stamp, so a working agent is
    never an idle-recycle candidate."""
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    assert 7 in d.pages.last_use
    assert time.monotonic() - d.pages.last_use[7] < 5


async def test_reap_idle_agent_pages_closes_only_idle_owned_pages() -> None:
    """The idle sweep closes agent-owned pages whose owner has been idle past
    the timeout, clears the slot + stamp, and leaves recent-use agents and
    slotless (user) tabs untouched."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/old"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/live"}, 8)
    up.pages[99] = "https://user.example.com/mine"  # the user's own tab
    up.calls.clear()

    # agent 7 idle past the timeout; agent 8 used the browser just now
    _stamp_idle(d, 7, page_lifecycle._TAB_IDLE_TIMEOUT_S + 60)

    closed = await page_lifecycle.reap_idle_agent_pages(d)

    assert closed == 1
    assert up.pages == {
        2: "https://example.com/live",
        99: "https://user.example.com/mine",
    }
    assert d.pages.get_agent_page(7, d.generation) is None
    assert d.pages.get_agent_page(8, d.generation) == 2
    assert 7 not in d.pages.last_use
    assert [c[:2] for c in up.calls if c[0] == "close_page"] == [("close_page", {"pageId": 1})]


async def test_reap_idle_agent_pages_idempotent() -> None:
    """A second pass has nothing left to close — slot cleared, stamp gone."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    _stamp_idle(d, 7, page_lifecycle._TAB_IDLE_TIMEOUT_S + 1)
    up.calls.clear()

    await page_lifecycle.reap_idle_agent_pages(d)
    await page_lifecycle.reap_idle_agent_pages(d)

    assert [c[:2] for c in up.calls if c[0] == "close_page"] == [("close_page", {"pageId": 1})]
    assert {7: d.pages.get_agent_page(7, d.generation)} == {7: None}


async def test_reap_idle_agent_pages_within_timeout_keeps_page() -> None:
    """An agent that used the browser inside the timeout window keeps its
    page even if its URL points at a dead host — idle is the only signal."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "https://example.com/x"}, 7)
    _stamp_idle(d, 7, page_lifecycle._TAB_IDLE_TIMEOUT_S - 1)
    up.calls.clear()

    assert await page_lifecycle.reap_idle_agent_pages(d) == 0
    assert up.pages == {1: "https://example.com/x"}
    assert d.pages.get_agent_page(7, d.generation) == 1


async def test_release_agent_page_clears_idle_stamp() -> None:
    """A deterministic release also drops the last-use stamp, so a released
    agent never lingers in the idle registry."""
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    assert 7 in d.pages.last_use
    await page_lifecycle.release_agent_page(d, 7)
    assert 7 not in d.pages.last_use


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
    assert d.pages.get_agent_page(7, d.generation) is None
    assert d.pages.get_agent_page(8, d.generation) == 2
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
    assert d.pages.get_agent_page(7, d.generation) is None


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
    # near deadline, same generation (a renewal under this daemon must see it)
    d.pages.ttl_deadlines[1] = (time.monotonic() + 1.0, d.generation)
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
    d.pages.set_agent_page(7, 99, d.generation)  # as if select_page had adopted the user's tab

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
    assert d.pages.get_agent_page(7, d.generation) == 2
    assert 2 in d.pages.ttl_deadlines  # the new page has its own TTL
    assert 1 not in d.pages.ttl_deadlines


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


async def test_idle_sweep_drops_ttl_slot() -> None:
    """The idle sweep closing a page also forgets its TTL slot."""
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "x"}, 7)
    _stamp_idle(d, 7, page_lifecycle._TAB_IDLE_TIMEOUT_S + 1)
    await page_lifecycle.reap_idle_agent_pages(d)
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
