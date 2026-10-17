import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from mcp import types
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from base.cluster.machine.tests.gateway_stubs import gateway_api_base_stub
from services.desktop.browser import gateway_session, page_lifecycle, session
from services.desktop.browser.mcp_daemon import (
    ChromeMcpDaemon,
    handle_client,
    no_page_result,
    selected_id,
    text_of,
)
from services.desktop.browser.page_lifecycle import (
    PageRegistry,
    parse_page_listing,
)
from services.desktop.browser.tab_groups.grouping import AgentTabGroups


class FakeUpstream:
    def __init__(self) -> None:
        self.next_id = 1
        self.pages: dict[int, str] = {}
        self.selected: int | None = None
        self.isolated_contexts: dict[int, str] = {}
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
            context = args.get("isolatedContext")
            if isinstance(context, str) and context:
                self.isolated_contexts[pid] = context
            self.selected = pid
            return self._listing()
        if name == "select_page":
            pid = args["pageId"]
            if pid not in self.pages:
                return err("No page with that id")
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
        if name == "navigate_page":
            if self.selected is None:
                return err("No page selected")
            self.pages[self.selected] = args.get("url", "about:blank")
            return _ok(f"navigated page {self.selected}")
        if self.selected is None:
            return err("No page selected")
        return _ok(f"ran {name} on page {self.selected}")

    def _listing(self) -> types.CallToolResult:
        lines: list[str] = []
        for pid, url in self.pages.items():
            line = f"  {pid}: {url}"
            if pid == self.selected:
                line += " [selected]"
            context = self.isolated_contexts.get(pid)
            if context:
                line += f" isolatedContext={context}"
            lines.append(line)
        return _ok("\n".join(lines))


def _ok(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=False)


def err(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


class NoTabGroups(AgentTabGroups):
    def __init__(self) -> None:
        super().__init__(cdp_port=0, tasks=asyncio.TaskGroup())

    async def snapshot(self) -> frozenset[str] | None:
        return None

    async def group_new_page(self, agent_id: int, before: frozenset[str] | None) -> None:
        return None


def new_daemon(upstream: Any, tab_groups: AgentTabGroups | None = None) -> ChromeMcpDaemon:
    return ChromeMcpDaemon(upstream, PageRegistry(), tab_groups or NoTabGroups())


def browser_daemon_with_fake_upstream() -> tuple[ChromeMcpDaemon, FakeUpstream]:
    up = FakeUpstream()
    return new_daemon(up), up


async def test_two_connections_do_not_clobber() -> None:
    d, up = browser_daemon_with_fake_upstream()
    _, a_page = await d.call_tool("new_page", {"url": "a"}, None)
    _, b_page = await d.call_tool("new_page", {"url": "b"}, None)
    assert (a_page, b_page) == (1, 2)
    assert up.selected == 2

    res, a_page = await d.call_tool("take_snapshot", {}, a_page)
    assert text_of(res) == "ran take_snapshot on page 1"
    assert a_page == 1


async def test_navigate_cold_start_from_no_page() -> None:
    d, up = browser_daemon_with_fake_upstream()
    res, page = await d.call_tool("navigate_page", {"url": "x"}, None)
    assert not res.is_error
    assert page == 1
    assert [c[0] for c in up.calls] == ["new_page"]


async def test_page_scoped_no_current_refuses_to_touch_global() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool("new_page", {"url": "other"}, None)
    up.calls.clear()

    res, page = await d.call_tool("take_snapshot", {}, None)
    assert res.is_error and "No page selected" in text_of(res)
    assert page is None
    assert up.calls == []


async def test_select_and_close_track_current() -> None:
    d, _ = browser_daemon_with_fake_upstream()
    await d.call_tool("new_page", {"url": "a"}, None)
    _, page = await d.call_tool("new_page", {"url": "b"}, None)
    _, page = await d.call_tool("select_page", {"pageId": 1}, page)
    assert page == 1
    _, page = await d.call_tool("close_page", {"pageId": 2}, page)
    assert page == 1
    _, page = await d.call_tool("close_page", {"pageId": 1}, page)
    assert page is None


async def test_select_page_accepts_integral_float_page_id() -> None:
    d, _ = browser_daemon_with_fake_upstream()
    await d.call_tool("new_page", {"url": "a"}, None)
    _, page = await d.call_tool("new_page", {"url": "b"}, None)
    _, page = await d.call_tool("select_page", {"pageId": 1.0}, page)
    assert page == 1 and isinstance(page, int)


async def test_repin_failure_falls_back_to_no_page() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool("new_page", {"url": "real"}, None)
    res, page = await d.call_tool("take_snapshot", {}, 99)
    assert res.is_error and "No page selected" in text_of(res)
    assert page is None
    assert "take_snapshot" not in [c[0] for c in up.calls]


async def test_list_pages_is_not_repinned() -> None:
    d, up = browser_daemon_with_fake_upstream()
    _, page = await d.call_tool("new_page", {"url": "a"}, None)
    up.calls.clear()
    await d.call_tool("list_pages", {}, page)
    assert [c[0] for c in up.calls] == ["list_pages"]


def test_selected_id_parses_marked_line() -> None:
    assert selected_id(_ok("  1: https://a\n  2: https://b [selected]")) == 2
    assert selected_id(_ok("  1: https://a")) is None


def test_selected_id_parses_isolated_context_suffix() -> None:
    text = "  29: Discord (https://discord.com/app) [selected] isolatedContext=reg-7"
    assert selected_id(_ok(text)) == 29
    assert selected_id(_ok("  1: https://a\n  2: https://b isolatedContext=x")) is None


def test_selected_id_rejects_marker_inside_title() -> None:
    assert selected_id(_ok("  3: my [selected] title (https://url)")) is None
    line = "  3: my [selected] title (https://url) [selected] isolatedContext=reg-7"
    assert selected_id(_ok(line)) == 3


def test_next_page_select_normalizes_page_id() -> None:
    ok = _ok("  1: https://a")
    assert ChromeMcpDaemon._next_page("select_page", {"pageId": 1}, ok, 3) == 1
    assert ChromeMcpDaemon._next_page("select_page", {"pageId": 1.0}, ok, 3) == 1
    assert ChromeMcpDaemon._next_page("select_page", {"pageId": 1.5}, ok, 3) == 3
    assert ChromeMcpDaemon._next_page("select_page", {"pageId": True}, ok, 3) == 3
    assert ChromeMcpDaemon._next_page("select_page", {"pageId": "1"}, ok, 3) == 3


def test_no_page_result_is_marked_error() -> None:
    res = no_page_result()
    assert res.is_error and "No page selected" in text_of(res)


def test_text_of_ignores_non_text_blocks() -> None:
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="a"),
            types.ImageContent(type="image", data="b64==", mime_type="image/png"),
            types.TextContent(type="text", text="b"),
        ],
        is_error=False,
    )
    assert text_of(result) == "ab"


class DeadUpstream:
    async def list_tools(self) -> Any:
        raise anyio.ClosedResourceError

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        raise anyio.ClosedResourceError


async def test_call_tool_flags_dead_and_raises_on_upstream_death() -> None:
    d = new_daemon(DeadUpstream())
    with pytest.raises(RuntimeError, match="upstream session is down"):
        await d.call_tool("new_page", {}, None)
    assert d.dead.is_set()


async def test_list_tools_flags_dead_on_upstream_death() -> None:
    d = new_daemon(DeadUpstream())
    with pytest.raises(RuntimeError, match="upstream session is down"):
        await d.list_tools()
    assert d.dead.is_set()


class TimedOutUpstream:
    async def list_tools(self) -> Any:
        raise MCPError(code=REQUEST_TIMEOUT, message="Request 'tools/list' timed out")

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        raise MCPError(code=CONNECTION_CLOSED, message="Connection closed")


class OtherMCPErrorUpstream:
    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        raise MCPError(code=-32602, message="Invalid params")


async def test_call_tool_flags_dead_on_timeout_upstream() -> None:
    d = new_daemon(TimedOutUpstream())
    with pytest.raises(RuntimeError, match="upstream session is down"):
        await d.call_tool("navigate_page", {"url": "https://x.com"}, None)
    assert d.dead.is_set()


async def test_list_tools_flags_dead_on_timeout_upstream() -> None:
    d = new_daemon(TimedOutUpstream())
    with pytest.raises(RuntimeError, match="upstream session is down"):
        await d.list_tools()
    assert d.dead.is_set()


async def test_other_mcp_error_does_not_flag_dead() -> None:
    d = new_daemon(OtherMCPErrorUpstream())
    with pytest.raises(MCPError):
        await d.call_tool("list_pages", {}, None)
    assert not d.dead.is_set()


def _short_sock_dir() -> tuple[Any, Any, Any]:
    import shutil
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="meiva-mcp-", dir="/tmp"))
    sock = d / "mcp.sock"
    return d, sock, shutil.rmtree


async def test_socket_in_use_false_when_nobody_listens() -> None:
    from services.desktop.browser import mcp_daemon

    d, sock, cleanup = _short_sock_dir()
    try:
        assert await mcp_daemon.socket_in_use(sock) is False
    finally:
        cleanup(d)


async def test_socket_in_use_true_when_listener_present() -> None:
    from services.desktop.browser import mcp_daemon

    d, sock, cleanup = _short_sock_dir()
    server = await asyncio.start_unix_server(lambda _r, w: w.close(), path=str(sock))
    try:
        assert await mcp_daemon.socket_in_use(sock) is True
    finally:
        server.close()
        await server.wait_closed()
        cleanup(d)


async def test_agent_affinity_shares_page_across_connections() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    assert d.pages.get_agent_page(7) == 1
    res = await d.call_tool_for_agent("take_snapshot", {}, 7)
    assert res.is_error is False
    assert ("select_page", {"pageId": 1}, 1) in up.calls
    assert up.calls[-1] == ("take_snapshot", {}, 1)


async def test_agent_affinity_isolated_between_agents() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "b"}, 8)
    res = await d.call_tool_for_agent("take_snapshot", {}, 7)
    assert res.is_error is False
    assert up.calls[-1] == ("take_snapshot", {}, 1)


async def test_agent_affinity_repin_failure_drops_slot() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    up.pages.clear()
    up.selected = None
    res = await d.call_tool_for_agent("take_snapshot", {}, 7)
    assert res.is_error is True
    assert d.pages.get_agent_page(7) is None


async def test_agent_affinity_management_tools_update_slot() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "b"}, 7)
    assert d.pages.get_agent_page(7) == 2
    await d.call_tool_for_agent("select_page", {"pageId": 1}, 7)
    assert d.pages.get_agent_page(7) == 1
    await d.call_tool_for_agent("close_page", {"pageId": 1}, 7)
    assert d.pages.get_agent_page(7) is None


async def test_agent_affinity_accepts_integral_float_page_id() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "b"}, 7)
    await d.call_tool_for_agent("select_page", {"pageId": 1.0}, 7)
    assert d.pages.get_agent_page(7) == 1


async def test_agent_affinity_isolated_context_suffix_registers_page() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent(
        "new_page", {"url": "https://discord.com/app", "isolatedContext": "reg-7"}, 7
    )
    assert d.pages.get_agent_page(7) == 1
    assert d.pages.ttl_deadlines[1] > time.monotonic()


async def test_agent_close_with_integral_float_drops_ttl_slot() -> None:
    d, _up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "b"}, 7)
    assert 2 in d.pages.ttl_deadlines
    await d.call_tool_for_agent("close_page", {"pageId": 2.0}, 7)
    assert 2 not in d.pages.ttl_deadlines


async def test_handle_client_agent_id_adopts_agent_page(tmp_path: Path) -> None:
    import contextlib

    up = FakeUpstream()
    daemon_ref: list[ChromeMcpDaemon | None] = [new_daemon(up)]
    sock_path = Path(tempfile.gettempdir()) / f"meiva-bmd-{uuid4().hex}.sock"
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, daemon_ref), path=sock_path
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


async def test_release_agent_page_closes_only_that_agent() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "a1"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "a2"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "b"}, 8)
    up.calls.clear()

    page_id = await page_lifecycle.release_agent_page(d, 7)

    assert page_id == 2
    assert ("close_page", {"pageId": 1}, 3) in up.calls
    assert ("close_page", {"pageId": 2}, 3) in up.calls
    assert 1 not in up.pages and 2 not in up.pages
    assert up.pages == {3: "b"}
    assert d.pages.get_agent_page(7) is None
    assert d.pages.get_agent_page(8) == 3


async def test_release_agent_page_idempotent_and_no_slot() -> None:
    d, up = browser_daemon_with_fake_upstream()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    up.calls.clear()

    assert await page_lifecycle.release_agent_page(d, 7) == 1
    assert await page_lifecycle.release_agent_page(d, 7) is None
    assert await page_lifecycle.release_agent_page(d, 99) is None
    assert [c[0] for c in up.calls] == ["close_page"]


async def test_release_agent_page_keeps_slot_when_upstream_down() -> None:
    d = new_daemon(DeadUpstream())
    d.pages.set_agent_page(7, 1)
    d.pages.register_created_page(1)
    with pytest.raises(RuntimeError, match="upstream session is down"):
        await page_lifecycle.release_agent_page(d, 7)
    assert d.pages.get_agent_page(7) == 1


async def test_release_agent_page_wire_method() -> None:
    import contextlib

    up = FakeUpstream()
    daemon_ref: list[ChromeMcpDaemon | None] = [new_daemon(up)]
    sock_path = Path(tempfile.gettempdir()) / f"meiva-bmd-{uuid4().hex}.sock"
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, daemon_ref), path=sock_path
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
    released_daemon = daemon_ref[0]
    assert released_daemon is not None
    assert released_daemon.pages.get_agent_page(7) is None

    bad = await roundtrip({"id": 3, "method": "release_agent_page"})
    assert bad["ok"] is False
    assert "agent_id" in bad["error"]

    server.close()
    await server.wait_closed()
    sock_path.unlink(missing_ok=True)


def test_parse_page_listing_extracts_urls() -> None:
    text = "  1: http://localhost:3112/memory/graph\n  2: http://a [selected]\n   3: about:blank"
    assert parse_page_listing(text) == {
        1: "http://localhost:3112/memory/graph",
        2: "http://a",
        3: "about:blank",
    }
    assert parse_page_listing("") == {}


def expire(d: ChromeMcpDaemon, page_id: int) -> None:
    assert page_id in d.pages.ttl_deadlines
    d.pages.ttl_deadlines[page_id] = time.monotonic() - 1.0


def record_emits(events: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> Any:

    def record(*args: Any, **kwargs: Any) -> None:
        events.append((args, kwargs))

    return record


def ttl_remaining(d: ChromeMcpDaemon, page_id: int) -> float:
    return d.pages.ttl_deadlines[page_id] - time.monotonic()


class TitledListing(FakeUpstream):
    def __init__(self) -> None:
        super().__init__()
        self.isolated: dict[int, str] = {}

    def _listing(self) -> types.CallToolResult:
        lines: list[str] = []
        for pid, url in self.pages.items():
            line = f"  {pid}: Page {pid} ({url})"
            if pid == self.selected:
                line += " [selected]"
            if pid in self.isolated:
                line += f" isolatedContext={self.isolated[pid]}"
            lines.append(line)
        return _ok("\n".join(lines))


async def test_gateway_origin_navigation_is_an_ordinary_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = "http://gateway.example:8000"

    async def forbidden(*_args: Any) -> Any:
        pytest.fail("a gateway-origin navigation must not touch the session cookie")

    monkeypatch.setattr(gateway_session, "gateway_api_base", gateway_api_base_stub(gateway))
    monkeypatch.setattr(gateway_session, "inject_session_cookie", forbidden)
    monkeypatch.setattr(session, "gateway_session_login", forbidden)
    d, up = browser_daemon_with_fake_upstream()

    opened = await d.call_tool_for_agent("navigate_page", {"url": f"{gateway}/pages/1"}, 7)
    again = await d.call_tool_for_agent("navigate_page", {"url": f"{gateway}/pages/2"}, 7)
    await asyncio.sleep(0)

    assert not opened.is_error and not again.is_error
    assert [call[0] for call in up.calls if call[0] != "select_page"] == [
        "new_page",
        "navigate_page",
    ]


__all__ = ["TitledListing"]
