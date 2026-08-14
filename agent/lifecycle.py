"""Process lifecycle helpers run in `main()`'s finally block + signal handlers.

Covers:
- `_exit_reason` — derive a tag for the `process_exit` event from sys.exc_info()
- `_install_lifecycle_signal_handlers` — SIGHUP / SIGTERM → SystemExit
- `_notify_exit` — tell the gateway this process has exited so it finalizes
  status='terminated' + closes pages (the gateway owns agents_meta + the
  pages table; the agent only notifies)
- `_route_process_end_notify` — route the finally-block notify by exit reason
  (hibernation swap-out parks 'hibernating', everything else finalizes)

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

# The hibernation swap-out signal. Distinct from SIGTERM (which means "die" ->
# 'terminated'): SIGUSR1 means "swap your process out to free RAM" -> the finally
# routes the exit notify to /hibernating (park as 'hibernating', keep pages + the
# checkpoint, no exit event) instead of /exited. Both signals convert to
# SystemExit through the SAME handler so main()'s finally still drains cleanly;
# the exit-reason STRING is what tells them apart downstream. POSIX-only.
_HIBERNATE_SIGNAL_NAME = "SIGUSR1"
# The `_exit_reason()` tag for a hibernation swap-out. main()'s finally routes on
# it; kept distinct from the raw "signal:SIGUSR1" so the process_exit event reads
# as an intentional swap-out, not a silent death.
HIBERNATE_EXIT_REASON = "hibernate"

# Set True (synchronously, in the SIGUSR1 handler) to mark this process's exit as
# a hibernation swap-out. A module flag — NOT the exception message — is the
# reliable channel because asyncio converts the handler's SystemExit into a task
# CancelledError before main()'s coroutine finally runs, so by then sys.exc_info()
# shows CancelledError, not SystemExit("signal:SIGUSR1"). The flag is set before
# the exception propagates, so it survives that conversion. It is the SINGLE
# routing channel: the SystemExit message string is only a diagnostic tag and is
# deliberately not part of the exit-reason protocol (the audit D2 finding — the
# string as a second, hidden channel required touching flag + string + route in
# lockstep for every new signal). Process-global and one-shot: the process exits
# right after, so it is never reset in production (tests that invoke the handler
# reset it explicitly).
_hibernate_requested = False


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
    # The hibernation flag is the single routing channel: asyncio turns the
    # SIGUSR1 handler's SystemExit into a task CancelledError before this finally
    # runs, so the exception below would otherwise read as
    # 'exception:CancelledError' and route to /exited. The flag, set in the
    # handler before the exception propagates, survives that conversion. The
    # SystemExit message string is NOT part of the routing protocol — a bare
    # SystemExit("signal:SIGUSR1") without the flag reads as the raw signal tag,
    # never as a hibernation swap-out.
    if _hibernate_requested:
        return HIBERNATE_EXIT_REASON
    exc = sys.exc_info()[1]
    if isinstance(exc, SystemExit):
        msg = str(exc) if exc.args else ""
        return msg if msg.startswith("signal:") else "system_exit"
    if exc is not None:
        return f"exception:{type(exc).__name__}"
    return "normal"


def _install_lifecycle_signal_handlers() -> None:
    """Convert SIGHUP / SIGTERM / SIGUSR1 to SystemExit so main()'s finally runs —
    otherwise the default handler kills immediately, finally does not run,
    agents.status stays stuck on 'running' / 'idling', and the process_exit
    event is also missed. SIGTERM is the graceful stop the reap / force path
    delivers to a detached agent; SIGHUP is kept as a defensive catch (the
    159 / 160 silent-death class, where a signal killed python immediately and
    left a ghost row with no one to collect); SIGUSR1 is the hibernation swap-out
    (see `_HIBERNATE_SIGNAL_NAME`) — same convert-to-SystemExit path so the
    process drains cleanly, but `_exit_reason` tags it "hibernate" so the finally
    routes to /hibernating rather than /exited.

    SIGINT untouched: Python's default conversion to KeyboardInterrupt
    already works; don't install twice.
    SIGKILL / SIGSTOP cannot be intercepted — that class of extreme silent
    deaths still leaks; but atexit / finally cover 99% of cases (the
    remaining 1% diagnosable from macOS Console for OOM / jetsam markers).
    """

    hibernate_signum = getattr(signal, _HIBERNATE_SIGNAL_NAME, None)

    def _exit_on_signal(signum: int, _frame: object) -> None:
        # Set the hibernation flag SYNCHRONOUSLY, before raising — asyncio will
        # convert this SystemExit into a CancelledError, so the flag (not the
        # exception) is what tells main()'s finally to park 'hibernating'.
        if hibernate_signum is not None and signum == hibernate_signum:
            global _hibernate_requested  # noqa: PLW0603 — a signal handler has no other channel to main()'s finally
            _hibernate_requested = True
        raise SystemExit(f"signal:{signal.Signals(signum).name}")

    # SIGHUP / SIGUSR1 are POSIX-only (undefined on Windows, where agents do not
    # run); register whichever of the three this platform defines.
    for name in ("SIGHUP", "SIGTERM", _HIBERNATE_SIGNAL_NAME):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _exit_on_signal)


def _notify_exit(agent_id: int) -> None:
    """Tell the gateway this process has reached its exit — it finalizes
    status='terminated' + closes the agent's pages.

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


def _notify_hibernate(agent_id: int) -> None:
    """Tell the gateway this process is swapping out for hibernation — it parks
    the row as 'hibernating' (WHERE status IN running/idling), keeping the agent's
    pages open and writing no exit event, unlike the /exited terminate path.

    main()'s finally routes here (instead of `_notify_exit`) when the exit reason
    is a hibernation swap-out (`HIBERNATE_EXIT_REASON`, set by the SIGUSR1
    handler). The hibernation controller relaunches the parked row when a
    heartbeat/inbound lands. Best-effort like `_notify_exit`: on failure the row
    stays 'idling' (or the transient 'running' the idle-wait finally leaves) with a
    now-dead pid, and the restarter's running/idling corpse-reaper finalizes it to
    'terminated' — a safe fallback (terminated is resurrectable), never a dormant
    sleep-death.
    """
    try:
        ava._gateway_client.hibernating(agent_id)
    except Exception:
        logger.opt(exception=True).warning(
            "agent {agent_id} hibernate-notify to gateway failed (non-fatal; "
            "the running/idling corpse-reaper will finalize to terminated)",
            agent_id=agent_id,
        )


def _route_process_end_notify(agent_id: int, reason: str) -> None:
    """Route the gateway process-end notify by exit reason: a hibernation swap-out
    (reason == HIBERNATE_EXIT_REASON) parks the row 'hibernating'; every other exit
    finalizes 'terminated'. A module helper (rather than inline in main's finally)
    keeps main within its statement budget; it looks up `_notify_exit` /
    `_notify_hibernate` as module globals so test monkeypatches on
    `agent.lifecycle.*` are honoured."""
    if reason == HIBERNATE_EXIT_REASON:
        _notify_hibernate(agent_id)
    else:
        _notify_exit(agent_id)
