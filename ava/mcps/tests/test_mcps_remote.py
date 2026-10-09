"""ava.mcps remote client and _connect branches: the Unix-socket remote client, remote/cache routing,
and the _connect config branches; split from ava/mcps/tests/test_mcps.py (task #4922)."""

import inspect
import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import ava.mcps as mcps_mod
import ava.mcps._remote as remote_mod
from ava.mcps._clients import McpClients
from ava.mcps.tests._mcps_helpers import _content_text, _make_tool, local_mcp_clients
from ava.mcps.tests._mcps_helpers import fake_config as fake_config
from ava.mcps.tests._mcps_helpers import mock_session as mock_session
from base.config import settings

# ─── _RemoteMCPClient (mock Unix socket) ───────────────────────────────────


class _FakeSocket:
    """Simulate Unix socket — feeds data from the recv_queue injected per test."""

    def __init__(self) -> None:
        self.timeout: float | None = None
        self.timeouts: list[float] = []
        self.connected_to: str | None = None
        self.sent: list[bytes] = []
        self.recv_queue: list[bytes | BaseException] = []
        self.closed = False

    def settimeout(self, s: float) -> None:
        self.timeout = s
        self.timeouts.append(s)

    def connect(self, path: str) -> None:
        self.connected_to = path

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        if not self.recv_queue:
            raise OSError("test queue exhausted")
        item = self.recv_queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def _patch_socket(monkeypatch: pytest.MonkeyPatch, sock: _FakeSocket) -> None:
    """Replace socket.socket(...) to return our fake."""
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: sock)  # pyright: ignore[reportUnknownArgumentType]


@pytest.mark.parametrize("method", ["list_tools", "call_tool"])
def test_remote_client_uses_request_timeout_for_dial_and_response(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    result: list[dict[str, Any]] | dict[str, Any] = (
        [] if method == "list_tools" else {"content": [], "isError": False}
    )
    sock = _FakeSocket()
    sock.recv_queue = [json.dumps({"id": 1, "ok": True, "result": result}).encode() + b"\n"]
    _patch_socket(monkeypatch, sock)
    monkeypatch.setattr(remote_mod.time, "time", lambda: 100.0)
    client = remote_mod.connect_remote("fake-socket-path")

    if method == "list_tools":
        assert client.list_tools("srv", timeout_seconds=7.5) == result
    else:
        assert client.call_tool("srv", "t", {}, timeout_seconds=7.5) == result

    assert sock.timeouts == [7.5, 7.5]


def test_remote_client_reuses_socket_with_each_requests_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"id": 1, "ok": True, "result": []}).encode() + b"\n",
        json.dumps({"id": 2, "ok": True, "result": []}).encode() + b"\n",
    ]
    _patch_socket(monkeypatch, sock)
    monkeypatch.setattr(remote_mod.time, "time", lambda: 100.0)
    client = remote_mod.connect_remote("fake-socket-path")

    assert client.list_tools("srv", timeout_seconds=7.5) == []
    assert client.list_tools("srv", timeout_seconds=2.0) == []

    assert sock.timeouts == [7.5, 7.5, 2.0]
    assert sock.closed is False


def test_remote_client_foreign_responses_do_not_reset_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = _FakeSocket()
    sock.recv_queue = [json.dumps({"id": 9, "ok": True, "result": []}).encode() + b"\n"]
    _patch_socket(monkeypatch, sock)
    clock = iter([100.0, 100.0, 102.0])
    monkeypatch.setattr(remote_mod.time, "time", lambda: next(clock))
    client = remote_mod.connect_remote("fake-socket-path")

    with pytest.raises(mcps_mod.MCPConnectError, match="timeout"):
        client.list_tools("srv", timeout_seconds=2.0)

    assert sock.timeouts == [2.0, 2.0]
    assert sock.closed is True


def test_remote_client_list_tools_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps(
            {
                "id": 1,
                "ok": True,
                "result": [{"name": "t1", "description": "d1", "input_schema": {"type": "object"}}],
            }
        ).encode()
        + b"\n"
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    tools = client.list_tools("srv", timeout_seconds=5.0)

    assert tools == [{"name": "t1", "description": "d1", "input_schema": {"type": "object"}}]
    assert sock.connected_to == "fake-socket-path"
    sent_req = json.loads(sock.sent[0].decode().rstrip())
    assert sent_req["method"] == "list_tools"
    assert sent_req["params"] == {"server": "srv"}
    assert sent_req["id"] == 1


def test_remote_client_call_tool_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps(
            {
                "id": 1,
                "ok": True,
                "result": {"content": [], "isError": False, "structuredContent": None},
            }
        ).encode()
        + b"\n"
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    out = client.call_tool("srv", "tool_x", {"k": "v"}, timeout_seconds=5.0)

    assert out == {"content": [], "isError": False, "structuredContent": None}
    sent_req = json.loads(sock.sent[0].decode().rstrip())
    assert sent_req["method"] == "call_tool"
    assert sent_req["params"] == {"server": "srv", "tool": "tool_x", "args": {"k": "v"}}


def test_remote_client_raises_on_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """daemon returns `ok: False` → MCPCallError, error field as message."""
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"id": 1, "ok": False, "error": "permission denied"}).encode() + b"\n"
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPCallError, match="permission denied"):
        client.list_tools("srv", timeout_seconds=5.0)


def test_remote_client_raises_when_connection_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """recv returns empty chunk → peer closed connection → MCPConnectError."""
    sock = _FakeSocket()
    sock.recv_queue = [b""]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPConnectError, match="connection closed"):
        client.list_tools("srv", timeout_seconds=5.0)


def test_remote_client_raises_on_recv_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """sock.recv raises TimeoutError → translates to MCPConnectError for the upper layer."""
    sock = _FakeSocket()
    sock.recv_queue = [TimeoutError()]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPConnectError, match="timeout"):
        client.list_tools("srv", timeout_seconds=5.0)


def test_remote_client_accumulates_chunked_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """response arrives in two parts (TCP-style) → concatenated until \n then parsed."""
    payload = json.dumps({"id": 1, "ok": True, "result": []}).encode() + b"\n"
    sock = _FakeSocket()
    sock.recv_queue = [payload[:10], payload[10:]]  # split in half
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    assert client.list_tools("srv", timeout_seconds=5.0) == []


def test_remote_client_request_ids_increment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two consecutive _request calls → id increments (used by daemon side to match request/response)."""
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"id": 1, "ok": True, "result": []}).encode() + b"\n",
        json.dumps({"id": 2, "ok": True, "result": []}).encode() + b"\n",
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    client.list_tools("srv", timeout_seconds=5.0)
    client.list_tools("srv", timeout_seconds=5.0)
    id1 = json.loads(sock.sent[0].decode().rstrip())["id"]
    id2 = json.loads(sock.sent[-1].decode().rstrip())["id"]
    assert (id1, id2) == (1, 2)


# --- response-id matching + close-on-failure (MCP cross-talk, Task #1147) ---


def test_remote_client_skips_stale_response_with_foreign_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale line — the daemon's late answer to a request whose client-side
    deadline already fired — carries the OLD request's id. The next request
    must skip it and wait for its own id, or every later response shifts by
    one (response stream permanently misaligned: request N+1 reads request
    N's result). The buffer models the state after a timed-out request 1:
    its response (id=1) arrived late, before request 2's own response."""
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"id": 1, "ok": True, "result": "stale"}).encode() + b"\n",
        json.dumps({"id": 2, "ok": True, "result": "fresh"}).encode() + b"\n",
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    # Model request 1 as already sent and timed out client-side: its id slot is
    # consumed and its late response is what sits at the head of the buffer.
    client._req_id = 1
    # this call sends id=2; it must NOT consume the id=1 stale line
    assert client.call_tool("srv", "t", {}, timeout_seconds=5.0) == "fresh"
    # the stale line was consumed and discarded, stream stays aligned
    assert sock.closed is False


def test_remote_client_skips_notification_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsolicited notification (no id) interleaved before the response must
    not be consumed as the response — the id match is the discriminator."""
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"method": "notifications/message", "params": {}}).encode() + b"\n",
        json.dumps({"id": 1, "ok": True, "result": "ok"}).encode() + b"\n",
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    assert client.call_tool("srv", "t", {}, timeout_seconds=5.0) == "ok"


def test_remote_client_skips_stale_and_notification_then_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both foreign-line classes together, in one buffer, before the matching
    response: the client scans until id matches (request id here is 1)."""
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"id": 7, "ok": True, "result": "very stale"}).encode() + b"\n",
        json.dumps({"method": "notifications/progress", "params": {}}).encode() + b"\n",
        json.dumps({"id": 1, "ok": True, "result": "mine"}).encode() + b"\n",
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    assert client.call_tool("srv", "t", {}, timeout_seconds=5.0) == "mine"


def test_remote_client_closes_socket_on_recv_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request-timeout leaves the stream ambiguous: the daemon may still be
    processing and its response may arrive later. Closing the socket (and
    forgetting it) means the next request reconnects fresh — a late response
    can never be consumed by the next request (the \u4e32\u8bdd enabler)."""
    sock = _FakeSocket()
    sock.recv_queue = [TimeoutError()]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPConnectError, match="timeout"):
        client.list_tools("srv", timeout_seconds=5.0)
    assert sock.closed is True
    assert client._sock is None, "next request must reconnect fresh"


def test_remote_client_closes_socket_when_connection_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOF mid-request: same ambiguous-stream argument — close and forget."""
    sock = _FakeSocket()
    sock.recv_queue = [b""]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPConnectError, match="connection closed"):
        client.list_tools("srv", timeout_seconds=5.0)
    assert sock.closed is True
    assert client._sock is None


def test_remote_client_raises_on_malformed_response_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated / garbage line is a stream-integrity failure: MCPConnectError
    (transport class, so the caller's local fallback engages) and the socket is
    closed for a fresh reconnect."""
    sock = _FakeSocket()
    sock.recv_queue = [b"not json at all\n"]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPConnectError, match="malformed"):
        client.list_tools("srv", timeout_seconds=5.0)
    assert sock.closed is True
    assert client._sock is None


def test_remote_client_error_response_keeps_socket_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `ok: False` response is a cleanly-consumed response — the stream stays
    aligned, so the connection is kept (no need to pay a reconnect)."""
    sock = _FakeSocket()
    sock.recv_queue = [json.dumps({"id": 1, "ok": False, "error": "nope"}).encode() + b"\n"]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPCallError, match="nope"):
        client.list_tools("srv", timeout_seconds=5.0)
    assert sock.closed is False
    assert client._sock is sock


# ─── _list_tools / _call_raw remote daemon path + cache fallback ─────────────


def test_list_tools_uses_remote_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """daemon available → directly use remote.list_tools, not reading cache / not connecting subprocess."""
    fake_remote = MagicMock()
    fake_remote.list_tools.return_value = [{"name": "t1", "description": "d", "input_schema": {}}]
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: fake_remote)

    for timeout in (7.5, 2.0):
        monkeypatch.setattr(settings.sandbox, "mcp_connect_timeout_seconds", timeout)
        tools = mcps_mod._list_tools("srv")
        assert tools == [{"name": "t1", "description": "d", "input_schema": {}}]
        assert fake_remote.list_tools.call_args.args == ("srv",)
        assert fake_remote.list_tools.call_args.kwargs == {"timeout_seconds": timeout}
    assert fake_remote.list_tools.call_count == 2


def test_list_tools_falls_back_to_cache_when_remote_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """daemon throws → falls back to disk cache."""
    fake_remote = MagicMock()
    fake_remote.list_tools.side_effect = OSError("daemon down")
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: fake_remote)

    cached: list[mcps_mod.ToolInfo] = [
        {"name": "cached_tool", "description": "", "input_schema": {}}
    ]
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: cached)  # pyright: ignore[reportUnknownArgumentType]

    tools = mcps_mod._list_tools("srv")
    assert tools == cached


def test_call_raw_uses_remote_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_remote = MagicMock()
    fake_remote.call_tool.return_value = {
        "content": [{"type": "text", "text": "ok"}],
        "isError": False,
        "structuredContent": None,
    }
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: fake_remote)

    for timeout in (7.5, 2.0):
        monkeypatch.setattr(settings.sandbox, "mcp_connect_timeout_seconds", timeout)
        out = mcps_mod._call_raw("srv", "tool_x", arg="v")
        assert out["content"][0]["text"] == "ok"
        assert fake_remote.call_tool.call_args.args == ("srv", "tool_x", {"arg": "v"})
        assert fake_remote.call_tool.call_args.kwargs == {"timeout_seconds": timeout}
    assert fake_remote.call_tool.call_count == 2


def test_call_raw_falls_back_to_local_when_remote_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """daemon crashed → fallback to local _connect path."""
    fake_remote = MagicMock()
    fake_remote.call_tool.side_effect = OSError("daemon down")
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: fake_remote)

    # Use existing mock_session flavor patch
    fake_session = MagicMock()
    fake_session.call_tool = AsyncMock(
        return_value=MagicMock(
            content=[_content_text("ok")],
            is_error=False,
            structured_content=None,
        )
    )

    async def _fake_connect(_mcp: McpClients, _server: str, **_kw: object) -> MagicMock:
        return fake_session

    local_mcp_clients(monkeypatch)
    monkeypatch.setattr(mcps_mod, "_connect", _fake_connect)

    out = mcps_mod._call_raw("srv", "tool_x", k="v")
    assert out["content"][0]["text"] == "ok"


# ─── _connect bad-config branches (running real _connect, inside background loop) ───


def test_list_tools_raises_when_server_not_in_config(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_connect` sees server not in mcp.json → MCPServerNotFound leaks to _list_tools."""
    fake_config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    local_mcp_clients(monkeypatch)
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(mcps_mod.MCPServerNotFound, match="nope"):
        mcps_mod._list_tools("nope")


def test_list_tools_raises_when_command_field_invalid(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config missing the command field → MCPError immediately (fail-fast)."""
    fake_config.write_text(json.dumps({"mcpServers": {"bad": {}}}), encoding="utf-8")
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    local_mcp_clients(monkeypatch)
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(mcps_mod.MCPError, match="command"):
        mcps_mod._list_tools("bad")


def test_list_tools_raises_when_command_not_str(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_config.write_text(json.dumps({"mcpServers": {"bad": {"command": 123}}}), encoding="utf-8")
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    local_mcp_clients(monkeypatch)
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(mcps_mod.MCPError, match="command"):
        mcps_mod._list_tools("bad")


def test_list_tools_uses_url_for_remote_server(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `url` entry connects via the HTTP path, not the stdio child path."""
    fake_config.write_text(
        json.dumps({"mcpServers": {"remote": {"url": "https://mcp.example.com/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    mcp = local_mcp_clients(monkeypatch)
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(mcps_mod, "_write_cache", lambda _s, _t: None)  # pyright: ignore[reportUnknownArgumentType]

    tool = MagicMock(spec=["name", "description", "input_schema"])
    tool.name = "scrape"
    tool.description = "d"
    tool.input_schema = {"type": "object"}
    session = MagicMock(list_tools=AsyncMock(return_value=MagicMock(tools=[tool])))
    stack = MagicMock()
    connect_http = AsyncMock(return_value=(session, stack))
    monkeypatch.setattr(mcps_mod, "_connect_http", connect_http)

    tools = mcps_mod._list_tools("remote")

    connect_http.assert_awaited_once_with("https://mcp.example.com/mcp", None, server="remote")
    # The session is cached with the stack that owns its transport, so a dead one is closed on rebuild.
    assert mcp.sessions["remote"] is session
    assert mcp.session_stacks["remote"] is stack
    assert tools == [{"name": "scrape", "description": "d", "input_schema": {"type": "object"}}]


def test_connect_http_local_fallback_initializes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-fallback HTTP connect mirrors the daemon: transport -> session -> initialize."""
    read, write = object(), object()
    streams = MagicMock()
    streams.__aenter__ = AsyncMock(return_value=(read, write))
    streams.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=streams)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", factory)
    session = MagicMock()
    session.initialize = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    client_session_cls = MagicMock(return_value=session_cm)
    monkeypatch.setattr("mcp.ClientSession", client_session_cls)
    client_factory = MagicMock(return_value=object())
    monkeypatch.setattr("mcp.client.streamable_http.create_mcp_http_client", client_factory)

    got, stack = mcps_mod._run_async(
        mcps_mod._connect_http("https://mcp.example.com/mcp", {"x-api-key": "k"})
    )

    assert got is session
    assert isinstance(stack, mcps_mod.AsyncExitStack)
    session.initialize.assert_awaited_once()
    client_factory.assert_called_once_with(headers={"x-api-key": "k"})
    assert factory.call_args.kwargs["http_client"] is client_factory.return_value
    # every local-fallback request must be bounded by the same timeout knob —
    # the SDK default (None) would block the calling agent forever on a hung server.
    assert client_session_cls.call_args.kwargs["read_timeout_seconds"] == (
        settings.sandbox.mcp_connect_timeout_seconds
    )


def test_connect_http_local_fallback_timeout_raises_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _hang(*_a: Any, **_k: Any) -> Any:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError("slow endpoint"))
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client",
        MagicMock(return_value=_hang()),
    )

    with pytest.raises(mcps_mod.MCPConnectError, match="timed out"):
        mcps_mod._run_async(mcps_mod._connect_http("https://mcp.example.com/mcp", None))


def test_connect_stdio_sets_request_timeout_on_client_session(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local-fallback stdio session gets a per-request timeout — without it
    (SDK default None) a hung server blocks `fut.result()` forever."""
    fake_config.write_text(
        json.dumps({"mcpServers": {"fs": {"command": "uvx", "args": ["mcp-server-filesystem"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    mcp = local_mcp_clients(monkeypatch)

    read, write = object(), object()
    streams = MagicMock()
    streams.__aenter__ = AsyncMock(return_value=(read, write))
    streams.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("mcp.client.stdio.stdio_client", MagicMock(return_value=streams))
    session = MagicMock()
    session.initialize = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    client_session_cls = MagicMock(return_value=session_cm)
    monkeypatch.setattr("mcp.ClientSession", client_session_cls)

    got = mcps_mod._run_async(mcps_mod._connect(mcp, "fs"))

    assert got is session
    session.initialize.assert_awaited_once()
    assert client_session_cls.call_args.kwargs["read_timeout_seconds"] == (
        settings.sandbox.mcp_connect_timeout_seconds
    )


# ─── _dump_content fail-fast on unknown content type ──────────────────────


def test_dump_content_raises_on_unknown_block() -> None:
    """content block without model_dump → MCPError (fail-fast, not silently swallowed)."""

    class _Unknown:
        pass

    with pytest.raises(mcps_mod.MCPError, match="Unrecognized"):
        mcps_mod._dump_content(_Unknown())


# ─── help() ────────────────────────────────────────────────────────────────


def test_help_prints_empty_message_when_no_servers(
    fake_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mcps_mod.help()
    out = capsys.readouterr().out
    assert "no MCP servers configured" in out


def test_help_lists_each_configured_server(
    fake_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {"command": "uvx", "args": ["mcp-server-filesystem", "/some/path"]},
                    "chrome": {"command": "npx", "args": ["chrome-mcp@latest"]},
                }
            }
        ),
        encoding="utf-8",
    )
    mcps_mod.help()
    out = capsys.readouterr().out
    assert "fs/" in out
    assert "chrome/" in out
    assert "uvx" in out
    assert "npx" in out
    assert "mcp-server-filesystem" in out


# ─── _ServerProxy detail paths ────────────────────────────────────────────


def test_proxy_getattr_rejects_underscore_names(mock_session: MagicMock) -> None:
    """`proxy._anything` (dunder / private) goes through AttributeError — not treated as tool call."""
    proxy = mcps_mod._ServerProxy("fs")
    with pytest.raises(AttributeError):
        proxy._not_a_tool  # noqa: B018 — intentionally trigger __getattr__


def test_proxy_tool_docstring_includes_schema(mock_session: MagicMock) -> None:
    """tool has input_schema → docstring includes JSON schema (for agent to see)."""
    mock_session.list_tools.return_value = MagicMock(
        tools=[
            _make_tool(
                "read_file",
                "Read a file",
                schema={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ]
    )
    proxy = mcps_mod._ServerProxy("fs")
    fn = proxy.read_file
    doc = fn.__doc__ or ""
    assert "Input schema" in doc
    assert "path" in doc
    assert "string" in doc


def test_proxy_all_prefers_disk_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """`__all_for_ava__` hits disk cache then stops — should not trigger _connect (server startup is expensive)."""
    cached: list[mcps_mod.ToolInfo] = [
        {"name": "alpha", "description": "", "input_schema": {}},
        {"name": "beta", "description": "", "input_schema": {}},
    ]
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: cached)  # pyright: ignore[reportUnknownArgumentType]

    async def _fail_connect(*_a: object, **_kw: object) -> None:
        raise AssertionError("_connect should not be called when disk cache hits")

    monkeypatch.setattr(mcps_mod, "_connect", _fail_connect)

    proxy = mcps_mod._ServerProxy("fs")
    assert proxy.__all_for_ava__ == ["alpha", "beta"]


def test_proxy_load_tool_names_uses_in_memory_cache_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second call to _load_tool_names → directly reads self._tools_cache without hitting disk again."""
    calls = {"n": 0}

    def _spy_read_cache(_s: str) -> list[mcps_mod.ToolInfo]:
        calls["n"] += 1
        return [{"name": "t1", "description": "", "input_schema": {}}]

    monkeypatch.setattr(mcps_mod, "_read_cache", _spy_read_cache)

    proxy = mcps_mod._ServerProxy("fs")
    proxy._load_tool_names()  # first: disk cache fills _tools_cache
    proxy._load_tool_names()  # second: uses in-memory cache
    assert calls["n"] == 1  # disk read only once


# ─── _schema_to_signature (display-only, no validation) ───────────────────


def test_schema_to_signature_renders_required_and_optional() -> None:
    """Has properties → synthesizes keyword-only signature: required no default, optional default None,
    returns str. Purely display, no validation."""
    schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "timeout": {"type": "integer"}},
        "required": ["url"],
    }
    sig = mcps_mod._schema_to_signature(schema)
    assert sig is not None
    params = sig.parameters
    assert list(params) == ["url", "timeout"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    assert params["url"].default is inspect.Parameter.empty
    assert params["url"].annotation is str
    assert params["timeout"].default is None
    assert params["timeout"].annotation is int
    assert sig.return_annotation is str


def test_schema_to_signature_none_without_properties() -> None:
    """Empty schema / no properties / properties empty dict → None (caller retains (**kwargs))."""
    assert mcps_mod._schema_to_signature({}) is None
    assert mcps_mod._schema_to_signature({"type": "object"}) is None
    assert mcps_mod._schema_to_signature({"properties": {}}) is None


def test_schema_to_signature_none_for_non_identifier_name() -> None:
    """Parameter name is not a valid identifier (hyphen / starts with digit) → overall fallback to (**kwargs)."""
    assert mcps_mod._schema_to_signature({"properties": {"page-size": {"type": "integer"}}}) is None


def test_schema_to_signature_none_for_python_keyword_name() -> None:
    """Parameter name collides with Python keyword (`from`) → fallback (.isidentifier() returns True for keywords)."""
    assert mcps_mod._schema_to_signature({"properties": {"from": {"type": "string"}}}) is None


def test_schema_to_signature_unknown_type_falls_back_to_any() -> None:
    """type is a list (`["string","null"]`) / omitted / non-primitive name → annotation falls back to Any, no crash."""
    schema: dict[str, Any] = {
        "properties": {"a": {"type": ["string", "null"]}, "b": {}, "c": {"type": "geo"}},
        "required": [],
    }
    sig = mcps_mod._schema_to_signature(schema)
    assert sig is not None
    assert all(p.annotation is Any for p in sig.parameters.values())


def test_make_tool_callable_attaches_signature(mock_session: MagicMock) -> None:
    """proxy.<tool> callable carries signature synthesized from schema → inspect.signature sees real names."""
    mock_session.list_tools.return_value = MagicMock(
        tools=[
            _make_tool(
                "navigate",
                "Go to a URL",
                schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            )
        ]
    )
    proxy = mcps_mod._ServerProxy("chrome")
    sig = inspect.signature(proxy.navigate)
    assert list(sig.parameters) == ["url"]
    assert sig.parameters["url"].annotation is str


def test_unknown_tool_keeps_kwargs_signature(mock_session: MagicMock) -> None:
    """No schema (info=None) → no signature attached, retains (**kwargs)."""
    mock_session.list_tools.return_value = MagicMock(tools=[])
    proxy = mcps_mod._ServerProxy("chrome")
    params = inspect.signature(proxy.whatever).parameters
    assert list(params) == ["kwargs"]
    assert params["kwargs"].kind is inspect.Parameter.VAR_KEYWORD


def test_tool_signature_shows_real_params_for_mcp_tool(mock_session: MagicMock) -> None:
    """An MCP tool callable carries its real parameter names, not (**kwargs: Any)."""
    mock_session.list_tools.return_value = MagicMock(
        tools=[
            _make_tool(
                "navigate",
                schema={
                    "properties": {"url": {"type": "string"}, "timeout": {"type": "integer"}},
                    "required": ["url"],
                },
            )
        ]
    )
    proxy = mcps_mod._ServerProxy("chrome")
    rendered = str(inspect.signature(proxy.navigate))
    assert "url: str" in rendered
    assert "timeout: int" in rendered
    assert "**kwargs" not in rendered
