"""Agent main loop — thin launcher.

Each OS process runs one agent, permanently bound to one agent_id.
Entry point `python -m agent --agent-id N`:

1. argparse picks up `--agent-id N` (required, no default)
2. UPDATE N from unclaimed 'idling' to 'running' in `agents_meta` (also fills
   pid + started_at) → 0-row affected raises (does not exist / not an unclaimed
   idling row / claimed by another process). 'running' covers both bootstrap and
   the run loop.
3. ainvoke graph once per TURN — the graph self-cycles within a turn (claim →
   before_llm → llm → before_exec → exec → after_exec), and claim ends the
   invocation at the turn boundary (goto END, `exit_requested=False`); the
   runloop re-invokes on the same checkpointer thread, so each turn gets its
   own root span/trace. The fresh invocation's claim long-awaits on Redis pub/sub
   for inbound, flipping agents_meta.status from
   'running' to 'idling', back to 'running' after claiming a batch.
4. On receiving a `terminate` kind inbound (unless a newer/unseen message
   vetoes it — see the veto block in `agent/graph/_claim.py`), claim → goto
   END with `exit_requested=True` → the runloop returns instead of re-invoking.
5. After the runloop returns, notify the gateway (`POST /api/agents/{id}/exited`)
   to finalize status='terminated' + close agent-owned show() pages; daemon-
   supervised serve() pages continue in persistent page sessions.

New processes don't start directly — always go through gateway route
`POST /api/agents`; the gateway internally creates a row via
`gateway/agents.py:spawn_agent` starts this process in a session.
SDK / frontend / `scripts/start_agent.py` all three entry points share this
HTTP path.

Sub-modules (the loop file split by boot / run-loop / exit):
- `agent/_starting.py` — early row claim (run from `__main__.py` before the heavy imports)
- `agent/_process_boot.py` — one-shot boot phases (framework config, MCP daemon, SDK identity, data plane, checkpointer, graph build)
- `agent/_runloop.py` — graph run loop (ainvoke lifecycle logging, fatal-LLM abort, DB-outage pause-and-recover)
- `agent/mcp_daemon.py` — `_MCPDaemon` subprocess manager + socket path helper
- `agent/startup.py` — one-shot startup helpers (saver wrap / reconcile inbounds / effective config / screen-capture notify)
- `agent/lifecycle.py` — process-exit helpers (exit_reason / signal handlers / notify routing / notify_exit)
"""

import argparse
import asyncio
import contextlib
import os
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool

from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.db import PG_KEEPALIVE_KWARGS
from shared.log import logger

from . import _boot_timing
from ._process_boot import (
    _boot_agent_process,
    _build_checkpointer,
    _build_data_plane,
    _build_graph,
)
from ._runloop import (
    _invoke_graph_with_lifecycle_logging,
    _probe_db_reachable,
    _wait_for_db_recovery,
)
from .db import LoggingConnectionPool
from .graph._context import AvaContext
from .lifecycle import (
    _exit_reason,
    _install_lifecycle_signal_handlers,
    _notify_exit,
    _route_process_end_notify,
)
from .mcp_daemon import _MCP_SOCKET_DIR, _mcp_socket_path, _MCPDaemon
from .startup import (
    _notify_screen_capture_at_startup,
    _reconcile_claimed_inbounds_at_startup,
    _repair_dangling_tool_use_at_startup,
    _wrap_saver_writes_with_loud_failure,
    _write_effective_config_to_restart_completed,
    page_reconcile_loop,
    reconcile_open_pages,
)

# Re-exports for tests + external callers — tests/agent/test_loop.py and
# tests/agent/test_loop_main.py import/`monkeypatch.setattr` every private
# helper by name from this module, so the boot/run-loop split keeps the same
# public surface via re-import. Note: monkeypatches on a name whose CALLER
# also moved (e.g. `_wait_for_db_recovery` inside `_recover_from_db_outage`)
# must target the defining module (`agent._runloop.*`) — a re-export only
# covers direct callers in THIS module.
__all__ = [
    "_MCP_SOCKET_DIR",
    "_MCPDaemon",
    "_emit_boot_failure",
    "_exit_reason",
    "_install_lifecycle_signal_handlers",
    "_invoke_graph_with_lifecycle_logging",
    "_mcp_socket_path",
    "_notify_exit",
    "_notify_screen_capture_at_startup",
    "_probe_db_reachable",
    "_reconcile_claimed_inbounds_at_startup",
    "_repair_dangling_tool_use_at_startup",
    "_route_process_end_notify",
    "_wait_for_db_recovery",
    "_wrap_saver_writes_with_loud_failure",
    "_write_effective_config_to_restart_completed",
    "main",
    "page_reconcile_loop",
    "run",
]


def _emit_boot_failure(agent_id: int, exc: BaseException) -> None:
    try:
        model = turn_settings.lm.llm_model
    except Exception:
        model = "<unknown>"

    try:
        from shared import telemetry

        telemetry.emit(
            category="telemetry",
            event_name="agent_boot_failed",
            level="error",
            agent_id=agent_id,
            attributes={
                "model": model,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
    except Exception:
        logger.warning(
            "agent {agent_id} boot-failure telemetry emit failed",
            agent_id=agent_id,
            exc_info=True,
        )


async def _renew_agent_lease_loop(pool: AsyncConnectionPool, agent_id: int) -> None:
    """Re-arm this agent's lease every `AGENT_LEASE_RENEW_INTERVAL_S`.

    The renewal is the one constant of the process — it runs while the agent
    works and while it idles, so an unexpired `lease_expires_at` reads as "the
    process is alive" to the quiesce, the reaper and the frontend (R1,
    Task #1021). A failed renewal logs and retries next interval: the TTL is
    10x the interval, so a transient DB blip is never fatal; a process that
    cannot renew at all is a zombie by definition and the reaper collects it —
    though after a DB outage the reaper's lease-zombie pass holds a grace
    window first, so this paused-but-alive process gets time to renew
    (2026-08-08 audit P1-2).
    """
    from agent.db import renew_agent_lease
    from shared.deploy_timing import AGENT_LEASE_RENEW_INTERVAL_S

    while True:
        await asyncio.sleep(AGENT_LEASE_RENEW_INTERVAL_S)
        try:
            await renew_agent_lease(pool, agent_id)
        except Exception:
            logger.warning(
                "agent {agent_id} lease renewal failed — the reaper will collect "
                "this process if it cannot renew",
                agent_id=agent_id,
                exc_info=True,
            )


async def main(
    agent_id: int,
    config_overlay: dict[str, object] | None = None,
    birth_config: dict[str, object] | None = None,
) -> None:
    try:
        mcp_daemon, llm = await _boot_agent_process(agent_id, config_overlay, birth_config)
    except Exception as exc:
        _emit_boot_failure(agent_id, exc)
        raise
    inbound_listener, event_publisher = await _build_data_plane(agent_id)

    try:
        # One pool + one dedicated Redis pub/sub subscription. Pool gives us
        # automatic health check + reconnect on idle eviction by remote PG /
        # PgBouncer — single AsyncConnection silently dies after server idle
        # timeout, dropping all subsequent SQL on the floor (agent 57 lost 5h
        # of checkpoints from a 5h47m idle). The
        # `check=AsyncConnectionPool.check_connection` callback SELECT 1's every
        # borrowed conn so callers always get a live one.
        #
        # A single pool (max_size=2) carries BOTH consumers — langgraph
        # checkpoint SQL and kernel ad-hoc SQL (inbound queue, agents_meta CAS):
        # - The saver serializes all its SQL through an internal lock (never
        #   holds more than one conn) and opens every cursor with
        #   `row_factory=dict_row` itself, so it needs no conn-level row factory.
        # - Kernel ops read rows positionally, so the pool keeps psycopg's
        #   default tuple rows.
        # - `autocommit=True` serves both: the saver expects it, and ops cursor
        #   execute commits at once (matches the old AsyncConnection that
        #   callers manually `.commit()`d). `prepare_threshold=None` (never
        #   prepare — psycopg3's `0` prepares immediately, which collides across
        #   pooled backends) is what keeps the borrows transaction-pooling-safe.
        # Budget: max_size=2 Postgres connections per agent (listener uses
        # Redis, not PG).
        async with LoggingConnectionPool[psycopg.AsyncConnection](
            settings.data_plane.db_url,
            pool_name="agent",
            min_size=1,
            max_size=2,
            # keepalive/connect_timeout (PG_KEEPALIVE_KWARGS) bound how long a
            # borrowed conn on a half-dead socket hangs — the laptop-sleep case —
            # so check_connection's SELECT 1 fails fast instead of on the OS TCP
            # timeout. autocommit/prepare_threshold serve the saver + ad-hoc ops.
            kwargs={"autocommit": True, "prepare_threshold": None, **PG_KEEPALIVE_KWARGS},
            check=AsyncConnectionPool.check_connection,
            timeout=settings.agent.db_pool_acquire_timeout_seconds,
            open=False,
        ) as db_pool:
            checkpointer = await _build_checkpointer(db_pool, agent_id)
            # Probe + restore open pages (re-serve dead serve_dir pages, close
            # dead no-dir pages) — see reconcile_open_pages. Best-effort,
            # before the graph goes live so links work the moment the agent
            # wakes; the heartbeat re-runs it and the periodic
            # page_reconcile_loop below covers busy agents while it lives.
            await reconcile_open_pages(db_pool, agent_id, event_publisher=event_publisher)
            # R1 (Task #1021): rebuild watchers whose sessions a stop / rollout
            # reaped — the #1014 fix. A registry row with a missing session is
            # either a standing cron schedule to re-spawn or a missed one-shot
            # to mark + alert; see ava.watcher.reconcile. Best-effort: a
            # registry failure must never block boot.
            try:
                from ava.watcher import reconcile as _reconcile_watchers

                for _sentence in _reconcile_watchers():
                    logger.info("watcher reconcile: %s", _sentence)
            except Exception:
                logger.warning("watcher reconcile failed at boot", exc_info=True)
            graph = await _build_graph(checkpointer, config_overlay, agent_id)
            ctx = AvaContext(
                ops_pool=db_pool,
                inbound_listener=inbound_listener,
                llm=llm,
                event_publisher=event_publisher,
            )

            # Block on the MCP daemon's readiness now — just before the graph goes
            # live and an exec node could reach an MCP tool. It was spawned at the
            # top of boot and has been booting in parallel with all the work above,
            # so this usually returns at once; the line below reports the outcome.
            await mcp_daemon.await_ready()
            _boot_timing.mark("mcp_ready")

            # The MCP daemon is a per-machine shared service (watchdog-owned);
            # `_MCPDaemon.started` is always False by design, so the old
            # "started" if ... else "fallback" ternary logged "fallback" on
            # every boot. Report what is actually true: whether the shared
            # socket exists (the SDK connects lazily either way).
            logger.info(
                "ava starting — model={model} pid={pid} mcp_daemon={mcp}",
                model=turn_settings.lm.llm_model,
                pid=os.getpid(),
                mcp="shared" if Path(mcp_daemon.socket_path).exists() else "local-fallback",
            )

            # One ainvoke per turn — the graph self-cycles within a turn and
            # claim ends the invocation at the turn boundary; the runloop
            # re-invokes until claim requests process exit (terminate/restart).
            # Each invocation's input is a minimal state update (see the
            # comment at the ainvoke call site for why it must not be a full
            # AgentState()). The claim node itself long-awaits inbound, so no
            # initial messages need to be stuffed in from outside.
            _boot_timing.mark("graph_build")
            _boot_timing.emit()

            # R1 (Task #1021): the agent's half of its liveness lease — a
            # background renewer running for the whole graph lifetime (idle
            # included), cancelled in the finally below. The lease is what the
            # quiesce/reaper/frontend read as "this process is alive".
            lease_renewer = asyncio.create_task(_renew_agent_lease_loop(db_pool, agent_id))
            # R1 (Task #2257): periodic page-liveness scan — the heartbeat
            # check-in only reaches idle agents, so a busy agent's pages would
            # otherwise go unreconciled for as long as its turn lasts (2026-09-01:
            # a serve() page died at a platform update and stayed dead ~4h). This
            # task scans every heartbeat interval (default 300 s) regardless of
            # idle/busy; a pass is skipped when the heartbeat already scanned the
            # same interval. Boot covered t=0; this covers everything after.
            page_reconciler = asyncio.create_task(
                page_reconcile_loop(db_pool, agent_id, event_publisher=event_publisher)
            )
            try:
                await _invoke_graph_with_lifecycle_logging(graph, agent_id, ctx)
            finally:
                lease_renewer.cancel()
                page_reconciler.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease_renewer
                    await page_reconciler
    finally:
        # process_exit event emitted before closing DB handles — Postgres sink
        # uses its own pool (the event emitter's, shared.telemetry), independent of ours.
        # reason distinguishes normal / signal:SIGHUP / exception:X so silent
        # deaths leave a trace (159 / 160 cases like a session closing
        # ungraceful exit; combined with _install_lifecycle_signal_handlers
        # converting SIGHUP / SIGTERM to SystemExit, finally can reach here).
        # logger.opt(exception=True) when reason='exception:X' also feeds
        # traceback into events.payload (`shared.log._postgres_sink` stuffs automatically).
        reason = _exit_reason()
        logger.opt(exception=reason.startswith("exception:")).info(
            "agent {agent_id} process_exit reason={reason} pid={pid}",
            event="process_exit",
            agent_id=agent_id,
            reason=reason,
            pid=os.getpid(),
        )

        # ── Cleanup associated resources: MCP daemon ──
        # The agent's shells / watchers are deliberately NOT reaped here —
        # they persist across terminate/restart/update so background work
        # (a running task, a watcher that can resurrect this agent) outlives
        # the process. Reclaiming orphans is a separate periodic admin concern.
        # Stop the MCP daemon (which closes Chrome/Playwright etc. browser
        # servers).

        # Stop the SSE event publisher first: a bounded drain flushes any queued
        # events (including a final Error emit from the crash path above), then
        # the worker is cancelled.
        await event_publisher.aclose()
        # Pools close automatically via `async with`; listener owns a single
        # conn outside that lifecycle and must be closed explicitly.
        await inbound_listener.close()
        # Notify the gateway this process has exited; the exit-reason tag rides
        # the process_exit event. process_exit above is complementary: it always
        # writes (including silent death), while agent_terminated fires only when
        # the status really transitions.
        await _route_process_end_notify(agent_id, reason)
        # ── MCP daemon stop ──
        await mcp_daemon.stop()


def run() -> None:
    """Entry point — `python -m agent --agent-id N` calls this.

    The per-agent config overlay and the birth-config each arrive in an env var
    (`$AVA_AGENT_CONFIG_OVERLAY` / `$AVA_AGENT_BIRTH_CONFIG`), not on argv: either
    JSON blob may carry a Settings field as sensitive as a provider api_key (today
    only the overlay actually does; birth_config is framework-fields-only, but a
    future frozen per-agent field could carry one), and argv is world-readable
    through `ps` (issue #974). `ops.agent_launch` puts both in the child env
    dict; this pops them so the agent's own children (shell sessions, watchers)
    do not inherit them.
    """
    from shared.env_registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV
    from shared.migrations import assert_schema_current
    from shared.platform import raise_fd_limit

    raise_fd_limit(65536)  # this process spawns shells/watchers; keep the ceiling raised
    parser = argparse.ArgumentParser(prog="python -m agent")
    parser.add_argument(
        "--agent-id",
        type=int,
        required=True,
        help="Required: the agent_id this process is bound to. "
        "Pre-INSERTed as unclaimed 'idling' row by spawn_agent / resurrect_agent.",
    )
    args = parser.parse_args()

    def _decode_env_json(env_name: str) -> dict[str, object] | None:
        raw = os.environ.pop(env_name, "")
        if not raw:
            return None
        import json as _json

        value = _json.loads(raw)
        if not isinstance(value, dict):
            raise SystemExit(f"{env_name} must be a JSON object, got {type(value).__name__}")
        return value

    overlay = _decode_env_json(AGENT_CONFIG_OVERLAY_ENV)
    birth = _decode_env_json(AGENT_BIRTH_CONFIG_ENV)
    # Retain the popped maps for the exec subprocess: each execute_code child
    # must see the same effective per-agent settings the agent process booted
    # with (agent/_config_carrier.py).
    from agent._config_carrier import store_config_maps

    store_config_maps(overlay, birth)

    # DB schema version out of sync with code blows up early — otherwise
    # subsequent claim_agent_row / checkpointer.setup hitting drift
    # would give a more obscure error than SchemaVersionMismatch.
    assert_schema_current(settings.data_plane.db_url)
    _install_lifecycle_signal_handlers()
    # On Windows the default ProactorEventLoop is incompatible with psycopg's
    # async wait (loop.add_reader / loop.add_writer raise NotImplementedError).
    # Force SelectorEventLoop instead — it supports add_reader/add_writer (for
    # psycopg) and create_subprocess_exec (for the MCP daemon) on Windows.
    import sys as _sys

    if _sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(args.agent_id, config_overlay=overlay, birth_config=birth))
