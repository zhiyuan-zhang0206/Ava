"""Unit tests for the MCP-daemon direct dial to the computer-mcp service
(ava/_mcp_computer.py): line-protocol round-trips, agent-identity stamping,
and self-healing on a desynced stream.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from ava._mcp_computer import connect_computer_direct


class FakeServer:
    """Echoes a canned list_tools/call_tool response; records requests."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = [
            {"name": "click", "description": "", "input_schema": {}}
        ]
        self.result = {"content": [{"type": "text", "text": "pong"}]}
        self.desync = False  # reply with a wrong id once, then heal
        self.error: str | None = None  # when set, reply ok=False

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            req = json.loads(line)
            self.requests.append(req)
            if req["method"] == "list_tools":
                resp = {"id": req["id"], "ok": True, "result": self.tools}
            elif self.error is not None:
                resp = {"id": req["id"], "ok": False, "error": self.error}
            else:
                resp = {"id": req["id"], "ok": True, "result": self.result}
            if self.desync:
                resp["id"] = req["id"] + 100
                self.desync = False
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        writer.close()


@pytest.fixture
async def session_and_server(
    tmp_path: Path,
) -> AsyncIterator[tuple[Any, FakeServer]]:
    server = FakeServer()
    # tmp_path can exceed the AF_UNIX path bound (104 chars); bind a short
    # path under /tmp instead.
    sock = f"/tmp/computer-mcp-test-{os.getpid()}.sock"  # noqa: S108 — test-only short AF_UNIX path
    with suppress(OSError):
        Path(sock).unlink()
    srv = await asyncio.start_unix_server(server.handle, path=sock)
    session, _stack = await connect_computer_direct(sock)
    try:
        yield session, server
    finally:
        await session.close()
        srv.close()
        await srv.wait_closed()
        with suppress(OSError):
            Path(sock).unlink()


async def test_list_tools_round_trip(session_and_server: tuple[Any, FakeServer]) -> None:
    session, server = session_and_server
    result = await session.list_tools()
    names = [t.name for t in result.tools]
    assert names == ["click"]
    # list_tools carries no agent identity
    assert server.requests[0].get("agent_id") is None


async def test_call_tool_carries_agent_id(session_and_server: tuple[Any, FakeServer]) -> None:
    session, server = session_and_server
    session.client_agent_id = 42
    await session.call_tool("click", {"x": 1, "y": 2})
    req = server.requests[0]
    assert req["method"] == "call_tool"
    assert req["tool"] == "click"
    assert req["args"] == {"x": 1, "y": 2}
    assert req["agent_id"] == 42


async def test_call_tool_without_identity(session_and_server: tuple[Any, FakeServer]) -> None:
    session, server = session_and_server
    session.client_agent_id = None
    await session.call_tool("click", {"x": 1})
    assert server.requests[0]["agent_id"] is None


async def test_desync_rebuilds_and_fails_loud(session_and_server: tuple[Any, FakeServer]) -> None:
    session, server = session_and_server
    server.desync = True
    with pytest.raises(RuntimeError, match="connection rebuilt"):
        await session.call_tool("click", {"x": 1})
    # the next call works on the rebuilt socket
    await session.call_tool("click", {"x": 2})
    assert len(server.requests) == 2
    assert server.requests[1]["args"] == {"x": 2}


async def test_daemon_error_surfaces(session_and_server: tuple[Any, FakeServer]) -> None:
    session, server = session_and_server
    server.error = "quota exceeded"
    with pytest.raises(RuntimeError, match="quota exceeded"):
        await session.call_tool("click", {"x": 1})
