"""Unit tests for ava._mcp_browser — in-daemon client for the browser-mcp service.

No real browser daemon: the line protocol is exercised with an in-memory
asyncio pair (StreamReader/StreamWriter), and the socket path is patched for
the connect-retry test. Mirrors the wrapper's protocol expectations: the
service answers `list_tools` with a bare tool-dict list and `call_tool` with
a CallToolResult dump, both as one JSON line.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

import ava._mcp_browser as browser_mod


class _FakeWriter:
    """Minimal asyncio.StreamWriter — captures written bytes; close no-ops."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self._chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def requests(self) -> list[dict[str, Any]]:
        raw = b"".join(self._chunks).decode("utf-8")
        return [json.loads(line) for line in raw.splitlines() if line]


def _make_session(
    responses: list[dict[str, Any]],
) -> tuple[browser_mod.BrowserLineSession, _FakeWriter]:
    """In-memory (session, writer): the reader yields one response line per
    request, the writer records the wire requests."""
    reader = asyncio.StreamReader()

    async def readline() -> bytes:
        for resp in responses:
            reader.feed_data((json.dumps(resp) + "\n").encode())
        reader.feed_eof()
        return await asyncio.StreamReader.readline(reader)

    reader.readline = readline  # type: ignore[method-assign]
    writer = _FakeWriter()
    return browser_mod.BrowserLineSession(reader, writer), writer  # type: ignore[arg-type]


# ─── list_tools / call_tool protocol mapping ─────────────────────────────


async def test_list_tools_wraps_bare_tool_list() -> None:
    """The service answers list_tools with a bare tool-dict list (the wrapper
    validated each entry the same way); the session wraps it in the SDK
    ListToolsResult shape the daemon's request loop consumes."""
    session, _ = _make_session(
        [
            {
                "id": 1,
                "ok": True,
                "result": [
                    {"name": "navigate", "description": "go", "inputSchema": {}},
                    {"name": "click", "description": "", "inputSchema": {}},
                ],
            }
        ]
    )
    result = await session.list_tools()
    assert [t.name for t in result.tools] == ["navigate", "click"]
    assert result.tools[0].description == "go"


async def test_call_tool_maps_result_dump() -> None:
    """call_tool forwards tool+args and validates the CallToolResult dump."""
    session, writer = _make_session(
        [
            {
                "id": 1,
                "ok": True,
                "result": {
                    "content": [{"type": "text", "text": "hello"}],
                    "isError": False,
                    "structuredContent": {"k": 1},
                },
            }
        ]
    )
    result = await session.call_tool("navigate", {"url": "https://x"})
    assert result.is_error is False
    assert result.content[0].text == "hello"  # type: ignore[union-attr]
    assert result.structured_content == {"k": 1}
    # the wire request carries the tool name + args
    assert writer.requests() == [
        {"id": 1, "method": "call_tool", "tool": "navigate", "args": {"url": "https://x"}}
    ]


async def test_list_tools_wire_payload() -> None:
    session, writer = _make_session([{"id": 1, "ok": True, "result": []}])
    await session.list_tools()
    assert writer.requests() == [{"id": 1, "method": "list_tools"}]


async def test_request_id_mismatch_raises() -> None:
    """A response whose id does not match the request means the stream
    desynced — fail loud instead of handing back another call's result. A
    session built without a re-dial path (unit test) has nothing to rebuild
    and simply raises."""
    session, _ = _make_session([{"id": 99, "ok": True, "result": []}])
    with pytest.raises(RuntimeError, match="response id 99 != request 1"):
        await session.list_tools()


async def test_request_id_mismatch_reconnects_then_next_call_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A desynced response rebuilds the socket (ids restart at 1) instead of
    poisoning the session: the call that observed the mismatch raises, and the
    next call succeeds on the fresh connection. The old socket is closed."""
    from shared import paths

    sock_path = Path(f"/tmp/ava-browser-sock-{os.getpid()}.sock")  # noqa: S108 — test-only short AF_UNIX path
    with contextlib.suppress(OSError):
        sock_path.unlink()

    connections: int = 0
    closed: int = 0
    rebuilt = asyncio.Event()
    all_closed = asyncio.Event()

    async def _handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        nonlocal connections, closed
        connections += 1
        rebuilt.set()
        desynced = connections == 1
        try:
            while line := await r.readline():
                req = json.loads(line)
                # The desynced connection answers with a stale id (a response
                # to an earlier request) — never the request's own id.
                resp = {"id": 99 if desynced else req["id"], "ok": True, "result": []}
                w.write((json.dumps(resp) + "\n").encode())
                await w.drain()
        finally:
            w.close()
            closed += 1
            if closed == 2:
                all_closed.set()

    server = await asyncio.start_unix_server(_handle, path=str(sock_path))
    monkeypatch.setattr(paths, "chrome_mcp_socket", lambda: sock_path)

    session, stack = await browser_mod.connect_browser_direct()
    with pytest.raises(RuntimeError, match="response id 99 != request 1"):
        await session.list_tools()
    # The rebuilt connection serves subsequent calls normally.
    result = await session.list_tools()
    assert result.tools == []
    await asyncio.wait_for(rebuilt.wait(), timeout=2)  # the rebuild dialed a new socket
    assert connections == 2  # the mismatch triggered a rebuild
    # Stack teardown closes the CURRENT (rebuilt) socket, not the dead one.
    await stack.aclose()
    await asyncio.wait_for(all_closed.wait(), timeout=2)  # server saw both EOFs
    assert closed == 2
    server.close()
    await server.wait_closed()
    with contextlib.suppress(OSError):
        sock_path.unlink()


async def test_corrupt_line_reconnects_then_next_call_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-JSON response line also means the stream is lost — rebuild the
    connection so a single corrupt line cannot brick it for good."""
    from shared import paths

    sock_path = Path(f"/tmp/ava-browser-sock-{os.getpid()}.sock")  # noqa: S108 — test-only short AF_UNIX path
    with contextlib.suppress(OSError):
        sock_path.unlink()

    connections: int = 0
    rebuilt = asyncio.Event()

    async def _handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        rebuilt.set()
        corrupt = connections == 1
        try:
            while line := await r.readline():
                if corrupt:
                    w.write(b"not json at all\n")
                else:
                    req = json.loads(line)
                    w.write(
                        (json.dumps({"id": req["id"], "ok": True, "result": []}) + "\n").encode()
                    )
                await w.drain()
        finally:
            w.close()

    server = await asyncio.start_unix_server(_handle, path=str(sock_path))
    monkeypatch.setattr(paths, "chrome_mcp_socket", lambda: sock_path)

    session, stack = await browser_mod.connect_browser_direct()
    with pytest.raises(RuntimeError, match="corrupted line"):
        await session.list_tools()
    result = await session.list_tools()
    assert result.tools == []
    await asyncio.wait_for(rebuilt.wait(), timeout=2)
    assert connections == 2
    await stack.aclose()
    server.close()
    await server.wait_closed()
    with contextlib.suppress(OSError):
        sock_path.unlink()


async def test_ok_false_raises_with_error() -> None:
    session, _ = _make_session([{"id": 1, "ok": False, "error": "upstream down"}])
    with pytest.raises(RuntimeError, match="upstream down"):
        await session.list_tools()


async def test_eof_raises_connection_error() -> None:
    """A closed service connection (daemon died) surfaces as ConnectionError —
    the daemon's transport-error retry treats it as reconnectable."""
    reader = asyncio.StreamReader()
    reader.feed_eof()
    session = browser_mod.BrowserLineSession(reader, _FakeWriter())  # type: ignore[arg-type]
    with pytest.raises(ConnectionError):
        await session.list_tools()


# ─── connect + retry ─────────────────────────────────────────────────────


async def test_connect_browser_direct_dials_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """connect_browser_direct opens one unix connection and returns a session
    whose stack closes the writer on exit."""
    from shared import paths

    # AF_UNIX paths cap at ~104 chars; pytest tmp_path is far longer, so use a
    # short fixed path (test-only, unique per run via pid).
    sock_path = Path(f"/tmp/ava-browser-sock-{os.getpid()}.sock")  # noqa: S108 — test-only short AF_UNIX path
    with contextlib.suppress(OSError):
        sock_path.unlink()

    async def _drop(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()

    server = await asyncio.start_unix_server(_drop, path=str(sock_path))
    monkeypatch.setattr(paths, "chrome_mcp_socket", lambda: sock_path)

    session, stack = await browser_mod.connect_browser_direct()
    assert isinstance(session, browser_mod.BrowserLineSession)
    await stack.aclose()
    server.close()
    await server.wait_closed()
    with contextlib.suppress(OSError):
        sock_path.unlink()


async def test_connect_browser_direct_retries_until_socket_appears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cold-start race (service socket not yet bound) retries instead of
    failing immediately — mirrors the wrapper's connect retry."""
    from shared import paths

    sock_path = Path(f"/tmp/ava-browser-sock-{os.getpid()}.sock")  # noqa: S108 — test-only short AF_UNIX path
    with contextlib.suppress(OSError):
        sock_path.unlink()
    monkeypatch.setattr(browser_mod, "_CONNECT_ATTEMPTS", 20)
    monkeypatch.setattr(browser_mod, "_CONNECT_DELAY_S", 0.02)
    monkeypatch.setattr(paths, "chrome_mcp_socket", lambda: sock_path)

    server_future: asyncio.Future[asyncio.AbstractServer] = asyncio.Future()

    async def _late_server() -> None:
        await asyncio.sleep(0.08)

        async def _drop(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            w.close()

        server = await asyncio.start_unix_server(_drop, path=str(sock_path))
        server_future.set_result(server)
        await asyncio.sleep(5)

    task = asyncio.create_task(_late_server())
    try:
        session, stack = await browser_mod.connect_browser_direct()
        assert isinstance(session, browser_mod.BrowserLineSession)
        await stack.aclose()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(OSError):
            sock_path.unlink()
