"""One-shot agent-stack warm-up — pay the first agent's cold-start at `ava start`.

On a fresh box the *first* agent process is slow: it cold-imports the heavy boot
chain (langgraph + the Anthropic client + the ava SDK), cold-spawns its MCP
daemon subprocess, and -- when the shared browser is on -- pays the one-time
`chrome-devtools-mcp` npx download plus a first CDP handshake. None of this is
cached on a box that has only just come up, so the first spawn after a fresh
`ava start` lags well behind every spawn after it.

This module does that work once, eagerly, so the OS page cache (and the npx
package cache) are already warm by the time a real agent spawns. `ava start`
fires it detached on every agent-runner-capable host
(`cli/commands/_warmup.py`).

Every step is best-effort: a warm-up failure is logged and swallowed, never
raised. The fallback is exactly today's behavior -- the first agent pays the
cold cost itself -- so a broken warm-up can only fail to *help*, never break a
spawn. That is why the defensive try/except here is correct rather than a
fail-fast violation: the "happy path" being guarded is a pure optimization whose
absence is the tested status quo.

Run: .venv/bin/python -m agent.warmup
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path

from shared.config import settings
from shared.log import logger
from shared.platform_probes import browser_incapability

# Warm-up's own MCP daemon socket. A fixed name (not an agent_id) so it never
# collides with a real agent's `mcp_daemon.<agent_id>.sock`; cleaned up after.
_WARMUP_SOCKET = "mcp_daemon.warmup.sock"


def _warm_imports() -> None:
    """Import the heavy boot chain a real agent imports, to prime the page cache.

    `agent.loop` pulls in langgraph + the chat model + the ava SDK -- exactly the
    chain the agent's `from .loop import run` triggers after it claims its row.
    Importing it here (in a throwaway process) warms the same `.pyc`/`.so` pages
    the first agent will read.

    Uses `import_module` (not a bare `import`) because the modules are loaded
    purely for that side effect -- no name is bound -- so a bare import reads as
    dead to the linters.
    """
    importlib.import_module("agent.loop")
    importlib.import_module("ava._mcps_daemon")  # the per-agent MCP daemon module


async def _warm_mcp_daemon() -> None:
    """Spawn the MCP daemon subprocess once and wait until it is ready, then stop.

    Mirrors `python -m ava._mcps_daemon <sock>` on a throwaway socket: read its
    one ready line, terminate. Warms the daemon's fork/exec + import +
    socket-bind path so the per-machine shared daemon (ops roster session
    "mcp-daemon") comes up from warm caches on a fresh box.
    """
    from shared.paths import run_dir as _run_dir

    sock = str(_run_dir() / _WARMUP_SOCKET)
    with suppress(OSError):
        Path(sock).unlink()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ava._mcps_daemon",
        sock,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        assert proc.stdout is not None  # noqa: S101
        await asyncio.wait_for(
            proc.stdout.readline(), timeout=settings.sandbox.mcp_daemon_start_timeout_seconds
        )
    finally:
        with suppress(ProcessLookupError):
            proc.terminate()
        with suppress(Exception):
            await asyncio.wait_for(
                proc.wait(), timeout=settings.sandbox.mcp_daemon_stop_timeout_seconds
            )
        with suppress(OSError):
            Path(sock).unlink()


def _cdp_live(port: int) -> bool:
    """True if the shared browser's CDP endpoint answers on `port` right now."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


async def _warm_browser() -> None:
    """Connect the chrome MCP bridge once: confirm the shared service answers.

    The shared browser + the shared chrome MCP daemon launch as their own
    services in the same `ava start`; they take a few seconds to come up. Poll
    until CDP answers, then connect the chrome MCP bridge (which dials the shared
    daemon's socket) and list its tools. `list_tools` is read-only, so this
    leaves no stray browser tab. If CDP never comes up within the window, skip --
    best-effort. The one-time `chrome-devtools-mcp` npx download now happens in
    the daemon's own startup, not per agent.
    """
    port = settings.services.browser_cdp_port
    deadline = settings.sandbox.mcp_daemon_start_timeout_seconds
    waited = 0.0
    while waited < deadline and not _cdp_live(port):
        await asyncio.sleep(1.0)
        waited += 1.0
    if not _cdp_live(port):
        logger.info("[warmup] browser CDP not up within {}s, skipping browser warm", deadline)
        return

    from ava._mcps_daemon import _connect_server

    session, stack = await _connect_server("chrome")
    try:
        await session.list_tools()
    finally:
        await stack.aclose()


def main() -> int:
    """Warm the agent boot stack: heavy imports, the MCP daemon, then (if the
    shared browser is enabled) the chrome MCP server. Each step is independent
    and best-effort; always returns 0."""
    logger.info("[warmup] warming agent boot stack")
    # On Windows the default ProactorEventLoop is incompatible with psycopg's
    # async wait — see agent/loop.py for the full rationale.
    import sys as _sys

    if _sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        _warm_imports()
    except Exception as e:
        logger.warning("[warmup] import warm failed: {}", e)
    try:
        asyncio.run(_warm_mcp_daemon())
    except Exception as e:
        logger.warning("[warmup] mcp daemon warm failed: {}", e)
    if settings.services.browser_enabled:
        incap = browser_incapability()  # display + Chrome + npx, or the reason it's missing
        if incap is None:
            try:
                asyncio.run(_warm_browser())
            except Exception as e:
                logger.warning("[warmup] browser warm failed: {}", e)
        else:
            logger.info("[warmup] browser warm skipped: {}", incap)
    logger.info("[warmup] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
