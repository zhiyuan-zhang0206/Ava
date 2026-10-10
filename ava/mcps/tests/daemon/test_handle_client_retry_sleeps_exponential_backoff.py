"""Daemon cases: handle client retry sleeps exponential backoff."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import ava.mcps._daemon as daemon_mod
from ava.mcps.tests.test_daemon import (
    _call_result,
    _content,
    _FakeWriter,
    _make_reader,
    _make_session,
    _tool,
    _write_config,
    _writer_arg,
)
from ava.mcps.tests.test_daemon import (
    _no_reap_stale_daemons as _no_reap_stale_daemons,
)
from ava.mcps.tests.test_daemon import (
    daemon_wide as daemon_wide,
)
from ava.mcps.tests.test_daemon import (
    fake_home as fake_home,
)
from ava.mcps.tests.test_daemon import (
    scope as scope,
)
from ava.mcps.tests.test_daemon import (
    short_socket_path as short_socket_path,
)


async def test_handle_client_retry_sleeps_exponential_backoff(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """Each retry sleeps with increasing backoff (0s, 1s, 2s)."""
    _write_config(fake_home, {"fs": {"command": "x"}})

    sleeps: list[float] = []

    async def _fake_sleep(duration: float) -> None:
        sleeps.append(duration)

    monkeypatch.setattr(daemon_mod.asyncio, "sleep", _fake_sleep)

    session = _make_session()
    session.list_tools = AsyncMock(side_effect=BrokenPipeError())
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {"id": 1, "method": "list_tools", "params": {"server": "fs"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )
    # 3 attempts = 2 retries = 2 sleep calls: attempt 0 fails → sleep(1),
    # attempt 1 fails → sleep(2), attempt 2 fails → no sleep (last attempt)
    assert sleeps == [1, 2]


async def test_shared_connections_isolate_sessions(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: daemon_mod._Scope,
    daemon_wide: daemon_mod._DaemonWide,
) -> None:
    """Two clients on the shared daemon never share session state.

    Each connection gets its own sessions/stacks/locks (created inside
    `_handle_connection`); agent A's `fs` session must be invisible to agent B,
    and each connection triggers its own `_connect_server`.
    """
    _write_config(fake_home, {"fs": {"command": "x"}})
    session_a = _make_session()
    session_b = _make_session()
    connect = AsyncMock(side_effect=[(session_a, MagicMock()), (session_b, MagicMock())])
    monkeypatch.setattr(daemon_mod, "_connect_server", connect)

    async def _call_list_tools() -> tuple[list[dict[str, Any]], Any]:
        """Run one full connection (request line -> response) with fresh state."""
        req = {"id": 1, "method": "list_tools", "params": {"server": "fs"}}
        reader = _make_reader([(json.dumps(req) + "\n").encode()])
        writer = _FakeWriter()
        await daemon_mod._handle_connection(reader, _writer_arg(writer), daemon_wide)
        return writer.responses(), writer

    resp_a, _ = await _call_list_tools()
    resp_b, _ = await _call_list_tools()
    assert resp_a[0]["ok"] is True and resp_b[0]["ok"] is True
    # two independent connects — one per connection
    assert connect.await_count == 2
    # module-level caches stay untouched by the shared path
    assert scope.local.sessions == {}


async def test_handle_connection_cleans_sessions_on_close(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, daemon_wide: daemon_mod._DaemonWide
) -> None:
    """When a client connection ends, its MCP server subprocesses are released.

    The whole point of per-connection ownership: a dead agent must not leak its
    chrome/x stdio children in the shared daemon.
    """
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session()
    stack = MagicMock()
    aclose = AsyncMock()
    stack.aclose = aclose  # type: ignore[method-assign]
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock(return_value=(session, stack)))

    req = {"id": 1, "method": "list_tools", "params": {"server": "fs"}}
    reader = _make_reader([(json.dumps(req) + "\n").encode(), b""])
    writer = _FakeWriter()
    await daemon_mod._handle_connection(reader, _writer_arg(writer), daemon_wide)
    # the connection's own stack was closed on disconnect
    aclose.assert_awaited_once()


async def test_handle_client_ping_returns_pong(scope: daemon_mod._Scope) -> None:
    """Lock-free liveness probe: no config / session involved, answers pong.

    The watchdog healthcheck dials the shared socket with ping; a slow MCP
    server must never make the daemon look dead.
    """
    req = {"id": 0, "method": "ping"}
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(reader, _writer_arg(writer), scope)
    [resp] = writer.responses()
    assert resp == {"id": 0, "ok": True, "result": "pong"}


async def test_get_session_shared_true_uses_daemon_wide_buckets(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """A `"shared": true` server caches in the daemon-wide buckets and is
    wrapped in a serializing session — one stdio child for every connection."""
    _write_config(fake_home, {"disc": {"command": "x", "shared": True}})
    session = _make_session()
    stack = MagicMock()
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock(return_value=(session, stack)))

    got = await daemon_mod._get_session("disc", scope)
    assert isinstance(got, daemon_mod._SerialSession)
    # per-connection buckets untouched; daemon-wide buckets hold the child
    assert scope.local.sessions == {}
    assert scope.shared.sessions["disc"] is got
    assert scope.shared.stacks["disc"] is stack


async def test_shared_connections_share_one_server_child(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, daemon_wide: daemon_mod._DaemonWide
) -> None:
    """Two client connections on a shared server trigger exactly one connect:
    the daemon-wide child is reused, and closing one connection does not
    release it (it outlives every connection, released at daemon shutdown)."""
    _write_config(fake_home, {"disc": {"command": "x", "shared": True}})
    session = _make_session(tools=[_tool("echo")])
    stack = MagicMock()
    stack.aclose = AsyncMock()  # type: ignore[method-assign]
    connect = AsyncMock(return_value=(session, stack))
    monkeypatch.setattr(daemon_mod, "_connect_server", connect)

    async def _one_connection() -> None:
        req = {"id": 1, "method": "list_tools", "params": {"server": "disc"}}
        reader = _make_reader([(json.dumps(req) + "\n").encode()])
        writer = _FakeWriter()
        await daemon_mod._handle_connection(reader, _writer_arg(writer), daemon_wide)

    await _one_connection()
    await _one_connection()

    assert connect.await_count == 1  # one child for two connections
    # connection teardown did not release the shared child
    assert daemon_wide.shared.sessions["disc"] is not None
    stack.aclose.assert_not_awaited()


async def test_shared_browser_server_connects_direct_no_child(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `"shared": "browser"` server is dialed directly (no stdio child):
    _connect_server dispatches to connect_browser_direct, never stdio_client."""
    _write_config(fake_home, {"chrome": {"command": ".venv/bin/python", "shared": "browser"}})
    browser_session = MagicMock()
    browser_stack = MagicMock()
    direct = AsyncMock(return_value=(browser_session, browser_stack))
    # _connect_server imports the direct-connect helper lazily inside the
    # function, so patch the source module attribute it resolves.
    import ava.mcps._browser as browser_mod

    monkeypatch.setattr(browser_mod, "connect_browser_direct", direct)
    spawned: list[int] = []

    def _boom(*_a: object, **_k: object) -> None:
        spawned.append(1)

    monkeypatch.setattr(daemon_mod, "stdio_client", _boom, raising=False)

    session, stack = await daemon_mod._connect_server("chrome", {}, timeout_seconds=lambda: 15.0)
    assert session is browser_session and stack is browser_stack
    direct.assert_awaited_once_with()
    assert spawned == []


async def test_get_session_shared_browser_uses_per_connection_buckets(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """A `"shared": "browser"` server caches in the CALLER's per-connection
    buckets, not the daemon-wide ones: each agent connection keeps its own
    socket, so the browser-mcp service's page affinity stays per agent and no
    connection can corrupt another's request-id stream. It is not wrapped in
    _SerialSession either — one connection is one serial caller by design."""
    _write_config(fake_home, {"chrome": {"command": ".venv/bin/python", "shared": "browser"}})
    browser_session = MagicMock()
    browser_stack = MagicMock()
    connect = AsyncMock(return_value=(browser_session, browser_stack))
    monkeypatch.setattr(daemon_mod, "_connect_server", connect)

    got = await daemon_mod._get_session("chrome", scope)
    assert got is browser_session
    assert scope.local.sessions["chrome"] is browser_session
    assert scope.local.stacks["chrome"] is browser_stack
    # daemon-wide buckets untouched — the socket dies with its connection
    assert scope.shared.sessions == {}
    assert scope.shared.stacks == {}


async def test_shared_browser_connections_keep_own_socket(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, daemon_wide: daemon_mod._DaemonWide
) -> None:
    """Two client connections on `"shared": "browser"` each dial their own
    browser-mcp socket (one connect per connection) and release it on close —
    there is no daemon-wide shared browser socket to desync."""
    _write_config(fake_home, {"chrome": {"command": ".venv/bin/python", "shared": "browser"}})
    connect = AsyncMock(
        side_effect=[(_make_session(), MagicMock()), (_make_session(), MagicMock())]
    )
    monkeypatch.setattr(daemon_mod, "_connect_server", connect)

    async def _one_connection() -> None:
        req = {"id": 1, "method": "list_tools", "params": {"server": "chrome"}}
        reader = _make_reader([(json.dumps(req) + "\n").encode()])
        writer = _FakeWriter()
        await daemon_mod._handle_connection(reader, _writer_arg(writer), daemon_wide)

    await _one_connection()
    await _one_connection()
    assert connect.await_count == 2  # per-connection sockets, no sharing
    assert daemon_wide.shared.sessions == {}


@pytest.mark.flaky  # real AF_UNIX sockets: two live browser-service connections
async def test_browser_concurrent_connections_no_id_desync(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, daemon_wide: daemon_mod._DaemonWide
) -> None:
    """Two connections calling the browser server concurrently both succeed.

    Regression for the id-desync bug (browser-mcp daemon response id N !=
    request N+2, permanently): the browser session used to live in the
    daemon-wide buckets, so concurrent connections multiplexed on ONE socket
    with ONE shared id counter and corrupted each other's response stream.
    Each connection must dial its own socket."""
    import ava.mcps._browser as browser_mod

    _write_config(fake_home, {"chrome": {"command": ".venv/bin/python", "shared": "browser"}})

    sock_path = Path(
        f"/tmp/ava-browser-{os.getpid()}-{os.urandom(3).hex()}.sock"  # noqa: S108 — test-only short AF_UNIX path
    )
    with contextlib.suppress(OSError):
        sock_path.unlink()

    async def _browser_service(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        # Serial, echoes request ids; the delay makes the two clients overlap
        # (both in flight before either response arrives).
        try:
            while line := await r.readline():
                await asyncio.sleep(0.05)
                req = json.loads(line)
                resp = {"id": req["id"], "ok": True, "result": {"content": [], "isError": False}}
                w.write((json.dumps(resp) + "\n").encode())
                await w.drain()
        finally:
            w.close()

    server = await asyncio.start_unix_server(_browser_service, path=str(sock_path))

    async def _direct() -> tuple[Any, Any]:
        reader, writer = await asyncio.open_unix_connection(sock_path, limit=64 * 1024 * 1024)
        session = browser_mod.BrowserLineSession(reader, writer, sock=str(sock_path))
        stack = contextlib.AsyncExitStack()
        stack.push_async_callback(session.close)
        return session, stack

    monkeypatch.setattr(browser_mod, "connect_browser_direct", _direct)

    async def _one_connection(req_id: int) -> list[dict[str, Any]]:
        req = {
            "id": req_id,
            "method": "call_tool",
            "params": {"server": "chrome", "tool": "navigate", "args": {"url": "https://x"}},
        }
        reader = _make_reader([(json.dumps(req) + "\n").encode()])
        writer = _FakeWriter()
        await daemon_mod._handle_connection(reader, _writer_arg(writer), daemon_wide)
        return writer.responses()

    try:
        resp_a, resp_b = await asyncio.gather(_one_connection(1), _one_connection(2))
        assert resp_a[0]["ok"] is True
        assert resp_b[0]["ok"] is True
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            sock_path.unlink()


async def test_connect_server_unknown_shared_value_raises(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecognized `shared` value fails fast instead of silently falling
    back to a per-connection child (which would defeat the declared intent)."""
    _write_config(fake_home, {"weird": {"command": "x", "shared": "mars"}})
    with pytest.raises(ValueError, match="unknown shared value 'mars'"):
        await daemon_mod._connect_server("weird", {}, timeout_seconds=lambda: 15.0)


async def test_invalidate_session_shared_clears_daemon_wide_buckets(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """Transport-error invalidation of a shared server rebuilds the daemon-wide
    child (not the caller's per-connection buckets)."""
    _write_config(fake_home, {"disc": {"command": "x", "shared": True}})
    session = _make_session()
    stack = MagicMock()
    stack.aclose = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(daemon_mod, "_connect_server", AsyncMock(return_value=(session, stack)))
    await daemon_mod._get_session("disc", scope)
    assert "disc" in scope.shared.sessions

    await daemon_mod._invalidate_session("disc", scope)
    assert "disc" not in scope.shared.sessions
    assert "disc" not in scope.shared.stacks
    stack.aclose.assert_awaited_once()


async def test_serial_session_serializes_concurrent_calls() -> None:
    """Concurrent calls on a shared session are serialized under one lock: the
    inner session sees one in-flight call at a time."""
    inner = _make_session()
    entered = 0
    max_inflight = 0
    in_flight = 0
    release = asyncio.Event()

    async def _slow_list_tools() -> MagicMock:
        nonlocal entered, max_inflight, in_flight
        entered += 1
        in_flight += 1
        max_inflight = max(max_inflight, in_flight)
        await release.wait()
        in_flight -= 1
        return MagicMock(tools=[])

    inner.list_tools = AsyncMock(side_effect=_slow_list_tools)  # type: ignore[method-assign]
    serial = daemon_mod._SerialSession(inner, asyncio.Lock())

    t1 = asyncio.create_task(serial.list_tools())
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(serial.list_tools())
    await asyncio.sleep(0.01)
    assert entered == 1  # second call is blocked on the lock
    release.set()
    await asyncio.gather(t1, t2)
    assert entered == 2
    assert max_inflight == 1  # never two at once


def test_shared_kind_reads_spec(fake_home: Path) -> None:
    """_shared_kind returns the declared value, None when absent/unknown."""
    _write_config(
        fake_home,
        {
            "a": {"command": "x", "shared": True},
            "b": {"command": "x", "shared": "browser"},
            "c": {"command": "x"},
        },
    )
    assert daemon_mod._shared_kind("a") is True
    assert daemon_mod._shared_kind("b") == "browser"
    assert daemon_mod._shared_kind("c") is None
    assert daemon_mod._shared_kind("nope") is None


async def test_connect_server_routes_url_servers_to_http(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `url` entry skips the stdio child path entirely — `_connect_http` owns it."""
    _write_config(fake_home, {"remote": {"url": "https://mcp.example.com/mcp"}})
    timeout_reader = MagicMock(return_value=15.0)
    http = AsyncMock(return_value=(MagicMock(), MagicMock()))
    monkeypatch.setattr(daemon_mod, "_connect_http", http)

    session, stack = await daemon_mod._connect_server("remote", {}, timeout_seconds=timeout_reader)

    http.assert_awaited_once_with(
        "https://mcp.example.com/mcp",
        None,
        oauth=False,
        server="remote",
        oauth_locks={},
        timeout_seconds=timeout_reader,
    )
    assert session is http.return_value[0]
    assert stack is http.return_value[1]


async def test_connect_http_initializes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP connect = streamable_http_client -> ClientSession -> initialize."""
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
    session_cls = MagicMock(return_value=session_cm)
    monkeypatch.setattr("mcp.ClientSession", session_cls)

    got, stack = await daemon_mod._connect_http(
        "https://mcp.example.com/mcp", None, oauth_locks={}, timeout_seconds=lambda: 15.0
    )

    assert got is session
    session.initialize.assert_awaited_once()
    assert session_cls.call_args.args == (read, write)
    assert factory.call_args.args == ("https://mcp.example.com/mcp",)
    assert factory.call_args.kwargs["http_client"] is None
    await stack.aclose()


async def test_connect_http_passes_headers_to_client_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static auth headers ride on an SDK-built httpx client."""
    streams = MagicMock()
    streams.__aenter__ = AsyncMock(return_value=(object(), object()))
    streams.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=streams)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", factory)
    session = MagicMock()
    session.initialize = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("mcp.ClientSession", MagicMock(return_value=session_cm))
    client_factory = MagicMock(return_value=object())
    monkeypatch.setattr("mcp.client.streamable_http.create_mcp_http_client", client_factory)

    await daemon_mod._connect_http(
        "https://mcp.example.com/mcp",
        {"Authorization": "Bearer k"},
        oauth_locks={},
        timeout_seconds=lambda: 15.0,
    )

    client_factory.assert_called_once_with(headers={"Authorization": "Bearer k"})
    assert factory.call_args.kwargs["http_client"] is client_factory.return_value


async def test_connect_http_fails_fast_and_closes_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead endpoint raises (no retry) and releases the half-built stack."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=ConnectionError("endpoint unreachable"))
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client",
        MagicMock(return_value=_boom()),
    )
    aclose = AsyncMock()
    monkeypatch.setattr(daemon_mod.AsyncExitStack, "aclose", aclose)

    with pytest.raises(ConnectionError):
        await daemon_mod._connect_http(
            "https://mcp.example.com/mcp", None, oauth_locks={}, timeout_seconds=lambda: 15.0
        )
    aclose.assert_awaited_once()


async def test_connect_server_routes_oauth_servers(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `oauth: true` url entry builds the OAuth client, not static headers."""
    _write_config(fake_home, {"remote": {"url": "https://mcp.example.com/mcp", "oauth": True}})
    timeout_reader = MagicMock(return_value=15.0)
    http = AsyncMock(return_value=(MagicMock(), MagicMock()))
    monkeypatch.setattr(daemon_mod, "_connect_http", http)

    await daemon_mod._connect_server("remote", {}, timeout_seconds=timeout_reader)

    http.assert_awaited_once_with(
        "https://mcp.example.com/mcp",
        None,
        oauth=True,
        server="remote",
        oauth_locks={},
        timeout_seconds=timeout_reader,
    )


async def test_connect_http_oauth_builds_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oauth=True hands the HTTP client off to the OAuth builder."""
    streams = MagicMock()
    streams.__aenter__ = AsyncMock(return_value=(object(), object()))
    streams.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=streams)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", factory)
    session = MagicMock()
    session.initialize = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("mcp.ClientSession", MagicMock(return_value=session_cm))

    oauth_client = MagicMock()
    oauth_builder = AsyncMock(return_value=oauth_client)
    locks: dict[str, Any] = {}
    import ava.mcps._oauth as oauth_mod

    monkeypatch.setattr(oauth_mod, "oauth_http_client", oauth_builder)

    await daemon_mod._connect_http(
        "https://mcp.example.com/mcp",
        None,
        oauth=True,
        server="exa",
        oauth_locks=locks,
        timeout_seconds=lambda: 15.0,
    )

    oauth_builder.assert_awaited_once_with("https://mcp.example.com/mcp", "exa", locks)
    assert factory.call_args.kwargs["http_client"] is oauth_client


async def test_shared_computer_use_server_connects_direct_no_child(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `"shared": "computer_use"` server is dialed directly (no stdio child):
    _connect_server dispatches to connect_computer_direct, never stdio_client."""
    _write_config(
        fake_home,
        {"computer_use": {"command": ".venv/bin/python", "shared": "computer_use"}},
    )
    session = MagicMock()
    stack = MagicMock()
    direct = AsyncMock(return_value=(session, stack))
    import ava.mcps._computer as computer_mod

    monkeypatch.setattr(computer_mod, "connect_computer_direct", direct)
    spawned: list[int] = []

    def _boom(*_a: object, **_k: object) -> None:
        spawned.append(1)

    monkeypatch.setattr(daemon_mod, "stdio_client", _boom, raising=False)

    got_session, got_stack = await daemon_mod._connect_server(
        "computer_use", {}, timeout_seconds=lambda: 15.0
    )
    assert got_session is session and got_stack is stack
    direct.assert_awaited_once_with()
    assert spawned == []


async def test_computer_use_call_stamps_agent_id(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, daemon_wide: daemon_mod._DaemonWide
) -> None:
    """The agent id from the client envelope is stamped onto the computer_use
    session before every call_tool, so the computer daemon can gate and audit
    per agent (the line payload carries it)."""
    _write_config(
        fake_home,
        {"computer_use": {"command": ".venv/bin/python", "shared": "computer_use"}},
    )
    import ava.mcps._computer as computer_mod

    received: dict[str, object] = {}

    async def _computer_service(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        line = await r.readline()
        req = json.loads(line)
        received.update(req)
        resp = {"id": req["id"], "ok": True, "result": {"content": [], "isError": False}}
        w.write((json.dumps(resp) + "\n").encode())
        await w.drain()
        w.close()

    sock = Path(f"/tmp/ava-computer-{os.getpid()}-{os.urandom(3).hex()}.sock")  # noqa: S108
    with contextlib.suppress(OSError):
        sock.unlink()
    server = await asyncio.start_unix_server(_computer_service, path=str(sock))

    async def _direct() -> tuple[Any, Any]:
        reader, writer = await asyncio.open_unix_connection(str(sock), limit=64 * 1024 * 1024)
        session = computer_mod.ComputerLineSession(reader, writer, sock=str(sock))
        stack = contextlib.AsyncExitStack()
        stack.push_async_callback(session.close)
        return session, stack

    monkeypatch.setattr(computer_mod, "connect_computer_direct", _direct)

    req = {
        "id": 1,
        "method": "call_tool",
        "params": {"server": "computer_use", "tool": "click", "args": {"x": 1}},
        "agent_id": 42,
    }
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    try:
        await daemon_mod._handle_connection(reader, _writer_arg(writer), daemon_wide)
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            sock.unlink()
    assert received.get("agent_id") == 42
    assert received.get("tool") == "click"


async def test_handle_client_stamps_agent_id_on_session(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, scope: daemon_mod._Scope
) -> None:
    """The SDK's per-request agent_id reaches the session before the call —
    BrowserLineSession (and ComputerLineSession) carry it on the wire so the
    service can key per-agent state. Sessions without the attribute (plain
    stdio servers) are skipped."""
    _write_config(fake_home, {"fs": {"command": "x"}})
    session = _make_session(
        call_result=_call_result([_content({"type": "text", "text": "hi"})]),
    )
    monkeypatch.setattr(
        daemon_mod, "_connect_server", AsyncMock(return_value=(session, MagicMock()))
    )

    req = {
        "id": 7,
        "method": "call_tool",
        "params": {"server": "fs", "tool": "read", "args": {}},
        "agent_id": 42,
    }
    reader = _make_reader([(json.dumps(req) + "\n").encode()])
    writer = _FakeWriter()
    await daemon_mod._handle_client(
        reader,
        _writer_arg(writer),
        scope,
    )

    [resp] = writer.responses()
    assert resp["ok"] is True
    session.call_tool.assert_awaited_once_with("read", {})
    assert session.client_agent_id == 42
