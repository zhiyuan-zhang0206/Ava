"""Per-agent computer MCP bridge: stdio MCP server in front of the shared service.

Speaks the standard MCP protocol to one agent (over stdio) and forwards every
call over a Unix socket to the per-machine `services.computer.mcp_daemon`, which
executes desktop actions through the signed permissions helper and audits them.
The tool list passes through verbatim from the daemon (single source of truth).
`agent_id` is stamped on every request from this process's identity, so the
audit stream carries the acting agent.

Wired in ava_builtins/mcps/computer_use/.mcp.json as the local-fallback command
(the MCP daemon's primary path dials the service directly — see
ava/_mcp_computer.py). Takes no arguments (the socket path is derived from
settings, matching the daemon). The MCP daemon spawns it with cwd pinned to the
repo root (`ava/_mcp_config.py:server_cwd`), so the relative interpreter path
resolves there:

    .venv/bin/python -m services.computer.mcp_wrapper
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from services.computer.protocol import Response
from shared.paths import computer_mcp_socket

# A single snapshot result (PNG metadata) is small, but keep the same generous
# line cap as the browser wrapper — a future brain tier may inline image data.
_LINE_LIMIT = 64 * 1024 * 1024

# The computer-mcp service is a supervised daemon that should already be up; a
# fresh agent may still race it on a cold cluster start, so retry the connect
# briefly before failing.
_CONNECT_ATTEMPTS = 10
_CONNECT_DELAY_S = 0.5


def _agent_id() -> int | None:
    """This process's agent identity (None when running outside an agent)."""
    import os

    raw = os.environ.get("AVA_AGENT_ID")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


class _Link:
    """One Unix-socket connection to the daemon, serializing request/response."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()
        self._id = 0

    async def request(self, payload: dict[str, Any]) -> Any:
        async with self._lock:
            self._id += 1
            payload = {"id": self._id, **payload, "agent_id": _agent_id()}
            self._writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await self._writer.drain()
            line = await self._reader.readline()
            if not line:
                raise ConnectionError("computer MCP daemon closed the connection")
            resp: Response = json.loads(line)
            if resp.get("id") != self._id:
                raise RuntimeError(
                    f"computer MCP daemon response id {resp.get('id')} != request {self._id}"
                )
            if resp["ok"] is False:
                raise RuntimeError(resp.get("error", "computer MCP daemon error"))
            return resp["result"]

    def close(self) -> None:
        with suppress(Exception):
            self._writer.close()


class _ReconnectingLink:
    """Wraps _Link with automatic reconnect on transport errors.

    When the shared daemon restarts (cluster update, watchdog respawn), the
    existing socket connection dies; reconnect transparently instead of
    surfacing a transport error. A call already delivered but unanswered is
    never retried — the action may have run on the desktop."""

    def __init__(self) -> None:
        self._link: _Link | None = None
        self._lock = asyncio.Lock()

    async def _connect_once(self) -> _Link:
        reader, writer = await _connect()
        return _Link(reader, writer)

    async def request(self, payload: dict[str, Any]) -> Any:
        async with self._lock:
            for attempt in range(6):
                try:
                    if self._link is None:
                        self._link = await self._connect_once()
                    return await self._link.request(payload)
                except ConnectionError:
                    self._link = None
                    if attempt == 5:
                        raise
                    await asyncio.sleep(0.5 * (2**attempt))
                except (json.JSONDecodeError, RuntimeError):
                    # Delivered, response unknown or desynced: do NOT retry —
                    # the action may have executed. Reconnect for next time.
                    self._link = None
                    raise
            raise RuntimeError("unreachable")


async def _connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    sock = str(computer_mcp_socket())
    last: Exception | None = None
    for _ in range(_CONNECT_ATTEMPTS):
        try:
            return await asyncio.open_unix_connection(path=sock, limit=_LINE_LIMIT)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            last = e
            await asyncio.sleep(_CONNECT_DELAY_S)
    raise ConnectionError(f"computer MCP daemon not reachable at {sock}: {last}")


async def _serve() -> None:
    link = _ReconnectingLink()

    async def _list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        tools = await link.request({"method": "list_tools"})
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in tools])

    async def _call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        result = await link.request(
            {"method": "call_tool", "tool": params.name, "args": params.arguments or {}}
        )
        return types.CallToolResult.model_validate(result)

    server: Server[Any] = Server("computer_use", on_list_tools=_list_tools, on_call_tool=_call_tool)

    async with stdio_server() as (server_read, server_write):
        await server.run(server_read, server_write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
