"""Per-machine shared chrome-devtools-mcp service.

One upstream `chrome-devtools-mcp` attached to the shared headed Chrome,
multiplexed over a Unix socket to every agent's chrome MCP bridge
(`services.browser.mcp_wrapper`). This replaces the previous per-agent upstream:
N browser-using agents used to spawn N upstreams, and chrome-devtools-mcp's
collectors subscribe to the WHOLE browser's targets, so each of the N upstreams
independently buffered every tab's network/console traffic -- an N-fold
duplication that dominated agent-runner memory. One shared upstream collects
each tab once.

Two invariants make one upstream safe for many clients:

- Serial: a single lock around every upstream interaction, so concurrent
  clients never interleave a multi-step sequence (the operator chose serial over
  parallel -- one browser op at a time machine-wide).
- Per-connection page affinity: chrome-devtools-mcp has ONE global "selected
  page", but each client owns its own tabs. Each connection tracks its own
  current page; before forwarding a page-scoped call the daemon re-selects that
  connection's page, so client A's `click` never lands on client B's tab. A
  page-scoped call from a connection that has no page yet is NOT forwarded to
  whatever is globally selected (that could be another client's tab) -- it
  cold-starts (navigate -> new_page) or returns the no-page error.

Wire protocol (JSON line per request, mirrors `ava._mcps_daemon`):
  Request:  {"id": 1, "method": "list_tools"}
            {"id": 2, "method": "call_tool", "tool": "click", "args": {...}}
  Response: {"id": 1, "ok": true,  "result": [...]}            # tool dicts
            {"id": 2, "ok": true,  "result": {...}}            # CallToolResult dump
            {"id": 2, "ok": false, "error": "message"}
The bridge speaks MCP to its agent and translates to/from this line protocol;
results round-trip as pydantic `model_dump`/`model_validate`.

Run as a supervised daemon (ServiceSpec session "browser-mcp"):
    .venv/bin/python -m services.browser.mcp_daemon
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from services.browser.protocol import Request, Response
from services.browser.session import (
    gateway_session_is_valid,
    inject_session_cookie,
    last_injected_cookie,
)
from shared.config import settings
from shared.log import logger
from shared.machine import gateway_api_base
from shared.paths import chrome_mcp_socket

# A single tool result (screenshot / DOM snapshot) can be multi-MB on one line;
# lift the stream buffer cap well above StreamReader's 64KiB default.
_LINE_LIMIT = 64 * 1024 * 1024

# Pinned exact version — do NOT go back to @latest. npx re-resolves @latest on
# every daemon (re)start, so an upstream release can silently break the browser
# MCP overnight: on 2026-08-02 a newer chrome-devtools-mcp raised its engines
# floor to node ^20.19.0 || ^22.12.0 || >=23 while the session's PATH resolved
# node 18 first, and the upstream refused to start ("chrome upstream session is
# down"). An exact pin keeps deploys reproducible; bump it deliberately with a
# verified node version + a real chrome-MCP smoke test.
_UPSTREAM_PACKAGE = "chrome-devtools-mcp@1.6.0"

# Same lean flags as the standalone path used: drop the usage-statistics
# telemetry (and its watchdog subprocess) + the periodic update check.
_LEAN_FLAGS = ["--usageStatistics=false"]

# Page-management tools: they CHANGE which page a connection owns (or are
# browser-global) and so must not be re-pinned to the connection's current page
# before forwarding. Everything else is page-scoped and gets re-pinned.
_MANAGEMENT_TOOLS = frozenset({"new_page", "select_page", "close_page", "list_pages"})

# Per-agent page affinity — agent id -> that agent's current page. The
# selected-page state belongs to the AGENT, not to a TCP connection: an exec
# subprocess child re-connecting mid-turn (or the agent process itself after a
# session rebuild) must land on the same tab the agent selected, not cold-start
# with "No page selected" on every exec. Module-level on purpose: the
# ChromeMcpDaemon object is replaced on upstream reconnect, and this registry
# must survive that. One int per agent that has used the browser — bounded by
# the machine's agent count; a closed/crashed page drops the slot naturally
# via the existing re-pin failure path. Requests without an agent id (legacy
# wrapper clients) keep the per-connection fallback in _handle_client.
_AGENT_AFFINITY: dict[int, int | None] = {}

# chrome-devtools-mcp's cold-start error: a page-scoped call with no selected
# page. Matched verbatim from the upstream result; if upstream rewords it the
# daemon simply stops auto-recovering and the original error surfaces.
_NO_PAGE_MARKER = "No page selected"

# new_page / select_page / list_pages render the active tab as `  <id>: <url>
# [selected]`; that id is how the daemon learns a connection's current page.
_SELECTED_RE = re.compile(r"^\s*(\d+):.*\[selected\]\s*$", re.MULTILINE)

# Upstream session teardown (chrome-devtools-mcp died: Chrome restart, npx crash,
# OOM). The next upstream call raises one of these from the MCP stdio transport.
# The daemon now auto-reconnects on upstream death instead of exiting, so the
# watchdog only respawns when the whole daemon process itself dies (rare).
_UPSTREAM_DOWN = (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)
# The mcp SDK surfaces a dead/wedged upstream as MCPError with these codes (not
# as a transport-level anyio exception): CONNECTION_CLOSED when the peer's stdio
# read loop hit EOF, REQUEST_TIMEOUT when a call got no reply within the read
# timeout. Both mean "this upstream session is unusable" — the daemon must treat
# them like any other _UPSTREAM_DOWN and reconnect, or a wedged (V8-crashed,
# assert-failed, OOM'd) upstream leaves the daemon half-alive forever: ping
# still answers, list_tools serves its cache, and every real call hangs until
# the 180s read timeout, with `dead` never set (2026-08-06 #899).
_UPSTREAM_DOWN_ERROR_CODES = {CONNECTION_CLOSED, REQUEST_TIMEOUT}
_UPSTREAM_DOWN_MSG = "chrome upstream session is down; browser-mcp will restart"


def _is_upstream_down(exc: BaseException) -> bool:
    """True when `exc` means the upstream session is unusable and must reconnect."""
    return isinstance(exc, _UPSTREAM_DOWN) or (
        isinstance(exc, MCPError) and exc.code in _UPSTREAM_DOWN_ERROR_CODES
    )


# Reconnect backoff parameters for upstream death recovery.
_RECONNECT_INITIAL_DELAY_S = 1.0
_RECONNECT_MAX_DELAY_S = 30.0

# A wedged (not dead) upstream call must not hold the serial lock forever and
# freeze every client; bound each upstream request. Generous so a slow real
# navigation never trips it -- only a true hang does.
_READ_TIMEOUT = timedelta(seconds=180)

# Upstream watchdog: with no client calling, a wedged upstream would sit
# undetected forever (ping answers from the daemon, list_tools serves its
# cache). Probe the upstream itself every interval; a missing reply within the
# timeout marks the session dead and reconnects it (2026-08-06 #899).
_UPSTREAM_WATCHDOG_INTERVAL_S = 30.0
_UPSTREAM_WATCHDOG_TIMEOUT_S = 10.0

# Gateway session cookie refresh: the default server-side lifetime is 24h;
# refreshing every 6h leaves a comfortable margin and self-heals a lost,
# expired, or revoked managed-browser session. A navigation to a gateway URL
# additionally triggers an immediate validity check (_spawn_verify), so a
# revoked session heals on the next gateway page instead of waiting out the
# interval.
_SESSION_REFRESH_INTERVAL_S = 6 * 3600


def _text_of(result: types.CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


def _selected_id(result: types.CallToolResult) -> int | None:
    """The `[selected]` page id in a page-list result, or None on format drift."""
    m = _SELECTED_RE.search(_text_of(result))
    return int(m.group(1)) if m else None


def _no_page_result() -> types.CallToolResult:
    """The upstream's own no-page error, returned for a page-scoped call from a
    connection that owns no page -- forwarding it would touch another client's
    tab via the shared global selection."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{_NO_PAGE_MARKER}. Open a page first.")],
        is_error=True,
    )


class ChromeMcpDaemon:
    """Owns the single upstream session + the serial lock + the cached tool list.

    chrome-devtools-mcp page ids are a single shared namespace across all clients
    (one Chrome). `current_page` affinity keeps each client's calls on its own
    tab; it does not sandbox the namespace -- close_page / list_pages can still
    name any client's page (a client whose tab is closed out just gets a clean
    no-page error on its next call, never another client's tab).

    Two affinity shapes: requests carrying an `agent_id` resolve their page
    from the per-agent registry (`_AGENT_AFFINITY`, survives connections);
    requests without one fall back to the caller-supplied per-connection page.
    """

    def __init__(self, upstream: ClientSession) -> None:
        self._upstream = upstream
        self._lock = asyncio.Lock()
        self._tools: list[types.Tool] | None = None
        # Set when an upstream call hits a closed session; run() watches it to
        # trigger a reconnect instead of exiting.
        self.dead = asyncio.Event()

    async def _call(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        """Forward to the upstream, turning a dead session into a clean signal:
        flag the daemon dead so run() reconnects, and raise a readable error
        so the client knows to retry."""
        try:
            return await self._upstream.call_tool(name, args)
        except _UPSTREAM_DOWN as e:
            self.dead.set()
            raise RuntimeError(_UPSTREAM_DOWN_MSG) from e
        except (MCPError, TimeoutError) as e:
            if _is_upstream_down(e):
                self.dead.set()
                raise RuntimeError(_UPSTREAM_DOWN_MSG) from e
            raise

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self._lock:
            if self._tools is None:
                try:
                    self._tools = (await self._upstream.list_tools()).tools
                except _UPSTREAM_DOWN as e:
                    self.dead.set()
                    raise RuntimeError(_UPSTREAM_DOWN_MSG) from e
                except (MCPError, TimeoutError) as e:
                    if _is_upstream_down(e):
                        self.dead.set()
                        raise RuntimeError(_UPSTREAM_DOWN_MSG) from e
                    raise
        return [t.model_dump(mode="json", by_alias=True) for t in self._tools]

    async def call_tool(
        self, name: str, args: dict[str, Any], current_page: int | None
    ) -> tuple[types.CallToolResult, int | None]:
        """Forward one call under the serial lock, pinned to `current_page`.

        Returns the result and the connection's updated current page.
        """
        async with self._lock:
            return await self._affinity_call(name, args, current_page)

    async def call_tool_for_agent(
        self, name: str, args: dict[str, Any], agent_id: int
    ) -> types.CallToolResult:
        """Forward one call pinned to the agent's page (per-agent affinity).

        The connection's page resolves from the agent-keyed registry, so a
        fresh connection presenting the same agent id (an exec subprocess
        child) lands on the tab the agent process selected. Same serial lock +
        re-pin machinery as `call_tool`; a re-pin failure (tab closed
        underneath) drops the slot to no-page exactly like the per-connection
        path.
        """
        async with self._lock:
            result, updated = await self._affinity_call(name, args, _AGENT_AFFINITY.get(agent_id))
            _AGENT_AFFINITY[agent_id] = updated
            return result

    async def _affinity_call(
        self, name: str, args: dict[str, Any], current_page: int | None
    ) -> tuple[types.CallToolResult, int | None]:
        page_scoped = name not in _MANAGEMENT_TOOLS
        # A navigation to a gateway URL is the early-refresh trigger: the
        # managed session's 401 surfaces exactly there, so check it right
        # after the page loads instead of waiting out the refresh interval.
        verify_after = _navigates_to_gateway(name, args)

        if page_scoped:
            # Re-pin to this connection's page so the call lands on the right tab.
            # If the page vanished (closed underneath / crashed), re-pin errors:
            # drop to the no-current path rather than hit the global selection.
            if current_page is not None:
                repin = await self._call("select_page", {"pageId": current_page})
                if repin.is_error:
                    logger.warning(
                        f"[browser-mcp] re-pin to page {current_page} failed "
                        f"({_text_of(repin)!r}); dropping affinity"
                    )
                    current_page = None
            if current_page is None:
                # No tab of our own to act on. navigate_page can bootstrap one;
                # anything else fails fast instead of touching another client's tab.
                if name == "navigate_page" and isinstance(args.get("url"), str):
                    result = await self._call("new_page", args)
                    if verify_after and not result.is_error:
                        _spawn_verify()
                    return result, (_selected_id(result) or current_page)
                return _no_page_result(), current_page

        result = await self._call(name, args)
        if verify_after and not result.is_error:
            _spawn_verify()
        return result, self._next_page(name, args, result, current_page)

    @staticmethod
    def _next_page(
        name: str, args: dict[str, Any], result: types.CallToolResult, current_page: int | None
    ) -> int | None:
        """The connection's current page after this call resolved."""
        if result.is_error:
            return current_page
        if name == "new_page":
            return _selected_id(result) or current_page  # the freshly opened tab
        if name == "select_page":
            pid = args.get("pageId")
            return pid if isinstance(pid, int) else current_page
        if name == "close_page" and args.get("pageId") == current_page:
            return None
        # navigate_page (forwarded) and every other page-scoped op stay re-pinned.
        return current_page


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    daemon_ref: list[ChromeMcpDaemon | None],
) -> None:
    """One agent bridge connection. Requests carrying an `agent_id` resolve
    their page from the per-agent registry (a fresh connection — exec
    subprocess child — inherits the agent's page); the rest track
    `current_page` per connection (legacy fallback). `daemon_ref` is a mutable
    cell so the handler always sees the current daemon across upstream
    reconnects; when None (reconnecting), returns a transient error."""
    current_page: int | None = None
    try:
        with suppress(ConnectionResetError, BrokenPipeError):
            while line := await reader.readline():
                try:
                    req: Request = json.loads(line)
                except json.JSONDecodeError as e:
                    _write(writer, {"id": None, "ok": False, "error": f"JSON parse error: {e}"})
                    await writer.drain()
                    continue

                req_id, method = req.get("id"), req.get("method")

                # ping is lock-free and must succeed even during reconnection:
                # the healthcheck probes the daemon process itself (not the
                # upstream), so a reconnect window should never trigger a
                # false-positive death and unnecessary respawn.
                if method == "ping":
                    _write(writer, {"id": req_id, "ok": True, "result": None})
                    await writer.drain()
                    continue

                daemon = daemon_ref[0]
                if daemon is None or daemon.dead.is_set():
                    # Daemon is reconnecting — tell the client to retry.
                    _write(
                        writer,
                        {
                            "id": req_id,
                            "ok": False,
                            "error": _UPSTREAM_DOWN_MSG,
                        },
                    )
                    await writer.drain()
                    continue

                try:
                    resp: Response
                    if method == "list_tools":
                        resp = {"id": req_id, "ok": True, "result": await daemon.list_tools()}
                    elif method == "call_tool":
                        tool = req.get("tool")
                        if not tool:
                            # Missing/empty tool name: reject at the protocol edge
                            # instead of forwarding a nameless call the upstream
                            # would reject with a less legible error.
                            resp = {
                                "id": req_id,
                                "ok": False,
                                "error": "call_tool requires a non-empty 'tool' name",
                            }
                        else:
                            agent_id = req.get("agent_id")
                            # bool is an int subclass — reject it so a JSON
                            # `true` can never alias another agent's slot.
                            if isinstance(agent_id, int) and not isinstance(agent_id, bool):
                                result = await daemon.call_tool_for_agent(
                                    tool, req.get("args") or {}, agent_id
                                )
                            else:
                                result, current_page = await daemon.call_tool(
                                    tool, req.get("args") or {}, current_page
                                )
                            resp = {
                                "id": req_id,
                                "ok": True,
                                "result": result.model_dump(mode="json"),
                            }
                    else:
                        resp = {"id": req_id, "ok": False, "error": f"Unknown method: {method}"}
                except Exception as e:  # one bad call must not drop the whole connection
                    resp = {"id": req_id, "ok": False, "error": f"{type(e).__name__}: {e}"}
                _write(writer, resp)
                await writer.drain()
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


def _write(writer: asyncio.StreamWriter, obj: Response) -> None:
    writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())


async def _create_upstream(browser_url: str) -> tuple[ClientSession, AsyncExitStack]:
    """Create a new chrome-devtools-mcp upstream session.

    Returns (session, stack) — the caller owns the stack and must close it on
    teardown. The stack owns the subprocess + stdio pipes; when aclosed, the
    npx child is terminated.
    """
    upstream_params = StdioServerParameters(
        command="npx",
        args=[
            "-y",
            _UPSTREAM_PACKAGE,
            "--browserUrl",
            browser_url,
            "--allow-unrestricted-paths",
            *_LEAN_FLAGS,
        ],
        env={**get_default_environment(), "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1"},
    )

    stack = AsyncExitStack()
    try:
        read, write = await stack.enter_async_context(stdio_client(upstream_params))
        session = await stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=_READ_TIMEOUT.total_seconds())
        )
        # Bounded: if Chrome is not up yet, initialize hangs — time out so the
        # reconnect loop retries instead of blocking forever.
        await asyncio.wait_for(
            session.initialize(), timeout=settings.sandbox.mcp_connect_timeout_seconds
        )
        return session, stack
    except BaseException:
        await stack.aclose()
        raise


async def _await_stop_or_timeout(stop: asyncio.Event, timeout: float) -> None:
    """Sleep for *timeout* seconds, waking early if *stop* is set."""
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout)


async def _await_death_or_stop(daemon: ChromeMcpDaemon, stop: asyncio.Event) -> None:
    """Wait for daemon.dead or stop signal, cleaning up tasks on exit."""
    dead_task = asyncio.create_task(daemon.dead.wait())
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({stop_task, dead_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        dead_task.cancel()
        stop_task.cancel()


async def _upstream_watchdog(daemon: ChromeMcpDaemon, stop: asyncio.Event) -> None:
    """Periodically ping the upstream session; a wedged one (no reply within the
    timeout) sets `dead` so run() reconnects even with no client calling."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                daemon._upstream.send_ping(), timeout=_UPSTREAM_WATCHDOG_TIMEOUT_S
            )
        except (TimeoutError, MCPError) as e:
            if stop.is_set():
                return
            logger.error(f"[browser-mcp] upstream watchdog: {type(e).__name__} — reconnecting")
            daemon.dead.set()
            return
        except Exception as e:
            if stop.is_set():
                return
            logger.warning(f"[browser-mcp] upstream watchdog ping failed: {e!r}; retrying")
        await _await_stop_or_timeout(stop, _UPSTREAM_WATCHDOG_INTERVAL_S)


def _gateway_session_params() -> tuple[str, str] | None:
    """(gateway_url, cluster_secret) when a gateway session can be minted, else
    None (with a logged reason) — the daemon keeps serving either way, the
    browser just cannot open auth-gated gateway URLs without the cookie."""
    try:
        gateway_url = gateway_api_base()
    except Exception as e:  # gateway URL unset on this unit
        logger.warning(f"[browser-mcp] gateway session injection disabled: {e}")
        return None
    secret = settings.data_plane.cluster_secret
    if not secret:
        logger.warning("[browser-mcp] gateway session injection disabled: empty cluster secret")
        return None
    return gateway_url, secret


async def _inject_gateway_session_once() -> None:
    """Log in + inject the gateway session cookie into Chrome (best effort).

    Never raises: every failure is logged and left for the next tick / the
    next upstream connect to retry.
    """
    params = _gateway_session_params()
    if params is None:
        return
    gateway_url, secret = params
    try:
        await inject_session_cookie(settings.services.browser_cdp_port, gateway_url, secret)
    except Exception as e:
        logger.warning(f"[browser-mcp] gateway session cookie injection failed: {e}")
        return
    logger.info(f"[browser-mcp] gateway session cookie injected for {gateway_url}")


async def _gateway_session_loop(stop: asyncio.Event) -> None:
    """Refresh the gateway session cookie every _SESSION_REFRESH_INTERVAL_S.

    The refresh cadence stays inside the configured session lifetime and
    self-heals a lost/revoked cookie within one interval. The loop never
    raises — failures are logged and retried on the next tick.
    """
    while not stop.is_set():
        await _inject_gateway_session_once()
        await _await_stop_or_timeout(stop, _SESSION_REFRESH_INTERVAL_S)


# Fire-and-forget injection tasks are tracked so the event loop never reaps
# them before they finish (RUF006); each task removes itself on completion.
_inject_tasks: set[asyncio.Task[None]] = set()


def _spawn_inject() -> None:
    """Schedule a one-shot gateway-session injection (best effort)."""
    task = asyncio.create_task(_inject_gateway_session_once())
    _inject_tasks.add(task)
    task.add_done_callback(_inject_tasks.discard)


def _navigates_to_gateway(name: str, args: dict[str, Any]) -> bool:
    """True when the call opens a URL under the gateway's own origin.

    Gateway-served pages are exactly where a revoked or expired managed
    session surfaces as a 401, so a navigation there is the early-refresh
    trigger. Best effort: an unresolvable gateway base simply means no check.
    """
    if name not in ("navigate_page", "new_page"):
        return False
    url = args.get("url")
    if not isinstance(url, str):
        return False
    try:
        gateway_base = gateway_api_base()
    except Exception:
        return False
    target = urlsplit(url)
    gateway = urlsplit(gateway_base)
    return target.scheme in ("http", "https") and (target.scheme, target.netloc) == (
        gateway.scheme,
        gateway.netloc,
    )


_verify_lock = asyncio.Lock()


async def _verify_gateway_session_once() -> None:
    """Re-inject the gateway session cookie when the stored one no longer
    authenticates (revoked or expired), so a 401 heals on the next gateway
    navigation instead of waiting out the refresh interval.

    Best effort like ``_inject_gateway_session_once``: failures are logged and
    left for the next trigger to retry. With no stored cookie (fresh daemon)
    this falls back to a plain injection. Concurrent verifies are collapsed
    onto the in-flight one — a single re-injection is enough.
    """
    if _verify_lock.locked():
        return
    async with _verify_lock:
        params = _gateway_session_params()
        if params is None:
            return
        gateway_url, _ = params
        cookie = last_injected_cookie()
        if cookie is None:
            await _inject_gateway_session_once()
            return
        _, value = cookie
        try:
            valid = await gateway_session_is_valid(gateway_url, value)
        except Exception as e:
            logger.warning(f"[browser-mcp] gateway session validity check failed: {e}")
            return
        if not valid:
            logger.info("[browser-mcp] gateway session no longer valid — refreshing early")
            await _inject_gateway_session_once()


# Fire-and-forget verify tasks are tracked like injections (RUF006).
_verify_tasks: set[asyncio.Task[None]] = set()


def _spawn_verify() -> None:
    """Schedule a one-shot gateway-session validity check (best effort)."""
    task = asyncio.create_task(_verify_gateway_session_once())
    _verify_tasks.add(task)
    task.add_done_callback(_verify_tasks.discard)


async def _start_session_maintenance() -> tuple[asyncio.Event, asyncio.Task[None]]:
    """SIGTERM/SIGINT stop event + the long-lived session-refresh task.

    The refresh loop keeps a valid gateway session cookie in the shared
    Chrome: inject at startup, then refresh before the server-side row
    expires. It is independent of the upstream session — a Chrome restart
    that drops the upstream does not lose the cookie (the profile persists),
    and the periodic tick heals anything that did change (fresh profile,
    revocation, expiry).
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    session_task = asyncio.create_task(_gateway_session_loop(stop))
    return stop, session_task


async def _socket_in_use(path: Path) -> bool:
    """True when a live process is already listening on ``path``.

    A successful connect proves an occupant; ``FileNotFoundError`` /
    ``ConnectionRefusedError`` mean a stale socket (nobody listening) and are
    safe to unlink. Any other error is treated as occupied — fail closed
    rather than risk stealing a live instance's socket.
    """
    try:
        reader, writer = await asyncio.open_unix_connection(path=path)
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError:
        return True
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    del reader  # nothing to close on a StreamReader; the writer close suffices
    return True


async def run() -> None:  # noqa: PLR0915 — upstream watchdog lifecycle keeps the loop at 51 statements
    """Start the browser-mcp daemon.

    Listens on the shared Unix socket and auto-reconnects the upstream
    chrome-devtools-mcp on failure — no watchdog round-trip needed for the
    common case (npx crash / Chrome restart / OOM). Only SIGTERM/SIGINT or
    a fatal daemon-process error stops the loop.
    """
    browser_url = f"http://127.0.0.1:{settings.services.browser_cdp_port}"
    sock = chrome_mcp_socket()
    # Single-instance guard: the old code unlinked the socket unconditionally,
    # so a second daemon (watchdog misjudged the first dead and respawned)
    # stole the socket out from under the live instance — its clients all
    # disconnected and the first process became an orphan (audit round 2, P1).
    # Probe before unlink: a live listener means an instance is already
    # serving; refuse to start instead of stealing.
    if await _socket_in_use(sock):
        logger.error(
            "[browser-mcp] socket %s is already served by a live daemon — "
            "refusing to start a second instance",
            sock,
        )
        raise SystemExit(1)
    with suppress(OSError):
        sock.unlink()

    # Mutable reference so _handle_client always sees the current daemon across
    # upstream reconnects. None while reconnecting.
    daemon_ref: list[ChromeMcpDaemon | None] = [None]

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, daemon_ref), path=str(sock), limit=_LINE_LIMIT
    )
    logger.info(f"[browser-mcp] listening on {sock} (upstream {browser_url})")

    stop, session_task = await _start_session_maintenance()

    # Tracks the current upstream stack so we can close it before reconnecting.
    current_stack: AsyncExitStack | None = None
    reconnect_delay = _RECONNECT_INITIAL_DELAY_S
    # Hoisted out of the loop so the shutdown path can cancel it (P3: the
    # stop path used to leave it running until its own next tick).
    watchdog_task: asyncio.Task[None] | None = None

    try:
        while not stop.is_set():
            try:
                session, stack = await _create_upstream(browser_url)
            except Exception as e:
                logger.error(
                    f"[browser-mcp] upstream creation failed: {e}; "
                    f"retrying in {reconnect_delay:.1f}s"
                )
                await _await_stop_or_timeout(stop, reconnect_delay)
                if stop.is_set():
                    break
                reconnect_delay = min(reconnect_delay * 2, _RECONNECT_MAX_DELAY_S)
                continue

            # Successfully connected — swap in the new daemon and stack.
            if current_stack is not None:
                with suppress(Exception):
                    await current_stack.aclose()
            current_stack = stack
            daemon_ref[0] = ChromeMcpDaemon(session)
            watchdog_task = asyncio.create_task(_upstream_watchdog(daemon_ref[0], stop))
            reconnect_delay = _RECONNECT_INITIAL_DELAY_S
            logger.info(f"[browser-mcp] upstream connected ({browser_url})")

            # Chrome is confirmed up here — inject the gateway session cookie
            # right away (the periodic loop covers expiry; this covers the
            # fresh-profile cold start and the post-restart gap without
            # waiting for the next tick).
            _spawn_inject()

            # Wait for upstream death or stop signal.
            await _await_death_or_stop(daemon_ref[0], stop)

            if daemon_ref[0].dead.is_set():
                logger.error("[browser-mcp] upstream session died; reconnecting...")
                watchdog_task.cancel()
                with suppress(BaseException):
                    await watchdog_task
                watchdog_task = None
                daemon_ref[0] = None
                # Close the dead upstream stack NOW — its npx/node children
                # used to linger until the next successful reconnect (audit
                # round 2, P2), holding resources for the whole backoff window.
                # current_stack is non-None here by construction: this branch
                # sits after a successful _create_upstream (which assigns it);
                # the None reset makes the next loop iteration's assignment the
                # only path that matters (pyright: comparison is always true).
                with suppress(Exception):
                    await current_stack.aclose()
                current_stack = None
    finally:
        logger.info("[browser-mcp] shutting down")
        if watchdog_task is not None:
            watchdog_task.cancel()
            with suppress(BaseException):
                await watchdog_task
        session_task.cancel()
        with suppress(Exception):
            await session_task
        if current_stack is not None:
            with suppress(Exception):
                await current_stack.aclose()
        server.close()
        await server.wait_closed()
        with suppress(OSError):
            sock.unlink()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
