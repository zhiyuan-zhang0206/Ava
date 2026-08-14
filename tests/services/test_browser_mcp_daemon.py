"""Unit tests for the shared chrome MCP daemon's per-connection page affinity.

The upstream chrome-devtools-mcp session is a fake that tracks one global
"selected page" (exactly the upstream state the daemon must keep clients from
clobbering). These verify the daemon re-pins each connection to its own page,
cold-starts a page-less navigate, and refuses to forward a page-scoped call from
a connection that owns no page (which would hit another client's tab). The
socket wiring is left to dev-cluster testing.
"""

import asyncio
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import types
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from services.browser.mcp_daemon import (
    ChromeMcpDaemon,
    _no_page_result,
    _selected_id,
    _text_of,
)


class FakeUpstream:
    """One global selected page, like the real upstream. Records every call so a
    test can assert which page a page-scoped op actually ran against."""

    def __init__(self) -> None:
        self.next_id = 1
        self.pages: dict[int, str] = {}
        self.selected: int | None = None
        self.calls: list[tuple[str, dict[str, Any], int | None]] = []

    async def list_tools(self) -> Any:
        tool = types.Tool(name="take_snapshot", description="", input_schema={})
        return type("R", (), {"tools": [tool]})()

    async def call_tool(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        self.calls.append((name, dict(args), self.selected))
        if name == "new_page":
            pid = self.next_id
            self.next_id += 1
            self.pages[pid] = args.get("url", "about:blank")
            self.selected = pid
            return self._listing()
        if name == "select_page":
            pid = args["pageId"]
            if pid not in self.pages:
                return _err("No page with that id")
            self.selected = pid
            return self._listing()
        if name == "close_page":
            pid = args["pageId"]
            self.pages.pop(pid, None)
            if self.selected == pid:
                self.selected = None
            return self._listing()
        if name == "list_pages":
            return self._listing()
        # page-scoped op: acts on whatever is globally selected
        if self.selected is None:
            return _err("No page selected")
        return _ok(f"ran {name} on page {self.selected}")

    def _listing(self) -> types.CallToolResult:
        lines = [
            f"  {pid}: {url}{' [selected]' if pid == self.selected else ''}"
            for pid, url in self.pages.items()
        ]
        return _ok("\n".join(lines))


def _ok(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=False)


def _err(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def _daemon() -> tuple[ChromeMcpDaemon, FakeUpstream]:
    up = FakeUpstream()
    return ChromeMcpDaemon(up), up  # type: ignore[arg-type]


async def test_two_connections_do_not_clobber() -> None:
    """A's op lands on A's page even though B selected its own page last."""
    d, up = _daemon()
    _, a_page = await d.call_tool("new_page", {"url": "a"}, None)
    _, b_page = await d.call_tool("new_page", {"url": "b"}, None)
    assert (a_page, b_page) == (1, 2)
    assert up.selected == 2  # B selected last

    res, a_page = await d.call_tool("take_snapshot", {}, a_page)
    assert _text_of(res) == "ran take_snapshot on page 1"  # re-pinned to A, not B
    assert a_page == 1


async def test_navigate_cold_start_from_no_page() -> None:
    """navigate_page from a connection with no page opens one (new_page)."""
    d, up = _daemon()
    res, page = await d.call_tool("navigate_page", {"url": "x"}, None)
    assert not res.is_error
    assert page == 1
    assert [c[0] for c in up.calls] == ["new_page"]  # never forwarded as navigate_page


async def test_page_scoped_no_current_refuses_to_touch_global() -> None:
    """A page-scoped op from a page-less connection returns the no-page error and
    does NOT reach the upstream (forwarding would hit another client's tab)."""
    d, up = _daemon()
    await d.call_tool("new_page", {"url": "other"}, None)  # some OTHER connection's tab
    up.calls.clear()

    res, page = await d.call_tool("take_snapshot", {}, None)
    assert res.is_error and "No page selected" in _text_of(res)
    assert page is None
    assert up.calls == []  # upstream never touched


async def test_select_and_close_track_current() -> None:
    d, _ = _daemon()
    await d.call_tool("new_page", {"url": "a"}, None)  # page 1
    _, page = await d.call_tool("new_page", {"url": "b"}, None)  # page 2
    _, page = await d.call_tool("select_page", {"pageId": 1}, page)
    assert page == 1
    _, page = await d.call_tool("close_page", {"pageId": 2}, page)
    assert page == 1  # closing a different page leaves current
    _, page = await d.call_tool("close_page", {"pageId": 1}, page)
    assert page is None  # closing our own clears it


async def test_repin_failure_falls_back_to_no_page() -> None:
    """If the pinned page vanished, re-pin errors -> treat as page-less rather
    than run the op against whatever is globally selected."""
    d, up = _daemon()
    await d.call_tool("new_page", {"url": "real"}, None)  # page 1, selected
    res, page = await d.call_tool("take_snapshot", {}, 99)  # 99 never existed
    assert res.is_error and "No page selected" in _text_of(res)
    assert page is None
    assert "take_snapshot" not in [c[0] for c in up.calls]  # op never ran


async def test_list_pages_is_not_repinned() -> None:
    """Management tools forward without an injected select_page."""
    d, up = _daemon()
    _, page = await d.call_tool("new_page", {"url": "a"}, None)
    up.calls.clear()
    await d.call_tool("list_pages", {}, page)
    assert [c[0] for c in up.calls] == ["list_pages"]  # no select_page injected


def test_selected_id_parses_marked_line() -> None:
    assert _selected_id(_ok("  1: https://a\n  2: https://b [selected]")) == 2
    assert _selected_id(_ok("  1: https://a")) is None


def test_no_page_result_is_marked_error() -> None:
    res = _no_page_result()
    assert res.is_error and "No page selected" in _text_of(res)


def test_text_of_ignores_non_text_blocks() -> None:
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="a"),
            types.ImageContent(type="image", data="b64==", mime_type="image/png"),
            types.TextContent(type="text", text="b"),
        ],
        is_error=False,
    )
    assert _text_of(result) == "ab"


class DeadUpstream:
    """An upstream whose session has closed: every call raises the transport's
    closed-resource error, the way the real ClientSession does after the
    chrome-devtools-mcp subprocess dies."""

    async def list_tools(self) -> Any:
        raise anyio.ClosedResourceError

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        raise anyio.ClosedResourceError


async def test_call_tool_flags_dead_and_raises_on_upstream_death() -> None:
    """A closed upstream session sets `dead` (so run() exits for respawn) and
    raises a readable error, not an opaque empty-message transport exception."""
    d = ChromeMcpDaemon(DeadUpstream())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="upstream session is down"):
        await d.call_tool("new_page", {}, None)
    assert d.dead.is_set()


async def test_list_tools_flags_dead_on_upstream_death() -> None:
    d = ChromeMcpDaemon(DeadUpstream())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="upstream session is down"):
        await d.list_tools()
    assert d.dead.is_set()


class TimedOutUpstream:
    """An upstream that wedged (V8-crashed / assert-failed but stdio half-open):
    the mcp SDK surfaces the missing reply as MCPError(REQUEST_TIMEOUT), NOT a
    transport-level anyio exception (2026-08-06 #899 — this was the gap that
    left the daemon half-alive: ping answered, cache served, dead never set)."""

    async def list_tools(self) -> Any:
        raise MCPError(code=REQUEST_TIMEOUT, message="Request 'tools/list' timed out")

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        raise MCPError(code=CONNECTION_CLOSED, message="Connection closed")


class OtherMCPErrorUpstream:
    """An MCPError that is NOT upstream death (e.g. a tool-level error response)
    must propagate raw without flagging the session dead."""

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        raise MCPError(code=-32602, message="Invalid params")


async def test_call_tool_flags_dead_on_timeout_upstream() -> None:
    """MCPError(REQUEST_TIMEOUT / CONNECTION_CLOSED) from the SDK counts as
    upstream death: dead is set so run() reconnects a wedged upstream."""
    d = ChromeMcpDaemon(TimedOutUpstream())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="upstream session is down"):
        await d.call_tool("navigate_page", {"url": "https://x.com"}, None)
    assert d.dead.is_set()


async def test_list_tools_flags_dead_on_timeout_upstream() -> None:
    d = ChromeMcpDaemon(TimedOutUpstream())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="upstream session is down"):
        await d.list_tools()
    assert d.dead.is_set()


async def test_other_mcp_error_does_not_flag_dead() -> None:
    """A tool-level MCP error (wrong params etc.) is a normal failure: it must
    not kill the session or trigger a reconnect."""
    d = ChromeMcpDaemon(OtherMCPErrorUpstream())  # type: ignore[arg-type]
    with pytest.raises(MCPError):
        await d.call_tool("list_pages", {}, None)
    assert not d.dead.is_set()


# --- audit round 2: single-instance socket guard ---------------------------------
# pytest's tmp_path lives under /private/var/folders — longer than the
# AF_UNIX 104-byte path cap, so these tests use a short /tmp dir instead.


def _short_sock_dir() -> tuple[Any, Any, Any]:
    import shutil
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="ava-mcp-", dir="/tmp"))
    sock = d / "mcp.sock"
    return d, sock, shutil.rmtree


async def test_socket_in_use_false_when_nobody_listens() -> None:
    """A stale (or absent) socket is not "in use" — the daemon may unlink it."""
    from services.browser import mcp_daemon

    d, sock, cleanup = _short_sock_dir()
    try:
        assert await mcp_daemon._socket_in_use(sock) is False
    finally:
        cleanup(d)


async def test_socket_in_use_true_when_listener_present() -> None:
    """A socket with a live listener is "in use" — a second daemon must refuse
    to start instead of unlink-stealing it (audit round 2, P1)."""
    from services.browser import mcp_daemon

    d, sock, cleanup = _short_sock_dir()
    server = await asyncio.start_unix_server(lambda _r, w: w.close(), path=str(sock))
    try:
        assert await mcp_daemon._socket_in_use(sock) is True
    finally:
        server.close()
        await server.wait_closed()
        cleanup(d)
