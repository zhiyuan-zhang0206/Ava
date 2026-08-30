"""In-daemon client for the per-machine browser-mcp service.

The chrome MCP server used to cost every agent a ~63MB `mcp_wrapper` stdio
bridge subprocess on top of the shared browser-mcp daemon. This module is the
process-less replacement: `_connect_browser_direct()` dials the service's
line protocol from inside the MCP daemon and returns a session-shaped client.
No child process is involved; each agent connection keeps its own socket. The
calling agent's id rides on every request (`client_agent_id`), so the
service's page affinity is keyed per AGENT, not per connection — an exec
subprocess child re-connecting mid-turn lands on the same tab the agent
process selected.

The session self-heals a desynced stream (a response whose id does not match
the request): it rebuilds the socket and restarts the id counter, so one lost
or corrupted response line can never brick the connection for good.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, suppress
from typing import Any

# A single tool result (screenshot / DOM snapshot) can be multi-MB on one
# line; lift the stream buffer cap well above StreamReader's 64KiB default
# (same limit as the wrapper and the browser daemon).
_LINE_LIMIT = 64 * 1024 * 1024

# The browser-mcp service is a supervised daemon that should already be up; a
# fresh agent may still race it on a cold cluster start, so retry the connect
# briefly before failing (mirrors the wrapper's own connect retry).
_CONNECT_ATTEMPTS = 10
_CONNECT_DELAY_S = 0.5


class BrowserLineSession:
    """MCP-session-shaped client over the browser-mcp service's line protocol.

    Replaces the per-agent `services.browser.mcp_wrapper` stdio bridge: the
    browser daemon already owns the single chrome-devtools-mcp upstream, so
    this process-less connection is all the daemon needs to expose the same
    tools. `list_tools` / `call_tool` are the only methods the MCP daemon's
    request loop uses, so duck-typing the ClientSession contract is enough.
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
        # Re-dial path used when the stream desyncs; None (test sessions) means
        # the session cannot rebuild itself.
        self._sock = sock
        # Set by the daemon before each call (mirrors ComputerLineSession):
        # the calling agent's id, carried on every request so the service keys
        # page affinity per agent instead of per connection.
        self.client_agent_id: int | None = None

    async def close(self) -> None:
        """Close the current socket; the service reads this as the connection
        ending (the agent's page affinity survives in the per-agent registry —
        it is keyed by agent id, not by socket)."""
        await _close_writer(self._writer)

    async def _reconnect(self) -> None:
        """Drop the desynced socket and dial a fresh one (ids restart at 1).

        Without an agent id the service treats each connection as its own
        page-affinity context, so affinity is lost here: the caller's next
        page-scoped call gets the service's no-page error and re-navigates.
        With `client_agent_id` set the service keys affinity per agent, and
        the rebuilt connection re-adopts the agent's page on its next call.
        Either way the alternative — a permanently broken connection — is
        worse.
        """
        await self.close()
        if self._sock is None:
            return  # constructed without a re-dial path (unit tests)
        reader, writer = await _dial_browser_mcp(self._sock)
        self._reader = reader
        self._writer = writer
        self._id = 0

    async def _request(self, payload: dict[str, Any]) -> Any:
        self._id += 1
        payload = {"id": self._id, **payload}
        self._writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await self._writer.drain()
        line = await self._reader.readline()
        if not line:
            raise ConnectionError("browser-mcp daemon closed the connection")
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as e:
            # A corrupt line means the stream is no longer line-aligned; no
            # amount of waiting realigns it. Rebuild and fail loud.
            await self._reconnect()
            raise RuntimeError(
                f"browser-mcp daemon returned a corrupted line; "
                f"connection rebuilt — retry the call: {e}"
            ) from e
        # One connection = one in-flight request (the service serves each
        # connection serially), so ids must arrive in order; a mismatch means
        # the stream desynced — fail loud, never hand back another call's
        # result, but rebuild the connection so the next call works instead
        # of poisoning this session forever.
        if resp.get("id") != self._id:
            # Capture before the rebuild: _reconnect resets the counter.
            req_id = self._id
            await self._reconnect()
            raise RuntimeError(
                f"browser-mcp daemon response id {resp.get('id')} != request "
                f"{req_id}; connection rebuilt — retry the call"
            )
        if resp["ok"] is False:
            raise RuntimeError(resp.get("error", "browser-mcp daemon error"))
        return resp["result"]

    async def list_tools(self) -> Any:
        from mcp import types

        # The service returns a bare tool-dict list (the wrapper validates each
        # entry the same way); wrap it in the SDK result shape the daemon's
        # request loop consumes (.tools).
        result = await self._request({"method": "list_tools"})
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in result])

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        from mcp import types

        payload: dict[str, Any] = {"method": "call_tool", "tool": name, "args": args}
        if self.client_agent_id is not None:
            payload["agent_id"] = self.client_agent_id
        result = await self._request(payload)
        return types.CallToolResult.model_validate(result)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    """Close one browser-mcp socket connection; the service reads this as the
    connection ending (releasing its page-affinity context for this agent)."""
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()


async def _dial_browser_mcp(
    sock: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Dial the browser-mcp service's unix socket.

    Retries briefly while the socket is not yet bound (the service is a
    supervised daemon that should already be up; a fresh agent may still race
    it on a cold cluster start — mirrors the wrapper's connect retry).
    """
    last: Exception | None = None
    for _ in range(_CONNECT_ATTEMPTS):
        try:
            return await asyncio.open_unix_connection(path=sock, limit=_LINE_LIMIT)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            last = e
            await asyncio.sleep(_CONNECT_DELAY_S)
    raise ConnectionError(f"browser-mcp daemon not reachable at {sock}: {last}")


async def connect_browser_direct() -> tuple[BrowserLineSession, AsyncExitStack]:
    """Line-protocol connection to the browser-mcp service (no subprocess).

    Returns (session, stack); the caller owns the stack — closing it drops the
    socket, which the service reads as the connection ending. The stack closes
    the session's CURRENT socket: on a desync the session re-dials internally,
    so the original writer may no longer be the live one.
    """
    from shared.paths import chrome_mcp_socket

    sock = str(chrome_mcp_socket())
    reader, writer = await _dial_browser_mcp(sock)
    session = BrowserLineSession(reader, writer, sock=sock)
    stack = AsyncExitStack()
    stack.push_async_callback(session.close)
    return session, stack


# Exit-hook release bound: one dial attempt, short timeout — the agent process
# is already exiting and must not stall on this retry sequence if the daemon is
# mid-restart. The daemon's own dead-page reaper is the second line of defense.
_RELEASE_TIMEOUT_S = 3.0


async def release_agent_chrome_pages(agent_id: int) -> bool:
    """Best-effort: ask the browser-mcp service to close this agent's page.

    Called from the agent process's exit hook so a terminated worker does not
    leave its tab (usually a dev-server page pointing at a dead localhost port)
    in the user's shared Chrome. The service closes exactly the agent's
    affinity page — never another agent's or the user's — and is idempotent.
    Never raises: a machine without the browser service (no socket), a daemon
    that is down, or any protocol error returns False for the caller to log;
    deaths that never reach the exit hook are covered by the service's periodic
    dead-page reaper.
    """
    try:
        from shared.paths import chrome_mcp_socket

        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path=str(chrome_mcp_socket()), limit=_LINE_LIMIT),
            timeout=_RELEASE_TIMEOUT_S,
        )
    except Exception:
        return False
    try:
        writer.write(
            (
                json.dumps({"id": 1, "method": "release_agent_page", "agent_id": agent_id}) + "\n"
            ).encode()
        )
        await asyncio.wait_for(writer.drain(), timeout=_RELEASE_TIMEOUT_S)
        line = await asyncio.wait_for(reader.readline(), timeout=_RELEASE_TIMEOUT_S)
        if not line:
            return False
        resp: dict[str, Any] = json.loads(line)
        return resp.get("ok") is True
    except Exception:
        return False
    finally:
        await _close_writer(writer)
