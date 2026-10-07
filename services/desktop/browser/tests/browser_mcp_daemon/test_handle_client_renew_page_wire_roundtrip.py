"""Browser mcp daemon cases: handle client renew page wire roundtrip."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from services.desktop.browser import page_lifecycle
from services.desktop.browser.mcp_daemon import ChromeMcpDaemon, _handle_client, _text_of
from services.desktop.browser.page_lifecycle import PageRegistry, reap_dead_agent_pages
from services.desktop.browser.tests.test_browser_mcp_daemon import (
    FakeUpstream,
    _daemon,
    _expire,
    _new_daemon,
    _restart_with_drift,
    _stamp_idle,
)


async def test_handle_client_renew_page_wire_roundtrip() -> None:
    """Wire-level: renew_page flows through the agent-keyed call path and its
    result serializes like any other CallToolResult."""
    import contextlib

    up = FakeUpstream()
    daemon_ref: list[ChromeMcpDaemon | None] = [_new_daemon(up)]
    sock_path = Path(tempfile.gettempdir()) / f"ava-bmd-{uuid4().hex}.sock"
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, daemon_ref), path=sock_path
    )

    async def roundtrip(payload: dict[str, Any]) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(path=sock_path)
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return json.loads(line)

    try:
        opened = await roundtrip(
            {
                "id": 1,
                "method": "call_tool",
                "tool": "new_page",
                "args": {"url": "x"},
                "agent_id": 7,
            }
        )
        assert opened["ok"] is True
        renewed = await roundtrip(
            {
                "id": 1,
                "method": "call_tool",
                "tool": "renew_page",
                "args": {"ttl": 120},
                "agent_id": 7,
            }
        )
        assert renewed["ok"] is True
        assert renewed["result"]["is_error"] is False
        assert "renewed" in renewed["result"]["content"][0]["text"]
    finally:
        server.close()
        await server.wait_closed()
        sock_path.unlink(missing_ok=True)


def test_new_generation_scopes_agent_page_entries() -> None:
    """The primitive: an entry is only visible to the generation that wrote it
    — a later generation reads the stale slot as absent."""
    pages = PageRegistry()
    g1 = pages.new_generation()
    pages.set_agent_page(7, 5, g1)
    assert pages.get_agent_page(7, g1) == 5

    g2 = pages.new_generation()

    assert g2 > g1
    assert pages.get_agent_page(7, g2) is None


async def test_reconnect_affinity_never_repins_stale_id() -> None:
    """After an upstream restart the old id must not be re-pinned: pre-fix the
    surviving slot selects id 1 — which now names a foreign tab — and the call
    runs there; now the slot reads as no-page and the agent rebuilds through
    the existing cold-start path."""
    d1, _up1 = _daemon()
    await d1.call_tool_for_agent("new_page", {"url": "https://example.com/mine"}, 7)  # page 1

    d2, up2 = _restart_with_drift(d1.pages)
    assert 1 in up2.pages  # the reused id exists: a stale re-pin WOULD land somewhere

    res = await d2.call_tool_for_agent("take_snapshot", {}, 7)

    assert res.is_error  # no-page — not the foreign tab
    assert not [c for c in up2.calls if c[0] == "select_page"]
    assert up2.selected is None  # nothing was ever selected

    nav = await d2.call_tool_for_agent("navigate_page", {"url": "https://example.com/mine"}, 7)

    assert not nav.is_error
    assert up2.calls[-1][0] == "new_page"  # cold start, its own fresh tab
    assert d2.pages.get_agent_page(7, d2.generation) == 2


async def test_reconnect_ttl_sweep_never_closes_reused_id() -> None:
    """A TTL slot minted before the reconnect must not close whatever tab now
    holds that id: pre-fix the expired deadline kills the foreign tab; now the
    stale slot is dropped, untouched."""
    d1, _up1 = _daemon()
    await d1.call_tool_for_agent("new_page", {"url": "https://example.com/mine"}, 7)

    d2, up2 = _restart_with_drift(d1.pages)
    _expire(d1, 1)  # the pre-reconnect deadline has passed

    closed = await page_lifecycle.reap_expired_pages(d2)

    assert closed == 0
    assert not [c for c in up2.calls if c[0] == "close_page"]
    assert up2.pages == {1: "https://user.example.com/other-tab"}  # untouched
    assert 1 not in d1.pages.ttl_deadlines  # the stale slot is dropped


async def test_reconnect_renew_page_reads_stale_slot_as_no_page() -> None:
    """renew_page across a reconnect: the pre-reconnect slot reads as no-page
    (open a page first), never renewed against the new process's unrelated id."""
    d1, _up1 = _daemon()
    await d1.call_tool_for_agent("new_page", {"url": "x"}, 7)

    d2, up2 = _restart_with_drift(d1.pages)
    res = await d2.call_tool_for_agent("renew_page", {"ttl": 60}, 7)

    assert res.is_error
    assert "no current page" in _text_of(res)
    assert up2.calls == []  # daemon-owned: nothing forwarded


async def test_reconnect_sweeps_drop_stale_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dead sweep drops entries from an earlier connection instead of
    treating them as candidates: the stale id is never probed or closed."""

    async def fake_probe(host: str, port: int) -> bool:
        return False  # everything dead

    monkeypatch.setattr("services.desktop.browser.page_lifecycle.port_listening", fake_probe)

    d1, _up1 = _daemon()
    await d1.call_tool_for_agent("new_page", {"url": "http://localhost:3111/x"}, 7)

    d2, up2 = _restart_with_drift(d1.pages)
    up2.calls.clear()

    await reap_dead_agent_pages(d2)

    assert d1.pages.affinity == {}  # the stale slot was dropped, not probed
    assert not [c for c in up2.calls if c[0] == "close_page"]


async def test_reconnect_idle_sweep_drops_stale_slot_without_closing() -> None:
    """The idle sweep reads a stale slot as no-page: no close, no stamp use —
    the entry is simply dropped."""
    d1, _up1 = _daemon()
    await d1.call_tool_for_agent("new_page", {"url": "https://example.com/x"}, 7)
    _stamp_idle(d1, 7, page_lifecycle._TAB_IDLE_TIMEOUT_S + 1)

    d2, up2 = _restart_with_drift(d1.pages)
    up2.calls.clear()

    closed = await page_lifecycle.reap_idle_agent_pages(d2)

    assert closed == 0
    assert [c[0] for c in up2.calls] == ["list_pages"]  # never a close
    assert d1.pages.affinity == {}


async def test_late_write_from_dead_connection_is_inert() -> None:
    """The race the generation closes structurally: a call resolved by the old
    connection can write its page slot after the new connection is installed —
    the write carries the dead generation, so no reader acts on it."""
    d1, _up1 = _daemon()
    d2, up2 = _restart_with_drift(d1.pages)

    d1.pages.set_agent_page(7, 1, d1.generation)  # the late write

    assert d2.pages.get_agent_page(7, d2.generation) is None

    res = await d2.call_tool_for_agent("take_snapshot", {}, 7)

    assert res.is_error
    assert not [c for c in up2.calls if c[0] == "select_page"]


async def test_handle_client_legacy_page_not_repinned_across_reconnect() -> None:
    """Wire-level, legacy path: a connection's OWN current page is stamped with
    the generation that resolved it, so after the upstream swap the same
    connection cold-starts instead of re-pinning the stale id."""
    import contextlib

    up1 = FakeUpstream()
    d1 = _new_daemon(up1)
    d2, up2 = _restart_with_drift(d1.pages)
    daemon_ref: list[ChromeMcpDaemon | None] = [d1]
    sock_path = Path(tempfile.gettempdir()) / f"ava-bmd-{uuid4().hex}.sock"
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, daemon_ref), path=sock_path
    )
    reader, writer = await asyncio.open_unix_connection(path=sock_path)

    async def send(payload: dict[str, Any]) -> dict[str, Any]:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        return json.loads(await reader.readline())

    try:
        opened = await send(
            {"id": 1, "method": "call_tool", "tool": "new_page", "args": {"url": "x"}}
        )
        assert opened["ok"] is True  # connection's page = 1, daemon 1's generation

        daemon_ref[0] = d2  # the upstream restarted in place

        blank = await send({"id": 2, "method": "call_tool", "tool": "take_snapshot", "args": {}})
        assert blank["ok"] is True
        assert blank["result"]["is_error"] is True  # no-page — not the foreign tab
        assert not [c for c in up2.calls if c[0] == "select_page"]
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        server.close()
        await server.wait_closed()
        sock_path.unlink(missing_ok=True)
