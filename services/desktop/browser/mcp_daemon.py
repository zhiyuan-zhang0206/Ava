"""Per-machine shared chrome-devtools-mcp service.

One upstream `chrome-devtools-mcp` attached to the shared headed Chrome,
multiplexed over a Unix socket to every agent's chrome MCP bridge
(`services.desktop.browser.mcp_wrapper`). This replaces the previous per-agent upstream:
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

Page lifetime (2026-09-11 ruling): every page this stack
CREATES (an explicit `new_page`, or the auto-created page on a page-less first
navigate) carries a hard TTL deadline (`AVA_CHROME_PAGE_DEFAULT_TTL_SECONDS`,
24h default) and is closed by the expiry sweep when
the deadline passes -- activity never extends it. `renew_page`, a daemon-owned
tool appended to the upstream tool list, moves the deadline to now + ttl (at
most 24h per call); pages this stack never created (the user's own tabs) are
out of scope by construction, and expiry surfaces as the page simply being
gone (next page-scoped call takes the existing no-page path) -- never as an
invented "page expired" error. See `services.desktop.browser.page_lifecycle`.

Tab groups: every page an AGENT's call creates is also filed into that agent's
Chrome tab group (`ChromeMcpDaemon._new_page`, via
`services.desktop.browser.tab_groups`). Presentation only: a grouping failure
is logged and never fails the call. Nothing here removes a group -- Chrome does
when the agent's last page closes, which every close path above ends in.

Wire protocol (JSON line per request, mirrors `ava.mcps._daemon`):
  Request:  {"id": 1, "method": "list_tools"}
            {"id": 2, "method": "call_tool", "tool": "click", "args": {...}}
            {"id": 3, "method": "release_agent_page", "agent_id": 7}
  Response: {"id": 1, "ok": true,  "result": [...]}            # tool dicts
            {"id": 2, "ok": true,  "result": {...}}            # CallToolResult dump
            {"id": 2, "ok": false, "error": "message"}
The bridge speaks MCP to its agent and translates to/from this line protocol;
results round-trip as pydantic `model_dump`/`model_validate`.

Run as a supervised daemon (ServiceSpec session "browser-mcp"):
    .venv/bin/python -m services.desktop.browser.mcp_daemon
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, types
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.log import logger
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import ava_home, chrome_mcp_socket
from base.telemetry import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.desktop.browser import page_lifecycle
from services.desktop.browser.gateway_session import GatewaySession, stop_on_signals
from services.desktop.browser.mcp_upstream import (
    _await_stop_or_timeout,
    _bounded,
    _bounded_stack_close,
    _create_upstream,
    _StoppingError,
    contained,
)
from services.desktop.browser.page_lifecycle import (
    RENEW_PAGE_TOOL,
    PageRegistry,
    _text_of,
    expired_page_reaper,
    handle_release_agent_page,
    renew_agent_page,
    renew_page_tool_dump,
)
from services.desktop.browser.protocol import Request, Response
from services.desktop.browser.tab_groups.grouping import AgentTabGroups

# A single tool result (screenshot / DOM snapshot) can be multi-MB on one line;
# lift the stream buffer cap well above StreamReader's 64KiB default.
_LINE_LIMIT = 64 * 1024 * 1024

# Page-management tools: they CHANGE which page a connection owns (or are
# browser-global) and so must not be re-pinned to the connection's current page
# before forwarding. Everything else is page-scoped and gets re-pinned.
_MANAGEMENT_TOOLS = frozenset({"new_page", "select_page", "close_page", "list_pages"})

# chrome-devtools-mcp's cold-start error: a page-scoped call with no selected
# page. Matched verbatim from the upstream result; if upstream rewords it the
# daemon simply stops auto-recovering and the original error surfaces.
_NO_PAGE_MARKER = "No page selected"

# new_page / select_page / list_pages render the active tab as `  <id>: <url>
# [selected]`; that id is how the daemon learns a connection's current page.
# The marker is not always last on the line: an isolated-context tab renders
# `[selected] isolatedContext=<name>` (e.g. `29: Discord (url) [selected]
# isolatedContext=reg-7`). Accept only `key=value` suffixes (or nothing) after
# the marker -- a title that happens to contain a literal `[selected]` followed
# by other text must not be mistaken for the marker.
_SELECTED_RE = re.compile(r"^\s*(\d+):.*\[selected\](?:\s+\S+=\S+)*\s*$", re.MULTILINE)

# Upstream session teardown (chrome-devtools-mcp died: Chrome restart, npx crash,
# OOM). The next upstream call raises one of these from the MCP stdio transport.
# Upstream death ends this daemon generation; only the service supervisor may restart it.
_UPSTREAM_DOWN = (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)
# The mcp SDK surfaces a dead/wedged upstream as MCPError with these codes (not
# as a transport-level anyio exception): CONNECTION_CLOSED when the peer's stdio
# read loop hit EOF, REQUEST_TIMEOUT when a call got no reply within the read
# timeout. Both mean "this upstream session is unusable" — the daemon must treat
# them like any other _UPSTREAM_DOWN and exit, or a wedged (V8-crashed,
# assert-failed, OOM'd) upstream leaves the daemon half-alive forever: ping
# still answers, list_tools serves its cache, and every real call hangs until
# the 180s read timeout, with `dead` never set (2026-08-06 #899).
_UPSTREAM_DOWN_ERROR_CODES = {CONNECTION_CLOSED, REQUEST_TIMEOUT}
_UPSTREAM_DOWN_MSG = "chrome upstream session is down; restart browser-mcp before continuing"


def _is_upstream_down(exc: BaseException) -> bool:
    """True when `exc` means this upstream session is unusable."""
    return isinstance(exc, _UPSTREAM_DOWN) or (
        isinstance(exc, MCPError) and exc.code in _UPSTREAM_DOWN_ERROR_CODES
    )


# Upstream watchdog: with no client calling, a wedged upstream would sit
# undetected forever (ping answers from the daemon, list_tools serves its
# cache). Probe the upstream itself every interval; a missing reply within the
# timeout marks the session dead and ends the process (2026-08-06 #899).
_UPSTREAM_WATCHDOG_INTERVAL_S = 30.0
_UPSTREAM_WATCHDOG_TIMEOUT_S = 10.0


def _selected_id(result: types.CallToolResult) -> int | None:
    """The `[selected]` page id in a page-list result, or None on format drift."""
    m = _SELECTED_RE.search(_text_of(result))
    return int(m.group(1)) if m else None


def _page_id(value: Any) -> int | None:
    """An int page id from a JSON argument, or None when the value names none.

    The MCP schema declares `pageId` as a number, so a JSON client may send an
    integral float (`29.0` for page 29): normalize it to the int the upstream
    selected, or the affinity never moves. A non-integral float, a string, or
    a bool (which would alias page 1) names no page -- None.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _in_isolated_context(args: dict[str, Any]) -> bool:
    """True for a `new_page` into an isolated browser context.

    Such a tab is invisible to Chrome extensions (`chrome.debugger.getTargets()`
    does not list it), so it cannot be put in a tab group; it is left out on
    purpose, not as a failure.
    """
    return args.get("isolatedContext") is not None


def _no_page_result() -> types.CallToolResult:
    """The upstream's own no-page error, returned for a page-scoped call from a
    connection that owns no page -- forwarding it would touch another client's
    tab via the shared global selection."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{_NO_PAGE_MARKER}. Open a page first.")],
        is_error=True,
    )


def _renew_requires_agent_result() -> types.CallToolResult:
    """`renew_page` resolves its target from the per-AGENT affinity registry, so
    a connection presenting no agent id (a legacy wrapper client) has no page
    to renew. Refused locally instead of forwarded: the name is daemon-owned,
    so the upstream would only answer with a less legible unknown-tool error."""
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text=f"{RENEW_PAGE_TOOL} requires an agent identity")
        ],
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
    from the per-agent registry (see page_lifecycle);
    requests without one fall back to the caller-supplied per-connection page.

    ``pages`` and ``tab_groups`` are built once by ``run()`` for this daemon process.
    """

    def __init__(
        self,
        upstream: ClientSession,
        pages: PageRegistry,
        tab_groups: AgentTabGroups,
    ) -> None:
        self._upstream = upstream
        self.pages = pages
        self.tab_groups = tab_groups
        self._lock = asyncio.Lock()
        self._tools: list[types.Tool] | None = None
        # Set when an upstream call hits a closed session; run() watches it to
        # terminate this unusable daemon.
        self.dead = asyncio.Event()

    async def _call(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        """Forward to the upstream, turning a dead session into a clean signal:
        flag the daemon dead so run() exits, and raise a readable error
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
        # Daemon-owned tool appended after the upstream's verbatim passthrough:
        # `chrome-devtools-mcp` has no TTL concept, so `renew_page` is
        # implemented entirely on this side (see `page_lifecycle`).
        return [t.model_dump(mode="json", by_alias=True) for t in self._tools] + [
            renew_page_tool_dump()
        ]

    async def call_tool(
        self, name: str, args: dict[str, Any], current_page: int | None
    ) -> tuple[types.CallToolResult, int | None]:
        """Forward one call under the serial lock, pinned to `current_page`.

        Returns the result and the connection's updated current page.
        """
        if name == RENEW_PAGE_TOOL:
            # `renew_page` is daemon-owned and per-AGENT; a connection without
            # an agent id has no page to renew (see `_renew_requires_agent_result`).
            return _renew_requires_agent_result(), current_page
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
        path. `renew_page` (daemon-owned, see `page_lifecycle`) is resolved
        here without an upstream call.
        """
        if name == RENEW_PAGE_TOOL:
            # Daemon-owned tool, handled entirely on this side (no upstream
            # round-trip); `renew_agent_page` takes the serial lock itself.
            return await renew_agent_page(self, agent_id, args)
        async with self._lock:
            page = self.pages.get_agent_page(agent_id)
            result, updated = await self._affinity_call(name, args, page, agent_id)
            self.pages.set_agent_page(agent_id, updated)
            return result

    async def _repin(self, current_page: int | None) -> int | None:
        """Re-pin to this connection's page so the call lands on the right tab.

        If the page vanished (closed underneath / crashed), re-pin errors: drop to the
        no-current path rather than hit the global selection.
        """
        if current_page is None:
            return None
        repin = await self._call("select_page", {"pageId": current_page})
        if repin.is_error:
            logger.warning(
                f"[browser-mcp] re-pin to page {current_page} failed "
                f"({_text_of(repin)!r}); dropping affinity"
            )
            return None
        return current_page

    async def _new_page(self, args: dict[str, Any], agent_id: int | None) -> types.CallToolResult:
        """Forward a `new_page` and do the bookkeeping for the page it created.

        The one place a page comes into being (an explicit `new_page`, or the
        auto-created page of a page-less first navigate): the created page gets
        its TTL deadline, and, when the caller is an agent, is filed into that
        agent's tab group. Grouping is presentation only and reports its own
        failures (see `AgentTabGroups`); it can never fail the page creation.
        """
        group = agent_id is not None
        if group and _in_isolated_context(args):
            group = False
            logger.info(
                "[browser-mcp] agent {}'s page in isolated context {!r} is not tab-grouped "
                "(extensions cannot see isolated-context tabs)",
                agent_id,
                args["isolatedContext"],
            )
        before = await self.tab_groups.snapshot() if group else None
        result = await self._call("new_page", args)
        if not result.is_error:
            self.pages.register_created_page(_selected_id(result))
            if group and agent_id is not None:
                await self.tab_groups.group_new_page(agent_id, before)
        return result

    async def _bootstrap_page(
        self, name: str, args: dict[str, Any], *, agent_id: int | None
    ) -> tuple[types.CallToolResult, int | None]:
        """No tab of our own to act on. navigate_page can bootstrap one; anything else fails
        fast instead of touching another client's tab."""
        if name == "navigate_page" and isinstance(args.get("url"), str):
            result = await self._new_page(args, agent_id)
            return result, (_selected_id(result) or None)
        return _no_page_result(), None

    async def _affinity_call(
        self,
        name: str,
        args: dict[str, Any],
        current_page: int | None,
        agent_id: int | None = None,
    ) -> tuple[types.CallToolResult, int | None]:
        page_scoped = name not in _MANAGEMENT_TOOLS

        if page_scoped:
            current_page = await self._repin(current_page)
            if current_page is None:
                return await self._bootstrap_page(name, args, agent_id=agent_id)

        # A page created here gets its TTL slot (see `page_lifecycle`) and tab
        # group in `_new_page`; a listing that drifted off the parseable shape
        # registers nothing.
        if name == "new_page":
            result = await self._new_page(args, agent_id)
        else:
            result = await self._call(name, args)
        if name == "close_page" and not result.is_error:
            # A clean close drops the TTL slot with the page; the shared
            # `_page_id` normalization accepts an integral-float pageId too
            # (bool is rejected so a JSON `true` can never alias page id 1).
            self.pages.drop_page_ttl(_page_id(args.get("pageId")))
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
            pid = _page_id(args.get("pageId"))
            return pid if pid is not None else current_page
        if name == "close_page" and args.get("pageId") == current_page:
            return None
        # navigate_page (forwarded) and every other page-scoped op stay re-pinned.
        return current_page


_ConnPage = int | None  # the connection's current page


async def _call_tool_response(
    daemon: ChromeMcpDaemon, req: Request, req_id: Any, conn_page: _ConnPage
) -> tuple[Response, _ConnPage]:
    """Serve a `call_tool` request; the response and the connection's page afterwards."""
    tool = req.get("tool")
    if not tool:
        # Missing/empty tool name: reject at the protocol edge
        # instead of forwarding a nameless call the upstream
        # would reject with a less legible error.
        return {
            "id": req_id,
            "ok": False,
            "error": "call_tool requires a non-empty 'tool' name",
        }, conn_page
    agent_id = req.get("agent_id")
    # bool is an int subclass — reject it so a JSON
    # `true` can never alias another agent's slot.
    if isinstance(agent_id, int) and not isinstance(agent_id, bool):
        result = await daemon.call_tool_for_agent(tool, req.get("args") or {}, agent_id)
    else:
        result, conn_page = await page_lifecycle.forward_legacy_call(
            daemon, tool, req.get("args") or {}, conn_page
        )
    return {"id": req_id, "ok": True, "result": result.model_dump(mode="json")}, conn_page


async def _respond(
    daemon: ChromeMcpDaemon, req: Request, req_id: Any, method: Any, conn_page: _ConnPage
) -> tuple[Response, _ConnPage]:
    """Serve one request on a live daemon; one bad call must not drop the whole connection."""
    try:
        if method == "list_tools":
            return {"id": req_id, "ok": True, "result": await daemon.list_tools()}, conn_page
        if method == "call_tool":
            return await _call_tool_response(daemon, req, req_id, conn_page)
        if method == "release_agent_page":
            return await handle_release_agent_page(daemon, req, req_id), conn_page
        return {"id": req_id, "ok": False, "error": f"Unknown method: {method}"}, conn_page
    except Exception as e:
        return {"id": req_id, "ok": False, "error": f"{type(e).__name__}: {e}"}, conn_page


async def _serve_line(
    line: bytes,
    writer: asyncio.StreamWriter,
    daemon_ref: list[ChromeMcpDaemon | None],
    conn_page: _ConnPage,
) -> _ConnPage:
    """Parse, serve and answer one request line; the connection's page afterwards."""
    try:
        req: Request = json.loads(line)
    except json.JSONDecodeError as e:
        _write(writer, {"id": None, "ok": False, "error": f"JSON parse error: {e}"})
        await writer.drain()
        return conn_page

    req_id, method = req.get("id"), req.get("method")

    # ping is lock-free and must succeed while the upstream connects:
    # the healthcheck probes the daemon process itself (not the
    # upstream), so an initial connect window should never trigger a
    # false-positive death and unnecessary respawn.
    if method == "ping":
        _write(writer, {"id": req_id, "ok": True, "result": None})
        await writer.drain()
        return conn_page

    daemon = daemon_ref[0]
    if daemon is None or daemon.dead.is_set():
        # The initial upstream connection is not ready.
        _write(writer, {"id": req_id, "ok": False, "error": _UPSTREAM_DOWN_MSG})
        await writer.drain()
        return conn_page

    resp, conn_page = await _respond(daemon, req, req_id, method, conn_page)
    _write(writer, resp)
    await writer.drain()
    return conn_page


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    daemon_ref: list[ChromeMcpDaemon | None],
) -> None:
    """One agent bridge connection. Requests carrying an `agent_id` resolve
    their page from the per-agent registry (a fresh connection — exec
    subprocess child — inherits the agent's page); the rest track their own
    page (see ``forward_legacy_call``). `daemon_ref` is a
    mutable cell so the handler always sees the current daemon across
    connects; when None (starting), it returns a pre-execution error."""
    conn_page: _ConnPage = None
    try:
        with suppress(ConnectionResetError, BrokenPipeError):
            while line := await reader.readline():
                conn_page = await _serve_line(line, writer, daemon_ref, conn_page)
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


def _write(writer: asyncio.StreamWriter, obj: Response) -> None:
    writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())


async def _await_death_or_stop(daemon: ChromeMcpDaemon, stop: asyncio.Event) -> None:
    """Wait for daemon.dead or stop signal, cleaning up tasks on exit."""
    async with asyncio.TaskGroup() as waiters:
        dead_task = waiters.create_task(daemon.dead.wait())
        stop_task = waiters.create_task(stop.wait())
        try:
            await asyncio.wait({stop_task, dead_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            dead_task.cancel()
            stop_task.cancel()


async def _upstream_watchdog(daemon: ChromeMcpDaemon, stop: asyncio.Event) -> None:
    """Periodically ping the upstream session; a wedged one (no reply within the
    timeout) sets `dead` so run() exits even with no client calling."""
    # quiesce-exempt: watches the upstream browser process; no database
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                daemon._upstream.send_ping(), timeout=_UPSTREAM_WATCHDOG_TIMEOUT_S
            )
        except (TimeoutError, MCPError) as e:
            if stop.is_set():
                return
            logger.error(
                f"[browser-mcp] upstream watchdog: {type(e).__name__} — terminating the daemon"
            )
            daemon.dead.set()
            return
        except Exception as e:
            if stop.is_set():
                return
            logger.warning(f"[browser-mcp] upstream watchdog ping failed: {e!r}; retrying")
        await _await_stop_or_timeout(stop, _UPSTREAM_WATCHDOG_INTERVAL_S)


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
    with suppress(OSError):
        await writer.wait_closed()
    del reader  # nothing to close on a StreamReader; the writer close suffices
    return True


async def run(*, browser_cdp_port: int, connect_timeout_reader: Callable[[], float]) -> None:
    """Start the browser-mcp daemon.

    One upstream connection serves this process. Creation failure or upstream
    death ends the process; no browser action or session is automatically replayed.
    """
    browser_url = f"http://127.0.0.1:{browser_cdp_port}"
    sock = chrome_mcp_socket()
    # Single-instance guard: the old code unlinked the socket unconditionally,
    # so a second daemon (watchdog misjudged the first dead and respawned)
    # stole the socket out from under the live instance — its clients all
    # disconnected and the first process became an orphan (audit round 2, P1).
    # Probe before unlink: a live listener means an instance is already
    # serving; refuse to start instead of stealing.
    if await _socket_in_use(sock):
        logger.error(
            "[browser-mcp] socket {} is already served by a live daemon — "
            "refusing to start a second instance",
            sock,
        )
        raise SystemExit(1)
    with suppress(OSError):
        sock.unlink()

    # Mutable reference so _handle_client always sees the current daemon across
    # initial upstream connection. None while starting.
    daemon_ref: list[ChromeMcpDaemon | None] = [None]

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, daemon_ref), path=str(sock), limit=_LINE_LIMIT
    )
    logger.info(f"[browser-mcp] listening on {sock} (upstream {browser_url})")

    # Composition root of the shared page/session state: built once, handed to
    # the ChromeMcpDaemon this process serves. One TaskGroup owns
    # the daemon-long tasks (the session refresh loop and the gateway session's
    # fire-and-forget one-shots and the tab-grouping tasks); the shutdown path cancels
    # them before it closes.
    pages = PageRegistry()
    stop = stop_on_signals()
    async with asyncio.TaskGroup() as tasks:
        gateway = GatewaySession(tasks)
        tab_groups = AgentTabGroups(browser_cdp_port, tasks)
        session_task = tasks.create_task(
            contained(gateway.refresh_loop(stop), "gateway session refresh")
        )
        current_stack: AsyncExitStack | None = None
        try:
            try:
                session, current_stack = await _create_upstream(
                    browser_url, stop, connect_timeout_reader=connect_timeout_reader
                )
            except _StoppingError:
                return
            daemon = ChromeMcpDaemon(session, pages, tab_groups)
            daemon_ref[0] = daemon
            await _serve_generation(daemon, stop, gateway)
            if daemon.dead.is_set():
                raise RuntimeError("browser-mcp upstream died; explicit service restart required")
        finally:
            logger.info("[browser-mcp] shutting down")
            session_task.cancel()
            gateway.cancel_one_shots()
            tab_groups.cancel_pending()
            # Two bounded steps, so the daemon may keep closing for up to
            # `shutdown_budget.SHUTDOWN_CEILING_S`; ava-root's window for this unit
            # is derived from that total.
            await _bounded_stack_close(current_stack, "upstream stack close during shutdown")
            server.close()
            await _bounded(server.wait_closed(), "server close")
            with suppress(OSError):
                sock.unlink()


async def _serve_generation(
    daemon: ChromeMcpDaemon, stop: asyncio.Event, gateway: GatewaySession
) -> None:
    """Serve one upstream connection until it dies or the stop event fires.

    The upstream watchdog and the expired-page reaper run per connection, so they live
    in a group that closes with it.
    """
    async with asyncio.TaskGroup() as helpers:
        watchdog = helpers.create_task(
            contained(_upstream_watchdog(daemon, stop), "upstream watchdog")
        )
        reaper = helpers.create_task(
            contained(expired_page_reaper(daemon, stop), "expired-page reaper")
        )
        # Chrome is confirmed up here — inject the gateway session cookie
        # right away (the periodic loop covers expiry; this covers the
        # fresh-profile cold start and the post-restart gap without
        # waiting for the next tick).
        gateway.spawn_inject()
        try:
            await _await_death_or_stop(daemon, stop)
        finally:
            watchdog.cancel()
            reaper.cancel()


def main() -> None:
    # Same boot seam every long-running service shares (cf. computer-mcp): a
    # per-daemon log file plus the event pipeline, so this daemon's expiry /
    # renewal events land attributed to `browser-mcp` and an uncaught
    # traceback is postmortem-able. Idempotent.
    image = LoadedCommit.capture()
    config = ConfigBoot()
    config.boot()
    machine = machine_from_boot(config)
    process_home = ava_home().resolve()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="browser-mcp")

    def database() -> Database:
        return Database(
            db_config_from_boot(config), gate=gate, direct_url=ClusterDirectUrl(machine.host)
        )

    pipeline = build_pipeline(database=database, machine=machine, home=process_home)
    init_gateway_process(
        name="browser-mcp",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
    )
    asyncio.run(
        run(
            browser_cdp_port=config.view.services.browser_cdp_port,
            connect_timeout_reader=lambda: config.view.sandbox.mcp_connect_timeout_seconds,
        )
    )


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
