"""In-process Unix socket client to the per-machine MCP daemon.

The agent process talks to the shared MCP daemon (ops roster session
"mcp-daemon") over a JSON-line Unix socket — one connection per agent, with
the agent's identity riding on every request so gated servers can enforce
per-agent governance. When the daemon is absent the SDK falls back to its own
local sessions (see `ava.mcps._connect`).

Moved out of `ava.mcps` (2026-08-13, #1229) to keep that module under its
line budget; `ava.mcps` re-exports these names.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from shared.config import settings

from ._mcp_config import MCPCallError, MCPConnectError, ToolInfo


class _RemoteMCPClient:
    """Lightweight Unix socket client to MCP daemon."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._req_id = 0

    def _ensure_connected(self) -> socket.socket:
        if self._sock is not None:
            try:
                self._sock.sendall(b"")
                return self._sock
            except OSError:
                with suppress(OSError):
                    self._sock.close()
                self._sock = None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(settings.sandbox.mcp_connect_timeout_seconds)
        sock.connect(self._socket_path)
        self._sock = sock
        return sock

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        with self._lock:
            self._req_id += 1
            req_id = self._req_id
            req = (
                json.dumps(
                    {
                        "id": req_id,
                        "method": method,
                        "params": params,
                        # The daemon forwards this to the computer-mcp service
                        # (and could forward it to any gated server): one
                        # connection = one agent, and the identity rides on
                        # every request so the service can gate per agent.
                        "agent_id": _current_agent_id(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            sock = self._ensure_connected()
            try:
                sock.sendall(req.encode("utf-8"))
                resp = self._read_response(sock, req_id)
            except BaseException:
                # Any failure leaves the stream in an ambiguous state: a
                # response to this request may still arrive (client deadline
                # fired while the daemon kept processing), or the stream may be
                # misaligned. A late response left in the buffer would be
                # consumed by the NEXT request. Close and forget the socket so
                # the next request reconnects fresh.
                with suppress(OSError):
                    sock.close()
                self._sock = None
                raise
            if not resp.get("ok"):
                raise MCPCallError(resp.get("error", "Unknown error"))
            return resp.get("result")

    def _read_response(self, sock: socket.socket, req_id: int) -> dict[str, Any]:
        """Read response lines until the one whose id matches `req_id`.

        Response-id matching: only the line whose id equals this request's id
        is its response. A stale line — a response to a request that timed out
        while the daemon kept processing it — or an unsolicited notification
        carries a foreign / missing id; consuming it would mis-associate the
        stream (this request gets the previous one's result, and the real
        response then shifts every later request — response cross-talk). Foreign
        lines are skipped; the unread tail stays buffered. Bounded by the same
        deadline as the connect phase (`mcp_connect_timeout_seconds`).
        """
        buf = b""
        deadline = time.time() + settings.sandbox.mcp_connect_timeout_seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise MCPConnectError("MCP daemon request timeout")
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                raise MCPConnectError("MCP daemon request timeout") from None
            if not chunk:
                raise MCPConnectError("MCP daemon connection closed")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    parsed = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    raise MCPConnectError("MCP daemon returned a malformed response line") from None
                if parsed.get("id") == req_id:
                    return parsed

    def list_tools(self, server: str) -> list[ToolInfo]:
        result = self._request("list_tools", {"server": server})
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema", {}),
            }
            for t in result
        ]

    def call_tool(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "call_tool",
            {
                "server": server,
                "tool": tool,
                "args": args,
            },
        )


def _current_agent_id() -> int | None:
    """This process's agent identity, from the bootstrap env (same source
    `ava.self.AGENT_ID` reads). None outside an agent process — background
    scripts recovered an identity only when AVA_AGENT_ID is set; a hosted
    turn context (turn contextvar bound) wins over the ambient env."""
    from shared.turn_identity import effective_agent_id

    return effective_agent_id()


def _socket_path_for() -> str:
    """Filesystem path of the shared MCP daemon socket — the client side of the
    convention. The daemon is a per-machine service (one for all agents;
    sessions isolated per connection). Thin alias of
    `shared.paths.mcp_daemon_shared_socket`, the single source of truth (moved
    to shared/ so the agent kernel's daemon can import it without going through
    this SDK-disable-able module)."""
    from shared.paths import mcp_daemon_shared_socket

    return mcp_daemon_shared_socket()


def _daemon_socket_path() -> str | None:
    """This process's MCP daemon socket path, or None to fall back to local
    connect. Derived from `ava.self.AGENT_ID` — so it resolves the same in the
    agent process and in any background script that recovered the same identity,
    letting a launched script reuse the agent's running daemon (shared MCP
    sessions) instead of spawning its own. Returns None when identity is unset or
    the socket file is absent. The `exists()` check is a cheap presence gate, not
    a liveness probe — a stale socket left by a hard-killed daemon still looks
    present; the caller's connect then fails and routes to the local fallback."""
    path = _socket_path_for()
    return path if Path(path).exists() else None


_remote_client: _RemoteMCPClient | None = None
_remote_client_lock = threading.Lock()


def _get_remote_client() -> _RemoteMCPClient | None:
    """Return cached remote client if this agent's MCP daemon is running, else
    None (caller falls back to local connect).

    The client is created once per subprocess and reused across calls.
    """
    global _remote_client  # noqa: PLW0603
    socket_path = _daemon_socket_path()
    if not socket_path:
        return None
    if _remote_client is not None:
        return _remote_client
    with _remote_client_lock:
        if _remote_client is not None:
            return _remote_client
        _remote_client = _RemoteMCPClient(socket_path)
        return _remote_client
