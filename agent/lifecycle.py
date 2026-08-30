"""Process lifecycle helpers run in `main()`'s finally block + signal handlers.

Covers:
- `_exit_reason` — derive a tag for the `process_exit` event from sys.exc_info()
- `_install_lifecycle_signal_handlers` — SIGHUP / SIGTERM → SystemExit
- `_notify_exit` — tell the gateway this process has exited so it finalizes
  status='terminated' + closes agent-owned show() pages (daemon-supervised
  serve() pages stay open; the gateway owns agents_meta + the pages table)

The agent process itself is detached and native (spawned via
`ops.agent_launch`); it is stopped with SIGTERM (graceful reap / `ava stop`) or
SIGKILL (force-terminate). Its persistent shell sessions
(`ava-agent-<id>-shell-<n>[-<name>]`) are deliberately NOT torn down on
exit — they persist across terminate/restart/update (each session runs in its
own detached pty host, shared/pty_sessions, so no infra teardown can reach
them) and background work outlives the process that started it. Reclaiming
truly-orphaned sessions is a separate periodic admin concern, not a lifecycle
hook.

All three are called from the main process' finally block so the agent leaves a
clean trail even on signalled exits (the SIGTERM/SIGHUP handlers convert the
signal into SystemExit so finally runs).
"""

from __future__ import annotations

import signal
import sys

import ava
from shared.log import logger


def _exit_reason() -> str:
    """Derive the process_exit event's reason tag from sys.exc_info().

    Called in `main()`'s finally block — at that moment the active exception
    (if any) is exposed by sys.exc_info():

    - SystemExit("signal:NAME"): converted by the handler installed by
      `_install_lifecycle_signal_handlers` from SIGHUP / SIGTERM — i.e. the
      "silent death" path (session closing etc.); reason restores the
      signal name directly.
    - Other SystemExit: ordinary sys.exit (no message / int code) — "system_exit".
    - Other Exception: type name (`exception:RuntimeError` /
      `exception:asyncio.CancelledError` etc.); combined with the traceback
      field automatically stuffed by _postgres_sink, that's enough for
      diagnosis.
    - No exception: graph.ainvoke returned normally (terminate inbound goto END),
      so "normal" — cross-confirmed with the gateway's agent_terminated event
      (emitted by the `/exited` finalize that `_notify_exit` triggers).
    """
    exc = sys.exc_info()[1]
    if isinstance(exc, SystemExit):
        msg = str(exc) if exc.args else ""
        return msg if msg.startswith("signal:") else "system_exit"
    if exc is not None:
        return f"exception:{type(exc).__name__}"
    return "normal"


def _install_lifecycle_signal_handlers() -> None:
    """Convert SIGHUP / SIGTERM to SystemExit so main()'s finally runs —
    otherwise the default handler kills immediately, finally does not run,
    agents.status stays stuck on 'running' / 'idling', and the process_exit
    event is also missed. SIGTERM is the graceful stop the reap / force path
    delivers to a detached agent; SIGHUP is kept as a defensive catch (the
    159 / 160 silent-death class, where a signal killed python immediately and
    left a ghost row with no one to collect).

    SIGINT untouched: Python's default conversion to KeyboardInterrupt
    already works; don't install twice.
    SIGKILL / SIGSTOP cannot be intercepted — that class of extreme silent
    deaths still leaks; but atexit / finally cover 99% of cases (the
    remaining 1% diagnosable from macOS Console for OOM / jetsam markers).
    """

    def _exit_on_signal(signum: int, _frame: object) -> None:
        raise SystemExit(f"signal:{signal.Signals(signum).name}")

    # SIGHUP is POSIX-only (undefined on Windows, where agents do not run);
    # register whichever of the two this platform defines.
    for name in ("SIGHUP", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _exit_on_signal)


def _notify_exit(agent_id: int) -> None:
    """Tell the gateway this process has reached its exit — it finalizes
    status='terminated' + closes its agent-owned show() pages. Daemon-supervised
    serve() pages stay open in their persistent page sessions.

    The gateway owns agents_meta and the pages table; the agent only notifies
    (`POST /api/agents/{id}/exited`). The finalize is guarded server-side so a
    concurrent restart leaves status 'restarting' untouched for the restarter.

    Best-effort: any failure (gateway unreachable during a silent death etc.)
    is logged and swallowed, not raised — the same non-fatal treatment as the
    other cleanup helpers. If the notify never lands, the gateway's own
    zombie-reaping (a later terminate / status probe detecting the dead pid)
    finalizes the row, so the status never petrifies.
    """
    try:
        ava._gateway_client.exited(agent_id)
    except Exception:
        # INFO, not WARNING: best-effort by design and self-healing (the gateway's
        # zombie-reaper finalizes the row), and it fires on every agent killed in a
        # rollout while the gateway is down — expected churn, per the 2026-08-05
        # alerting ruling. The exception stays for debugging.
        logger.opt(exception=True).info(
            "agent {agent_id} exit-notify to gateway failed (non-fatal; "
            "gateway zombie-reaping will finalize)",
            agent_id=agent_id,
        )


async def _route_process_end_notify(agent_id: int, _reason: str) -> None:
    """Every exit finalizes 'terminated' (the exit-reason tag still rides the
    `process_exit` event), then this agent's Chrome page is released so a
    terminated worker leaves no dead tab in the user's shared browser. A module
    helper (rather than inline in main's finally) keeps main within its
    statement budget; it looks up both helpers as module globals so test
    monkeypatches on `agent.lifecycle.*` are honoured."""
    _notify_exit(agent_id)
    await _release_agent_browser_page(agent_id)


async def _release_agent_browser_page(agent_id: int) -> None:
    """Best-effort close of this agent's browser-mcp page at process exit.

    Never raises — the exit path runs once and must not fail on a cleanup
    nicety. A machine without the browser service (no socket) and a down daemon
    both degrade to the service's own dead-page reaper, and the agent's
    persistent shells keep the tab's dev server lifecycle untouched either way.
    """
    try:
        from ava._mcp_browser import release_agent_chrome_pages

        released = await release_agent_chrome_pages(agent_id)
        if not released:
            logger.debug(
                "agent {agent_id} browser-page release skipped (no service / failed)",
                agent_id=agent_id,
            )
    except Exception:
        logger.opt(exception=True).debug(
            "agent {agent_id} browser-page release failed (non-fatal)",
            agent_id=agent_id,
        )
