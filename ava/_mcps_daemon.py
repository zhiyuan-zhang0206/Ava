"""MCP daemon — long-running MCP server management process.

One per-machine daemon subprocess (ops roster session "mcp-daemon") manages
the lifecycle of all MCP servers. Agent execute_code subprocesses connect to
the daemon via Unix socket to call tools; sessions are isolated per client
connection so agent A can never reach agent B's MCP state.

Protocol (JSON-line, one request/response per line):
  Request:  {"id": 1, "method": "list_tools", "params": {"server": "chrome"}}
            {"id": 2, "method": "call_tool",  "params": {"server": "chrome", "tool": "navigate", "args": {"url": "https://example.com"}}}
  Response: {"id": 1, "ok": true,  "result": [...]}
            {"id": 1, "ok": false, "error": "message"}

Server subprocess sharing: a server entry may declare `"shared"` in its
.mcp.json to avoid one stdio child per agent connection:

- `"shared": "browser"` / `"shared": "computer"` — no child at all: the daemon
  dials the per-machine browser-mcp / computer-mcp service's line protocol
  directly (each connection keeps its own socket, so page affinity / agent
  identity stays per agent). Replaces the 60+MB mcp_wrapper.
- `"shared": true` — one daemon-wide stdio child serves every connection
  (serialized per server), for stateless servers like x.

Launch: python -m ava._mcps_daemon [socket_path]
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

from loguru import logger

from shared.config import settings

from ._mcp_config import assert_requirements, is_transport_error, load_mcp_config, server_url
from ._mcp_oauth import _OAUTH_FLOW_TIMEOUT_S


def _load_config() -> dict[str, dict[str, Any]]:
    """Merged MCP server map (machine `$AVA_HOME/mcp.json` + plugin-bundled `.mcp.json`).

    Same loader the in-process tool surface reads, so both agree on the server set.
    """
    return load_mcp_config()


# ─── server subprocess sharing ─────────────────────────────────────────────
#
# A `.mcp.json` entry may declare `"shared"` to stop paying one stdio child
# per agent connection (chrome's wrapper alone is ~63MB RSS per agent):
#
# - `"shared": "browser"` / `"shared": "computer"` — the server is fronted by
#   a per-machine service (browser-mcp / computer-mcp); the daemon dials its
#   line protocol directly and spawns no child. Each connection keeps its own
#   socket, so the service's per-connection affinity still isolates agents (a
#   single shared wrapper would collapse every agent onto one context).
# - `"shared": true` — one daemon-wide stdio child serves every connection,
#   serialized per server. Only safe for stateless servers (x);
#   stateful servers must stay per-connection.


def _shared_kind(server: str) -> Any:
    """The server's `shared` declaration: `"browser"`, True, or falsy (none)."""
    cfg = _load_config()
    spec = cfg.get(server)
    return spec.get("shared") if spec else None


def _session_buckets(
    shared: Any,
    sessions: dict[str, Any],
    stacks: dict[str, AsyncExitStack],
    session_locks: dict[str, asyncio.Lock],
) -> tuple[dict[str, Any], dict[str, AsyncExitStack], dict[str, asyncio.Lock]]:
    """Which (sessions, stacks, locks) a server's sessions live in.

    `"shared": true` servers use the daemon-wide buckets (one child for every
    agent connection, released only at daemon shutdown). Everything else —
    including `"shared": "browser"` / `"shared": "computer"`, which dial the
    per-machine browser-mcp / computer-mcp services directly — uses the
    caller's per-connection buckets: each connection keeps its own socket, so
    the service's per-connection affinity (page context / agent identity)
    still isolates agents (a single daemon-wide socket would both collapse
    every agent onto one context and let concurrent connections corrupt each
    other's request-id stream).
    """
    if shared is True:
        return shared_sessions, shared_stacks, shared_locks
    return sessions, stacks, session_locks


class _SerialSession:
    """Serialize every call on a daemon-wide shared MCP session.

    Concurrent calls from different agent connections would interleave on the
    shared stdio pipe and inside single-threaded servers (x_mcp's read-one-line-at-a-time bridge). One call at a time keeps
    semantics identical to the browser-mcp daemon's serial choice; only the
    shared session is serialized, never the per-connection ones.
    """

    def __init__(self, session: Any, lock: asyncio.Lock) -> None:
        self._session = session
        self._lock = lock

    async def list_tools(self) -> Any:
        async with self._lock:
            return await self._session.list_tools()

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        async with self._lock:
            return await self._session.call_tool(name, args)


def _is_transport_error(exc: BaseException) -> bool:
    """Return True when *exc* indicates the MCP server process or stdio pipe
    died and a reconnect + retry is appropriate.

    Shared with the in-process SDK (`ava._mcp_config.is_transport_error`) so
    both sides agree on the retry seam. Notably treats the SDK-synthesized
    `MCPError(CONNECTION_CLOSED)` — raised `from None` when the stdio peer's
    read loop hits EOF, so the __cause__ probe alone missed it (2026-08-13
    #1229) — as a transport error, not a tool-level error.
    """
    return is_transport_error(exc)


async def _invalidate_session(
    server: str,
    sessions: dict[str, Any],
    stacks: dict[str, AsyncExitStack],
    session_locks: dict[str, asyncio.Lock],
) -> None:
    """Remove *server*'s dead cached session and close its resources (under
    the session lock to avoid racing a concurrent reconnect). Resolves the
    shared daemon-wide buckets for shared servers, per-connection otherwise."""
    s_sessions, s_stacks, s_locks = _session_buckets(
        _shared_kind(server), sessions, stacks, session_locks
    )
    lock = s_locks.get(server)
    if lock is None:
        return
    async with lock:
        s_sessions.pop(server, None)
        stack = s_stacks.pop(server, None)
        if stack is not None:
            with suppress(Exception):
                await stack.aclose()


async def _connect_server(server: str) -> Any:
    """Start MCP server and return (session, AsyncExitStack).

    A `"shared": "browser"` server needs no child process: the daemon dials
    the per-machine browser-mcp service directly. Everything else spawns a
    stdio child. On failure the local stack is closed so no resources
    (subprocess, pipes, fds) leak. The caller is responsible for the returned
    stack."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from ._mcp_browser import connect_browser_direct
    from ._mcp_config import resolve_command, server_cwd

    cfg = _load_config()
    spec = cfg[server]
    assert_requirements(spec)
    url = server_url(spec)
    if url is not None:
        return await _connect_http(
            url, spec.get("headers"), oauth=bool(spec.get("oauth")), server=server
        )
    shared = spec.get("shared")
    if shared == "browser":
        return await connect_browser_direct()
    if shared == "computer":
        from ._mcp_computer import connect_computer_direct

        return await connect_computer_direct()
    if shared not in (None, False, True):
        raise ValueError(
            f"Server {server!r}: unknown shared value {shared!r} "
            f'(expected true, "browser", or "computer")'
        )
    cmd = resolve_command(spec["command"])
    args_list = spec.get("args") or []
    env_dict = spec.get("env") or {}

    # Our own servers launch their interpreter by a relative path, which resolves
    # against the child's cwd — installed packages from their own dir (isolated
    # venv), built-ins from the repo root. Plugin / machine entries get None.
    cwd = server_cwd(server)
    params = StdioServerParameters(command=cmd, args=args_list, env=env_dict or None, cwd=cwd)
    local_stack = AsyncExitStack()
    try:
        devnull = open(os.devnull, "w")  # noqa: SIM115, PTH123
        try:
            read, write = await asyncio.wait_for(
                local_stack.enter_async_context(stdio_client(params, errlog=devnull)),
                timeout=settings.sandbox.mcp_connect_timeout_seconds,
            )
        except BaseException:
            devnull.close()
            raise
        devnull.close()
        session = await local_stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(
            session.initialize(), timeout=settings.sandbox.mcp_connect_timeout_seconds
        )
    except BaseException:
        await local_stack.aclose()
        raise
    return session, local_stack


async def _connect_http(
    url: str, headers: dict[str, str] | None, *, oauth: bool = False, server: str = ""
) -> tuple[Any, AsyncExitStack]:
    """Connect to a remote Streamable HTTP MCP server — no child process.

    The 2026-07-28 protocol revision is stateless over HTTP: every request is
    self-describing (protocol version + client capabilities ride in `_meta`), so
    one endpoint serves every agent connection with no session state on either
    side. Static auth (API keys) goes in `headers` via the SDK's own client
    factory; `oauth=True` builds an OAuth 2.1 provider instead (authorization
    code + PKCE, browser flow — see `ava/_mcp_oauth.py`). On failure the local
    stack is closed so no HTTP client leaks. The caller owns the returned stack.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import (
        create_mcp_http_client,  # pyright: ignore[reportPrivateImportUsage] — re-exported from mcp.shared._httpx_utils
        streamable_http_client,
    )

    local_stack = AsyncExitStack()
    # An OAuth authorization flow involves the user clicking through a browser,
    # so the connect envelope is far more generous there than for stdio/static-auth.
    timeout = _OAUTH_FLOW_TIMEOUT_S if oauth else settings.sandbox.mcp_connect_timeout_seconds
    try:
        if oauth:
            from ._mcp_oauth import oauth_http_client

            http_client = await oauth_http_client(url, server)
        else:
            http_client = create_mcp_http_client(headers=headers) if headers else None
        read, write = await asyncio.wait_for(
            local_stack.enter_async_context(streamable_http_client(url, http_client=http_client)),
            timeout=timeout,
        )
        session = await local_stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=timeout)
    except BaseException:
        await local_stack.aclose()
        raise
    return session, local_stack


async def _handle_client(  # noqa: PLR0915
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    sessions: dict[str, Any],
    stacks: dict[str, AsyncExitStack],
    session_locks: dict[str, asyncio.Lock],
) -> None:
    """Handle one Unix socket client connection."""
    try:
        # Client may drop mid-stream (peer reset / pipe closed) — that ends the
        # connection, not an error; the finally still closes our writer.
        with suppress(ConnectionResetError, BrokenPipeError):
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as e:
                    resp = {"id": None, "ok": False, "error": f"JSON parse error: {e}"}
                    writer.write((json.dumps(resp) + "\n").encode())
                    await writer.drain()
                    continue

                req_id = req.get("id")
                method = req.get("method")
                params = req.get("params") or {}
                server = params.get("server", "")

                try:
                    # Retry on transport errors (dead MCP server process / broken
                    # pipe): invalidate the cached session, back off, reconnect.
                    resp = None
                    for _attempt in range(3):
                        try:
                            if method == "ping":
                                # Lock-free liveness probe for the watchdog
                                # healthcheck: no server session involved, so a
                                # slow MCP server can never false-kill a busy
                                # daemon.
                                resp = {"id": req_id, "ok": True, "result": "pong"}
                            elif method == "list_tools":
                                session = await _get_session(
                                    server, sessions, stacks, session_locks
                                )
                                result = await session.list_tools()
                                tools = [
                                    {
                                        "name": t.name,
                                        "description": t.description or "",
                                        "input_schema": t.input_schema or {},
                                    }
                                    for t in result.tools
                                ]
                                resp = {"id": req_id, "ok": True, "result": tools}
                            elif method == "call_tool":
                                tool = params.get("tool", "")
                                args = params.get("args") or {}
                                session = await _get_session(
                                    server, sessions, stacks, session_locks
                                )
                                # The computer-mcp direct-dial session carries
                                # the calling agent's identity to the computer
                                # daemon (governance + audit). Duck-typed:
                                # only ComputerLineSession has the attribute.
                                if hasattr(session, "client_agent_id"):
                                    session.client_agent_id = req.get("agent_id")
                                result = await session.call_tool(tool, args)
                                content = []
                                for c in result.content or []:
                                    # Same fail-fast as ava/mcps.py:_dump_content: the MCP
                                    # SDK contract requires ContentBlock to be a pydantic
                                    # model; absence of model_dump means the SDK type
                                    # changed or we missed a new type — silently turning
                                    # it into {"type": "unknown"} would let the agent
                                    # mis-parse it as legal data. The outer try catches
                                    # and returns ok=False; the daemon stays alive.
                                    if not hasattr(c, "model_dump"):
                                        raise TypeError(  # noqa: TRY301
                                            f"Unrecognized MCP content block type {type(c).__name__!r} "
                                            f"({c!r}); ContentBlock should be a pydantic model — SDK upgrade or new type?"
                                        )
                                    content.append(
                                        c.model_dump(
                                            mode="python", exclude_none=True, by_alias=True
                                        )
                                    )
                                resp = {
                                    "id": req_id,
                                    "ok": True,
                                    "result": {
                                        "content": content,
                                        "isError": bool(result.is_error),
                                        "structuredContent": result.structured_content,
                                    },
                                }
                            else:
                                resp = {
                                    "id": req_id,
                                    "ok": False,
                                    "error": f"Unknown method: {method}",
                                }
                            break
                        except Exception as e:
                            if _attempt == 2 or not _is_transport_error(e):
                                raise
                            await _invalidate_session(server, sessions, stacks, session_locks)
                            await asyncio.sleep(min(2**_attempt, 8))
                except Exception as e:
                    resp = {"id": req_id, "ok": False, "error": f"{type(e).__name__}: {e}"}

                writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                await writer.drain()
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def _get_session(
    server: str,
    sessions: dict[str, Any],
    stacks: dict[str, AsyncExitStack],
    session_locks: dict[str, asyncio.Lock],
) -> Any:
    """Get or create MCP server session (thread-safe).

    Shared servers (`"shared": true` in their spec) live in the daemon-wide
    buckets and are wrapped in a serializing session, so every connection
    shares one stdio child instead of spawning its own; non-shared servers
    keep the per-connection isolation contract."""
    shared = _shared_kind(server)
    s_sessions, s_stacks, s_locks = _session_buckets(shared, sessions, stacks, session_locks)
    if server in s_sessions:
        return s_sessions[server]

    lock = s_locks.get(server)
    if lock is None:
        lock = asyncio.Lock()
        s_locks[server] = lock

    async with lock:
        if server in s_sessions:
            return s_sessions[server]

        cfg = _load_config()
        if server not in cfg:
            raise ValueError(f"Server {server!r} not configured")

        session, stack = await _connect_server(server)
        if shared is True:
            session = _SerialSession(session, lock)
        s_sessions[server] = session
        s_stacks[server] = stack
        return session


async def _cleanup(sessions: dict, stacks: dict) -> None:
    """Close all MCP sessions."""
    for stack in stacks.values():
        with suppress(Exception):
            await stack.aclose()
    sessions.clear()
    stacks.clear()


async def _handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """One client connection = one agent's session space.

    The shared daemon serves every agent on the machine; each connection gets
    its own MCP-server session cache, so agent A's chrome session can never be
    reached by agent B (mirrors the browser-mcp daemon's per-connection page
    affinity). When the connection closes — agent exit, restart, crash — its
    sessions are cleaned up so no MCP server subprocess outlives its owner.
    `"shared"` servers live in the daemon-wide buckets instead: they outlive
    the connection by design and are released at daemon shutdown.
    """
    sessions: dict[str, Any] = {}
    stacks: dict[str, AsyncExitStack] = {}
    session_locks: dict[str, asyncio.Lock] = {}
    try:
        await _handle_client(reader, writer, sessions, stacks, session_locks)
    finally:
        await _cleanup(sessions, stacks)


# ── socket ownership guards (Task #1142) ───────────────────────────────────
#
# The ghost-storm root cause: a respawn storm (restarter / rollout wave
# relaunching the daemon while the previous detached process is unreachable by
# session kill) leaves ghost daemons listening on an unlinked inode. Two
# hazards follow, both fixed here:
#   1. a new daemon unlinked the socket file unconditionally before binding,
#      severing the LIVE socket of the current serving daemon (it becomes a
#      ghost), and
#   2. a killed ghost's exit cleanup unlinked the socket file again — by then
#      the file belonged to whoever replaced it.
# The guards: refuse to start over a live socket; reap this unit's stale
# daemons before binding; and unlink only the file we ourselves bound.

_SOCKET_PROBE_TIMEOUT_S = 1.0


def _socket_is_live(socket_path: str) -> bool:
    """True when a daemon answers a ping over `socket_path`.

    Mirrors the healthcheck's probe (connect + ping + reply). A socket that
    connects but never answers counts as NOT live: a half-dead occupant must be
    replaceable, not shielded forever by a connect that succeeds.
    """
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_SOCKET_PROBE_TIMEOUT_S)
    try:
        sock.connect(socket_path)
        sock.sendall((json.dumps({"id": 0, "method": "ping"}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return False
            buf += chunk
        resp = json.loads(buf.split(b"\n", 1)[0])
        return bool(resp.get("ok"))
    except (OSError, json.JSONDecodeError, TimeoutError):
        return False
    finally:
        sock.close()


_DAEMON_MODULE = "ava._mcps_daemon"


def _is_daemon_cmdline(cmdline: list[str]) -> bool:
    """True when *cmdline* is an actual ``python -m ava._mcps_daemon`` launch.

    Substring matching is NOT enough: the session backend wraps the launch in
    ``bash -lc 'cd <root> && ... .venv/bin/python -m ava._mcps_daemon'``, so the
    wrapper's single argv element is the whole shell command — it CONTAINS the
    module name but is no daemon. Killing that wrapper orphans the real daemon
    and reaps the session record (has_session → False, `ava stop` loses the
    daemon). Requiring the module name to be its own argv element, preceded by
    ``-m`` (the exact shape `python -m` produces), excludes every wrapper while
    still matching a daemon launched through `exec` (bash exec → the process
    image IS the daemon, argv unchanged).
    """
    for i, part in enumerate(cmdline):
        if part == _DAEMON_MODULE and i > 0 and cmdline[i - 1] == "-m":
            return True
    return False


def _reap_stale_daemons(project_root: Path | None) -> None:
    """Kill every OTHER `_mcps_daemon` process belonging to this unit.

    A fresh bind must be the only listener; ghosts from earlier respawn storms
    are reaped first. Only processes whose argv IS the daemon launch
    (``-m ava._mcps_daemon``) qualify — the session backend's ``bash -lc``
    wrapper carries the module name in its command string but is never reaped
    (killing it would orphan the live daemon and fake a dead session, #1199).
    Ownership = cwd under this unit's project root OR the process
    environment's AVA_HOME; a co-located other-cluster daemon (same binary,
    different home) is never touched.
    """
    import psutil

    me = os.getpid()
    home = str(settings.general.ava_home)
    root = (
        str(project_root)
        if project_root is not None
        else str(Path(__file__).resolve().parent.parent)
    )
    reaped = 0
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid == me:
                continue
            cmdline = proc.info["cmdline"] or []
            if not _is_daemon_cmdline(cmdline):
                continue
            try:
                cwd = proc.cwd()
                env_home = proc.environ().get("AVA_HOME", "")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if not ((root and cwd == root) or env_home == home):
                continue
            proc.kill()
            reaped += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if reaped:
        logger.warning(
            "reaped {} stale mcp-daemon process(es) before binding",
            reaped,
        )


def _prepare_socket(socket_path: str) -> None:
    """Clear the way for a fresh bind: reap stale daemons, drop a stale file."""
    _reap_stale_daemons(settings.services.project_root)
    with suppress(OSError):
        Path(socket_path).unlink()


def _unlink_own_socket(socket_path: str, self_ino: int) -> None:
    """Unlink `socket_path` only while it is still the inode this daemon bound.

    A ghost's exit must not sever the live socket of a later occupant (Task
    #1142): the file may have been replaced by another daemon since we bound it.
    """
    with suppress(OSError):
        if Path(socket_path).stat().st_ino == self_ino:
            Path(socket_path).unlink()


async def run_daemon(socket_path: str) -> None:
    """Start the shared MCP daemon, listening on one Unix socket.

    A single per-machine process (ops roster session "mcp-daemon") replaces
    the previous one-daemon-per-agent design: 15 agents used to pay 15 daemon
    processes (~12MB each). Sessions are isolated per client connection, so the
    shared process is not a shared state.
    """
    # Reap this unit's stale daemons + drop a stale socket file, then bind.
    # The live-socket guard already ran in main(); anything still at
    # `socket_path` here is a dead file, safe to clear.
    _prepare_socket(socket_path)

    server = await asyncio.start_unix_server(_handle_connection, path=socket_path)
    # The inode WE bound: an exit cleanup must not unlink a later occupant's
    # socket file (Task #1142).
    self_ino = Path(socket_path).stat().st_ino

    logger.info("MCP daemon listening on {}", socket_path)

    # Graceful exit
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    # finally, not just after stop_event: a cancel (parent task teardown) raises
    # out of stop_event.wait(), and without this the listening socket fd would
    # leak — the server must close on every exit path, not only the graceful one.
    try:
        await stop_event.wait()
    finally:
        logger.info("MCP daemon shutting down...")
        server.close()
        await server.wait_closed()
        # Per-connection buckets are cleaned by _handle_connection as each
        # client disconnects; the daemon-wide shared buckets outlive every
        # connection and are released only here.
        await _cleanup(shared_sessions, shared_stacks)
        await _cleanup(sessions, stacks)
        _unlink_own_socket(socket_path, self_ino)


# Per-connection session buckets — one set per client connection, created in
# `_handle_connection`, released on disconnect (PR #1273 isolation contract).
sessions: dict[str, Any] = {}
stacks: dict[str, AsyncExitStack] = {}
session_locks: dict[str, asyncio.Lock] = {}

# Daemon-wide session buckets — `"shared": true` servers live here: one stdio
# child serves every agent connection (serialized via _SerialSession) and is
# released only at daemon shutdown.
shared_sessions: dict[str, Any] = {}
shared_stacks: dict[str, AsyncExitStack] = {}
shared_locks: dict[str, asyncio.Lock] = {}


def main() -> None:
    """Entry point: python -m ava._mcps_daemon [socket_path]

    No argument: bind the shared per-machine socket (normal operation, run as
    the "mcp-daemon" ops service). One argument: bind that path (tests /
    migration). Two or more: fail fast.
    """
    if len(sys.argv) > 2:
        logger.error("Usage: python -m ava._mcps_daemon [socket_path]")
        sys.exit(1)
    if len(sys.argv) == 2:
        socket_path = sys.argv[1]
    else:
        from shared.paths import mcp_daemon_shared_socket

        socket_path = mcp_daemon_shared_socket()
    logger.add(sys.stderr, format="{message}")
    # Refuse to start over a LIVE socket: unlink+rebind here is what turns the
    # serving daemon into a ghost when a respawn storm races the previous
    # instance's exit (Task #1142). The healthcheck's probe answers a ping, so
    # an alive daemon keeps the watchdog from ever respawning over it — a
    # stray launch therefore exits cleanly instead of stealing the socket.
    if Path(socket_path).exists() and _socket_is_live(socket_path):
        logger.error(
            "MCP daemon already serving {} — refusing to start over it; exiting",
            socket_path,
        )
        sys.exit(1)
    asyncio.run(run_daemon(socket_path))


if __name__ == "__main__":
    main()
