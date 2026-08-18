"""MCP daemon client-side handle — kept for interface compatibility.

The MCP daemon is now a per-machine shared service (ops roster session
"mcp-daemon", watchdog-managed like browser-mcp): one process serves every
agent on the machine, isolating sessions per client connection. Agents no
longer spawn their own daemon child — every method here is a no-op, and the
agent simply connects to the shared socket via `ava.mcps._get_remote_client`
(which falls back to local mode when the socket is absent).
"""

from __future__ import annotations

import asyncio

from shared.paths import mcp_daemon_shared_socket as _mcp_socket_path
from shared.paths import run_dir

__all__ = ["_MCP_SOCKET_DIR", "_MCPDaemon", "_mcp_socket_path"]

# Where the shared MCP daemon socket lives ($AVA_HOME/run). The naming
# convention lives in `shared.paths` (imported here as the daemon's bind path,
# and by `ava.mcps` for the client connect path), so the kernel does not import
# the agent-facing `ava.mcps` at startup — that module can be removed by
# AVA_SDK_DISABLE, which would otherwise crash the agent before it boots.
_MCP_SOCKET_DIR = run_dir()


class _MCPDaemon:
    """MCP daemon subprocess manager — spawn / ready-wait / cleanup."""

    def __init__(self, agent_id: int) -> None:
        self._agent_id = agent_id
        self._socket_path = _mcp_socket_path()
        self._proc: asyncio.subprocess.Process | None = None
        self._started = False

    @property
    def socket_path(self) -> str:
        return self._socket_path

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """No-op — the shared MCP daemon is a per-machine service, not a child
        of this agent. Kept for interface compatibility with the boot path."""

    async def spawn(self) -> None:
        """No-op — the shared MCP daemon is a per-machine service (watchdog-
        managed), not a child of this agent. If the shared socket is absent the
        SDK falls back to local mode on first use; nothing to do at boot."""
        self._started = False
        self._proc = None

    async def await_ready(self) -> None:
        """No-op — nothing to wait for; the shared daemon's readiness is the
        watchdog's job. The SDK connects lazily on first MCP use."""

    async def stop(self) -> None:
        """No-op — must NOT terminate the shared daemon (it serves every agent
        on the machine). The watchdog owns its lifecycle."""
        self._proc = None
