"""In-daemon client for the per-machine computer-mcp service.

The computer MCP server is fronted by the supervised `computer-mcp` daemon
(`services/computer/mcp_daemon.py`) exactly like the browser one — so the MCP
daemon dials its line protocol directly instead of paying a per-agent stdio
bridge child. `connect_computer_direct()` returns a session-shaped client; each
agent connection keeps its own socket, and the daemon stamps the agent's
identity (`client_agent_id`) onto every `call_tool` so the audit stream
carries the acting agent.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, suppress
from typing import Any

# Snapshot results stay small (PNG metadata), but keep the same generous line
# cap as the browser direct-dial so a future inline-image tier fits.
_LINE_LIMIT = 64 * 1024 * 1024

# The computer-mcp service is a supervised daemon that should already be up; a
# fresh agent may still race it on a cold cluster start, so retry the connect
# briefly before failing (mirrors the wrapper's own connect retry).
_CONNECT_ATTEMPTS = 10
_CONNECT_DELAY_S = 0.5


class ComputerLineSession:
    """MCP-session-shaped client over the computer-mcp service's line protocol.

    `list_tools` / `call_tool` are the only methods the MCP daemon's request
    loop uses, so duck-typing the ClientSession contract is enough. The MCP
    daemon stamps `client_agent_id` (a mutable attribute, set per request from
    the calling agent's envelope) before each call; the computer daemon reads
    it off the wire.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        sock: str | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._id = 0
        self._sock = sock  # re-dial path used when the stream desyncs
        self.client_agent_id: int | None = None

    async def close(self) -> None:
        await _close_writer(self._writer)

    async def _reconnect(self) -> None:
        await self.close()
        if self._sock is None:
            return
        reader, writer = await _dial_computer_mcp(self._sock)
        self._reader = reader
        self._writer = writer
        self._id = 0

    async def _request(self, payload: dict[str, Any], *, with_agent: bool) -> Any:
        self._id += 1
        if with_agent:
            payload = {"id": self._id, **payload, "agent_id": self.client_agent_id}
        else:
            payload = {"id": self._id, **payload}
        self._writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await self._writer.drain()
        line = await self._reader.readline()
        if not line:
            raise ConnectionError("computer-mcp daemon closed the connection")
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as e:
            await self._reconnect()
            raise RuntimeError(
                f"computer-mcp daemon returned a corrupted line; "
                f"connection rebuilt — retry the call: {e}"
            ) from e
        if resp.get("id") != self._id:
            req_id = self._id
            await self._reconnect()
            raise RuntimeError(
                f"computer-mcp daemon response id {resp.get('id')} != request "
                f"{req_id}; connection rebuilt — retry the call"
            )
        if resp["ok"] is False:
            raise RuntimeError(resp.get("error", "computer-mcp daemon error"))
        return resp["result"]

    async def list_tools(self) -> Any:
        from mcp import types

        result = await self._request({"method": "list_tools"}, with_agent=False)
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in result])

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        from mcp import types

        result = await self._request(
            {"method": "call_tool", "tool": name, "args": args}, with_agent=True
        )
        return types.CallToolResult.model_validate(result)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()


async def _dial_computer_mcp(
    sock: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    last: Exception | None = None
    for _ in range(_CONNECT_ATTEMPTS):
        try:
            return await asyncio.open_unix_connection(path=sock, limit=_LINE_LIMIT)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            last = e
            await asyncio.sleep(_CONNECT_DELAY_S)
    raise ConnectionError(f"computer-mcp daemon not reachable at {sock}: {last}")


async def connect_computer_direct(
    sock: str | None = None,
) -> tuple[ComputerLineSession, AsyncExitStack]:
    """Dial the per-machine computer-mcp service; return (session, stack).

    Mirrors `ava._mcp_browser.connect_browser_direct`: the returned stack is
    empty (the socket is the whole connection; nothing to close beyond the
    session's own writer), kept for interface parity with the stdio path."""
    if sock is None:
        from shared.paths import computer_mcp_socket

        sock = str(computer_mcp_socket())
    reader, writer = await _dial_computer_mcp(sock)
    return ComputerLineSession(reader, writer, sock=sock), AsyncExitStack()
