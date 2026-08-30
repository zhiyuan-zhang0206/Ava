"""Unit tests for the shared chrome MCP daemon's per-connection page affinity.

The upstream chrome-devtools-mcp session is a fake that tracks one global
"selected page" (exactly the upstream state the daemon must keep clients from
clobbering). These verify the daemon re-pins each connection to its own page,
cold-starts a page-less navigate, and refuses to forward a page-scoped call from
a connection that owns no page (which would hit another client's tab). The
socket wiring is left to dev-cluster testing.
"""

import asyncio
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from mcp import types
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from services.browser import page_lifecycle
from services.browser.mcp_daemon import (
    _AGENT_AFFINITY,
    ChromeMcpDaemon,
    _handle_client,
    _no_page_result,
    _selected_id,
    _text_of,
)
from services.browser.page_lifecycle import (
    local_host_port,
    parse_page_listing,
    port_listening,
    reap_dead_agent_pages,
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


# ── Per-agent affinity (agent_id keyed — exec subprocess children) ─────────


@pytest.fixture(autouse=True)
def _fresh_agent_affinity() -> Iterator[None]:
    """The per-agent registry is module-level (must survive upstream
    reconnects); a test session outlives every test, so clear it around each
    one the way the production process would start fresh."""
    _AGENT_AFFINITY.clear()
    yield
    _AGENT_AFFINITY.clear()


async def test_agent_affinity_shares_page_across_connections() -> None:
    """Agent 7's page selection survives a fresh connection: the second
    connection resolves the page from the per-agent registry, not from any
    per-connection tracking — the exec-subprocess shape."""
    d, up = _daemon()
    # connection 1: the agent opens its tab
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    assert _AGENT_AFFINITY[7] == 1
    # connection 2 (fresh): a page-scoped call lands on the agent's page
    res = await d.call_tool_for_agent("take_snapshot", {}, 7)
    assert res.is_error is False
    # the re-pin targeted the agent's page, then the call ran there
    assert ("select_page", {"pageId": 1}, 1) in up.calls
    assert up.calls[-1] == ("take_snapshot", {}, 1)


async def test_agent_affinity_isolated_between_agents() -> None:
    """Two agents keep separate pages: agent 7's snapshot re-pins to 7's tab
    even though agent 8's tab is the globally selected one."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)  # page 1
    await d.call_tool_for_agent("new_page", {"url": "b"}, 8)  # page 2, global
    res = await d.call_tool_for_agent("take_snapshot", {}, 7)
    assert res.is_error is False
    assert up.calls[-1] == ("take_snapshot", {}, 1)


async def test_agent_affinity_repin_failure_drops_slot() -> None:
    """The agent's tab vanished underneath (crashed / closed elsewhere): the
    slot drops to no-page, and the call refuses to touch the global selection
    (which could be another agent's tab)."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    up.pages.clear()
    up.selected = None
    res = await d.call_tool_for_agent("take_snapshot", {}, 7)
    assert res.is_error is True
    assert _AGENT_AFFINITY[7] is None


async def test_agent_affinity_management_tools_update_slot() -> None:
    """new_page / select_page / close_page update the per-agent slot exactly
    like the per-connection path updates its local."""
    d, _up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)  # -> page 1
    await d.call_tool_for_agent("new_page", {"url": "b"}, 7)  # -> page 2
    assert _AGENT_AFFINITY[7] == 2
    await d.call_tool_for_agent("select_page", {"pageId": 1}, 7)
    assert _AGENT_AFFINITY[7] == 1
    await d.call_tool_for_agent("close_page", {"pageId": 1}, 7)
    assert _AGENT_AFFINITY[7] is None


async def test_handle_client_agent_id_adopts_agent_page(tmp_path: Path) -> None:
    """Wire-level: a FRESH connection presenting the agent id inherits the
    page a previous connection selected — exactly what an exec subprocess
    child needs to not re-select on every execute_code."""
    import contextlib

    up = FakeUpstream()
    daemon_ref: list[ChromeMcpDaemon | None] = [ChromeMcpDaemon(up)]  # type: ignore[arg-type]
    # macOS unix sockets cap at ~104-char paths; pytest tmp dirs blow past it.
    sock_path = Path(tempfile.gettempdir()) / f"ava-bmd-{uuid4().hex}.sock"
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, daemon_ref), path=sock_path
    )
    assert sock_path.exists()

    async def roundtrip(payload: dict[str, Any]) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(path=sock_path)
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return json.loads(line)

    resp1 = await roundtrip(
        {
            "id": 1,
            "method": "call_tool",
            "tool": "new_page",
            "args": {"url": "a"},
            "agent_id": 7,
        }
    )
    assert resp1["ok"] is True
    # a new connection, same agent: page-scoped call lands on page 1
    resp2 = await roundtrip(
        {
            "id": 1,
            "method": "call_tool",
            "tool": "take_snapshot",
            "args": {},
            "agent_id": 7,
        }
    )
    assert resp2["ok"] is True
    text = resp2["result"]["content"][0]["text"]
    assert "page 1" in text

    server.close()
    await server.wait_closed()
    sock_path.unlink(missing_ok=True)
    server.close()
    await server.wait_closed()
    sock_path.unlink(missing_ok=True)


# ── Agent-terminate page release (process-exit hook) ─────────────────────


async def test_release_agent_page_closes_only_that_agent() -> None:
    """Releasing agent 7 closes exactly 7's page — agent 8's tab survives even
    though it is the upstream's current global selection; 7's slot is cleared."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)  # page 1
    await d.call_tool_for_agent("new_page", {"url": "b"}, 8)  # page 2, selected
    up.calls.clear()

    page_id = await page_lifecycle.release_agent_page(d, 7)

    assert page_id == 1
    assert ("close_page", {"pageId": 1}, 2) in up.calls
    assert 1 not in up.pages
    assert up.pages == {2: "b"}  # the other agent's tab untouched
    assert _AGENT_AFFINITY[7] is None
    assert _AGENT_AFFINITY[8] == 2


async def test_release_agent_page_idempotent_and_no_slot() -> None:
    """A second release is a no-op (slot already cleared) and an agent that
    never opened a page is a no-op — no second close_page reaches upstream."""
    d, up = _daemon()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    up.calls.clear()

    assert await page_lifecycle.release_agent_page(d, 7) == 1
    assert await page_lifecycle.release_agent_page(d, 7) is None
    assert await page_lifecycle.release_agent_page(d, 99) is None
    assert [c[0] for c in up.calls] == ["close_page"]  # exactly one close


async def test_release_agent_page_keeps_slot_when_upstream_down() -> None:
    """The upstream dying mid-release must not clear the slot — the page is
    still open and the reaper needs to find it after the reconnect."""
    d = ChromeMcpDaemon(DeadUpstream())  # type: ignore[arg-type]
    _AGENT_AFFINITY[7] = 1
    with pytest.raises(RuntimeError, match="upstream session is down"):
        await page_lifecycle.release_agent_page(d, 7)
    assert _AGENT_AFFINITY[7] == 1  # slot survives for the reaper


async def test_release_agent_page_wire_method() -> None:
    """Wire-level: an agent's exit hook sends `release_agent_page`; the daemon
    closes that agent's page and answers with the released page id. A missing
    agent id is rejected at the protocol edge."""
    import contextlib

    up = FakeUpstream()
    daemon_ref: list[ChromeMcpDaemon | None] = [ChromeMcpDaemon(up)]  # type: ignore[arg-type]
    sock_path = Path(tempfile.gettempdir()) / f"ava-bmd-{uuid4().hex}.sock"
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, daemon_ref), path=sock_path
    )
    assert sock_path.exists()

    async def roundtrip(payload: dict[str, Any]) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(path=sock_path)
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return json.loads(line)

    await roundtrip(
        {"id": 1, "method": "call_tool", "tool": "new_page", "args": {"url": "a"}, "agent_id": 7}
    )
    resp = await roundtrip({"id": 2, "method": "release_agent_page", "agent_id": 7})
    assert resp["ok"] is True
    assert resp["result"]["page_id"] == 1
    assert _AGENT_AFFINITY[7] is None

    bad = await roundtrip({"id": 3, "method": "release_agent_page"})
    assert bad["ok"] is False
    assert "agent_id" in bad["error"]

    server.close()
    await server.wait_closed()
    sock_path.unlink(missing_ok=True)


# ── Dead-page reaper (dead localhost port) ──────────────────────────────


def test_parse_page_listing_extracts_urls() -> None:
    text = "  1: http://localhost:3112/memory/graph\n  2: http://a [selected]\n   3: about:blank"
    assert parse_page_listing(text) == {
        1: "http://localhost:3112/memory/graph",
        2: "http://a",
        3: "about:blank",
    }
    assert parse_page_listing("") == {}


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

    monkeypatch.setattr("services.browser.page_lifecycle.port_listening", fake_probe)

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
    assert _AGENT_AFFINITY[7] is None
    assert _AGENT_AFFINITY[8] == 2
    assert _AGENT_AFFINITY[9] == 3
    assert _AGENT_AFFINITY[10] == 4
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

    monkeypatch.setattr("services.browser.page_lifecycle.port_listening", fake_probe)

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
    assert _AGENT_AFFINITY == {7: None, 8: None}
