"""Upstream chrome-devtools-mcp session lifecycle for the shared browser-MCP daemon.

The daemon owns the Unix-socket multiplexer and the per-connection protocol;
this module owns the one upstream session behind it: creating it (raced
against the stop event and the connect timeout), and every teardown await
bounded so a wedged child or SDK cleanup can never hold the daemon past the
operator's stop budget (2026-09-09 #2043).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import AsyncExitStack, suppress
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from shared.config import settings
from shared.log import logger

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

# A wedged (not dead) upstream call must not hold the serial lock forever and
# freeze every client; bound each upstream request. Generous so a slow real
# navigation never trips it -- only a true hang does.
_READ_TIMEOUT = timedelta(seconds=180)

# Every teardown await is bounded by this budget: after SIGTERM the daemon must
# exit even when a cleanup step wedges (a stuck upstream stack close, a
# session loop mid-request). A step that overruns is logged and shutdown
# continues — the process exits anyway, so no live resource is ever reported
# as stopped (2026-09-09 #2043).
_SHUTDOWN_STEP_TIMEOUT_S = 10.0


class _StoppingError(Exception):
    """The stop event fired while the upstream was connecting."""


async def _bounded(awaitable: Awaitable[object], what: str) -> None:
    """Await a cleanup step within ``_SHUTDOWN_STEP_TIMEOUT_S``.

    A cleanup step that wedges (a stack close waiting on a stuck child, a
    server close racing a half-dead loop) must not hold the daemon past the
    operator's stop budget. On overrun the step is cancelled, the incomplete
    close is logged as such, and shutdown continues.
    """
    try:
        await asyncio.wait_for(awaitable, timeout=_SHUTDOWN_STEP_TIMEOUT_S)
    except TimeoutError:
        logger.warning(
            "[browser-mcp] %s did not finish within %.0fs; continuing shutdown",
            what,
            _SHUTDOWN_STEP_TIMEOUT_S,
        )


async def _bounded_stack_close(stack: AsyncExitStack | None, what: str) -> None:
    """Close an upstream stack within the shutdown budget; never raises."""
    if stack is None:
        return
    with suppress(Exception):
        await _bounded(stack.aclose(), what)


async def _create_upstream(
    browser_url: str, stop: asyncio.Event
) -> tuple[ClientSession, AsyncExitStack]:
    """Create a new chrome-devtools-mcp upstream session.

    Returns (session, stack) — the caller owns the stack and must close it on
    teardown. The stack owns the subprocess + stdio pipes; when aclosed, the
    npx child is terminated.

    ``session.initialize()`` is raced against both the configured connect
    timeout and the ``stop`` event: SIGTERM during a hung connect interrupts
    the connect immediately instead of waiting out the full connect timeout.
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
        await _initialize_until_stop_or_timeout(session, stop)
        return session, stack
    except BaseException:
        # The failed connect's stack close is bounded too: a stuck child
        # termination must not wedge the reconnect loop (2026-09-09 #2043).
        await _bounded_stack_close(stack, "upstream stack close after failed connect")
        raise


async def _initialize_until_stop_or_timeout(session: ClientSession, stop: asyncio.Event) -> None:
    """Run ``session.initialize()`` until it returns, the stop event fires, or
    the configured connect timeout elapses — whichever comes first.

    SIGTERM during a hung connect wins the race immediately instead of waiting
    out the full connect timeout; a timeout cancels the initialize task so the
    failed connect's stack close can tear the child down cleanly.
    """
    init_task = asyncio.create_task(session.initialize())
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait(
            {init_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=settings.sandbox.mcp_connect_timeout_seconds,
        )
    finally:
        stop_task.cancel()
        with suppress(BaseException):
            await stop_task
    if stop.is_set():
        init_task.cancel()
        with suppress(BaseException):
            await init_task
        raise _StoppingError
    if not init_task.done():
        init_task.cancel()
        with suppress(BaseException):
            await init_task
        raise TimeoutError("upstream session.initialize() timed out")
    await init_task  # re-raise the initialize outcome, or return cleanly
