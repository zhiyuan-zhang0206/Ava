"""Agent-host daemon — the supervised process that runs every local agent's turns.

Phase 1 of `future/infra/agent-runner-as-server.md`, and the piece that makes the
other three real: `dispatcher.py` turns wakes into turn tasks, `host.py` runs a
turn, and this module is the long-running process they live in — pidfile,
healthz, process-scope boot, and the shutdown that drains them.

Every agent-runner starts one instance through its service roster.

Usage:
    .venv/bin/python -m services.agent_host.daemon

## Boot order, and why it is this one

1. **Pidfile**, so a second instance exits instead of racing the first for turns.
2. **Process-scope boot** — `init_process_scope` (trace export; must precede any
   model build so OpenLLMetry can instrument it), `land_cluster_extensions` (the
   cluster's installed skills onto this machine), then `load_process_extensions`
   (the external-plugin load). Exactly once per process: see
   `agent/_process_boot.py:load_process_extensions` for why repeating it is not
   an option, and issue #170 for the behavioural change that follows. The
   materialization is once per process for a milder reason — the skills
   directory belongs to the machine, not to any agent — but it lands here rather
   than per turn because the host is long-lived. Newly installed extensions
   take effect after its normal restart.
3. **The shared data plane** — isolated workload/control pools, checkpointer,
   graph. Before the scheduler exists, the control pool recovers any old
   applied hosted force whose durable exec evidence proves resource-free.
   `build_graph` runs the builtin-plugin load and the state-class build, which
   is the other half of "once per process".
4. **Healthz**, published only after the above, so a green probe means the host
   can actually take a turn.
5. **The dispatcher**, last: subscribing before the host can serve would drop
   wakes on the floor.

The daemon holds no agent identity. `init_gateway_process` leaves the log sink's
process agent unset, so every line is attributed by the turn contextvar the host
binds — one process, correct per-agent log files.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Collection
from typing import cast

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

import shared.redis_client
from agent._turn_progress import turn_progress_age_s, turn_progress_snapshot
from agent.hosted_ownership import settle_stale_running_rows
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.agent_host import boot_defer
from services.agent_host.dispatcher import InboundWakeDispatcher, TurnScheduler
from services.agent_host.host import AgentHost
from services.agent_host.pooled_checkpoint import PooledPostgresSaver
from services.agent_host.pools import build_control_pool, build_shared_pool
from services.agent_host.stdout_log import _rotate_stdout_log_forever
from shared import maintenance, paths, pool_release
from shared.config import settings
from shared.daemon_health import (
    Liveness,
    RouteHandler,
    health_port,
    start_health_server,
    stop_health_server,
)
from shared.daemon_shutdown import cancel_and_drain, install_graceful_shutdown
from shared.daemon_shutdown import hard_exit as _hard_exit
from shared.exec_request_evidence import disposition_hint
from shared.helper_chain_guard import parent_chain_intact
from shared.hosted_force import recover_orphaned_hosted_forces
from shared.log import init_gateway_process, logger
from shared.machine import machine_name
from shared.straggler_reap import settle_stranded_reaps_async
from shared.timing import assert_clock_lattice

_log = logging.getLogger("services.agent_host.daemon")

_MODULE = "services.agent_host.daemon"
_PIDFILE = settings.services.agent_host_pidfile

# A fixed timer proves liveness even when no agent has work.
_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = 15.0
_OWNERSHIP_RENEW_TIMEOUT_S = 10.0
# Gateway key presence proves the 15s host loop runs; four missed beats expire it.
_TURN_PROGRESS_HEARTBEAT_TTL_S = 60
_TURN_PROGRESS_PUBLISH_TIMEOUT_S = 3.0


# Plugin-discovery watchdog (issue #170): the host loads external plugins
# exactly once per process (`load_process_extensions`), so a plugin installed
# after boot is invisible to every agent on this runner until a restart. The
# runner's supervisor (watchdog -> healthcheck) restarts a dead host within a
# minute, so the fix is not a reload (plugin-spec-v2's S4 dispose contract is
# unimplemented — a second load would leak and fork class identity) but an
# intentional, ergonomic restart: watch $AVA_HOME/plugins, and on any change
# exit so the supervisor brings the host back with the new plugin loaded.
_PLUGINS_POLL_INTERVAL_S = 30.0


def _plugins_fingerprint() -> str:
    """A cheap fingerprint of the external-plugin directory.

    One entry per plugin subdirectory: name + plugin.py (size, mtime_ns).
    Changing, adding, or removing a plugin changes the fingerprint; touching
    any other file under the dir does not. The directory itself missing is a
    valid state (no plugins) — the fingerprint is then empty, not an error.
    """
    from shared.runtime_interpreter import external_plugin_read_root

    root = external_plugin_read_root()
    if not root.exists():
        return ""
    parts: list[str] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        plugin_py = sub / "plugin.py"
        if not plugin_py.exists():
            continue
        st = plugin_py.stat()
        parts.append(f"{sub.name}:{st.st_size}:{st.st_mtime_ns}")
    return "|".join(parts)


async def _watch_plugins_for_restart() -> None:
    """Exit the host when the plugin directory changes under it.

    Runs for the daemon's whole life; on a fingerprint change it logs the
    names that changed and raises SIGTERM at itself, which `install_graceful_shutdown`
    turns into the KeyboardInterrupt every daemon already unwinds through —
    the drains run, then the supervisor restarts the host fresh.
    """
    base = _plugins_fingerprint()
    while True:
        await asyncio.sleep(_PLUGINS_POLL_INTERVAL_S)
        now = _plugins_fingerprint()
        if now == base:
            continue
        _log.info(
            "[agent-host] external plugins changed under $AVA_HOME/plugins — "
            "restarting to load them (issue #170)"
        )
        signal.raise_signal(signal.SIGTERM)
        return


async def _publish_turn_progress_heartbeat(
    machine: str,
    active_agents: Collection[int],
) -> None:
    """Best-effort Redis snapshot for the gateway's out-of-process breaker."""
    from shared.hosted_db_wait import database_wait_snapshot

    snapshots = {}
    for agent_id in sorted(active_agents):
        snapshot = turn_progress_snapshot(agent_id)
        if snapshot is not None:
            waiting = database_wait_snapshot(agent_id, last_progress=snapshot["last_marks"][-1])
            snapshots[str(agent_id)] = {
                **snapshot,
                **({"db_wait": waiting} if waiting is not None else {}),
            }
    try:
        async with asyncio.timeout(_TURN_PROGRESS_PUBLISH_TIMEOUT_S):
            await shared.redis_client.get_async_redis().set(
                f"host_turn_progress:{machine}",
                json.dumps(snapshots, separators=(",", ":")),
                ex=_TURN_PROGRESS_HEARTBEAT_TTL_S,
            )
    except TimeoutError:
        _log.warning(
            "[agent-host] turn-progress heartbeat publish exceeded %.1fs",
            _TURN_PROGRESS_PUBLISH_TIMEOUT_S,
        )
    except Exception:
        # Defensive evidence only: a Redis outage must not stall renewal.
        _log.debug("[agent-host] turn-progress heartbeat publish failed", exc_info=True)


def _report_long_admission_waits(host: AgentHost) -> None:
    """One anomaly event per wait episode past the admission alert bound.

    Queueing is the configured memory/runtime trade-off working — not an error.
    A wait past the bound means the queue is backing up: raise the limit (after
    the joint capacity check) or inspect the turns holding slots. The wait
    itself is already exempt from stall cancellation; this event is the signal
    that replaces the (wrong) cancellation.
    """
    threshold = settings.daemon.host_admission_wait_alert_seconds
    for agent_id, waited_s in host.admission.long_waiters(threshold):
        logger.warning(
            "hosted turn for agent {agent_id} has queued at the admission gate "
            "for {waited_s:.0f}s (limit {limit}, {queued} queued) — raise the "
            "limit or inspect the turns holding slots; the turn stays queued",
            event="host_admission_wait_exceeded",
            agent_id=agent_id,
            waited_s=round(waited_s, 1),
            limit=host.admission.limit,
            queued=host.admission.waiting,
        )


async def _beat_forever(
    liveness: Liveness,
    host: AgentHost,
    scheduler: TurnScheduler,
    machine: str,
) -> None:
    """Liveness and ownership renewal, independent of the idle dispatcher.
    beat() precedes DB renewal — process health must not depend on the DB."""
    while True:
        _require_helper_parent_chain()
        liveness.beat()
        # Liveness stays unconditional; database work does not. A quiesced unit
        # is between stop and resume — renewing here would keep agent-row
        # leases alive across the whole window and add DB work the window
        # exists to stop. The leases lapse with their TTL; the first beat after
        # resume refreshes every row this host still owns.
        if not maintenance.quiesced():
            try:
                await asyncio.wait_for(host.renew_ownership(), timeout=_OWNERSHIP_RENEW_TIMEOUT_S)
            except TimeoutError:
                _log.warning("[agent-host] ownership renewal timed out")
            except Exception:
                _log.exception("[agent-host] ownership renewal failed — retrying next beat")
        await _publish_turn_progress_heartbeat(machine, scheduler.active_agents)
        _report_long_admission_waits(host)
        await asyncio.sleep(_LIVENESS_BEAT_STEP_S)


async def _stop_ownership_beat(beat: asyncio.Task[None] | None) -> None:
    if beat is not None:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat


async def _join_background_task(task: asyncio.Task[object]) -> None:
    """Join a cancelled task while retaining any failure that preceded cancel."""
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _close_host_runtime(
    host: AgentHost,
    scheduler: TurnScheduler,
    beat: asyncio.Task[None] | None,
    background: dict[str, asyncio.Task[object]],
) -> None:
    """Drain turns and release settled ownership even if background joins fail."""
    for task in background.values():
        if not task.cancelling():
            task.cancel()
    # A failed task can retain even KeyboardInterrupt. Every cleanup stage must
    # run before that failure propagates; closing the pools first strands
    # ownership and active turns. Callbacks unwind in reverse.
    async with contextlib.AsyncExitStack() as cleanup:
        cleanup.push_async_callback(host.aclose)
        cleanup.push_async_callback(_stop_ownership_beat, beat)
        cleanup.push_async_callback(scheduler.aclose)
        for task in background.values():
            cleanup.push_async_callback(_join_background_task, task)


class _PageEventPublisher:
    """Best-effort page events on the shared Redis channel — the daemon's
    stand-in for a per-agent SSE publisher (turns build their own; none
    exists outside a turn). Mirrors the gateway ttl_reaper's pattern so the
    frontend drops closed rows the daemon's scan closes; pages still heal
    without it, the events only keep the open-pages popover accurate.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[object]] = set()

    def emit(self, payload: str) -> None:
        from shared.config import settings
        from shared.redis_client import publish_best_effort

        # Fire-and-forget: publish_best_effort never raises; the task set
        # keeps a strong ref so the publish cannot be GC'd mid-flight.
        task = asyncio.create_task(
            publish_best_effort(
                settings.data_plane.events_channel, payload, context="agent_host_page"
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def _page_reconcile_forever(pool: AsyncConnectionPool) -> None:
    """Periodically probe + restore every hosted agent's open pages.

    Heartbeat check-ins only reach idle agents. The host therefore restores
    pages for busy agents too: once at startup and then every heartbeat
    interval, skipping pages already reconciled within that interval. A failed
    pass logs and retries on the next interval without blocking other turns.
    """
    from agent.startup import reconcile_all_open_pages
    from shared.config import settings

    interval_s = float(settings.daemon.heartbeat_interval_seconds)
    publisher = _PageEventPublisher()
    while True:
        # A quiesced unit (stop window) skips its pass, silently, until
        # resume: page probing would borrow the pools the stop released.
        if not maintenance.quiesced():
            try:
                await reconcile_all_open_pages(
                    pool, interval_s=interval_s, event_publisher=publisher
                )
            except Exception:
                _log.exception(
                    "[agent-host] periodic page reconcile pass failed — retrying next interval"
                )
        await asyncio.sleep(interval_s)


def _spawn_background_tasks(pool: AsyncConnectionPool) -> dict[str, asyncio.Task[object]]:
    """Create the daemon's background tasks for plugins, pages, event replay and logs.

    Split out of `run()` so the wiring is testable without booting the
    dispatcher: the reconciler's existence is what closes the
    busy-hosted-agent dead-page gap (task #2260), the rotator's is what keeps a
    traceback storm from filling the disk through the uncapped raw transcript
    (task #2356), and a regression that dropped either creation must turn a
    test red rather than silently reopen the gap.
    """
    from services.agent_host.impersonation_events import reconcile_forever

    return {
        "impersonation_events": asyncio.create_task(reconcile_forever()),
        "plugins_watch": asyncio.create_task(_watch_plugins_for_restart()),
        "page_reconciler": asyncio.create_task(_page_reconcile_forever(pool)),
        "stdout_log_rotate": asyncio.create_task(_rotate_stdout_log_forever()),
    }


async def _build_checkpointer(
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
) -> AsyncPostgresSaver:
    """One saver for the whole host, over the workload pool.

    No `setup()` call: the runner role holds no CREATE on the schema by design
    (task #1236), and the gateway owns langgraph's own migrations. A host booting
    against a schema that lacks the checkpoint tables fails on first use, loudly,
    which is the correct outcome for a runner that should never have been
    pointed at an unmigrated database.
    """
    from agent.startup import (
        _wrap_saver_writes_with_loud_failure,
        _wrap_saver_writes_with_nstep_interval,
    )
    from agent.state import build_checkpoint_serde
    from shared.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
    from shared.config.turn_view import turn_settings

    saver_pool = cast(AsyncConnectionPool[psycopg.AsyncConnection[DictRow]], pool)
    checkpointer = PooledPostgresSaver(conn=saver_pool, serde=build_checkpoint_serde())
    _wrap_saver_writes_with_loud_failure(checkpointer)
    _wrap_saver_writes_with_nstep_interval(
        checkpointer,
        lambda: turn_settings.agent.checkpoint_interval,
    )
    # Transition layer (tasks #3180/#3181): vanilla-era readers must see
    # delta-written threads' messages. Inert on vanilla-written data.
    wrap_saver_reads_with_delta_reconstruction(checkpointer)
    return checkpointer


async def _recover_hosted_forces_at_boot(
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection], machine: str
) -> None:
    """Recover only resource-free predecessor forces before scheduling starts."""
    recovered, deferred = await recover_orphaned_hosted_forces(control_pool, machine)
    logger.info("hosted boot recovery: observed {n} orphaned force(s)", n=len(recovered))
    streaks = boot_defer.record_deferrals(deferred)
    for agent_id, evidence in deferred.items():
        streak = streaks[agent_id]
        if streak >= boot_defer.ALERT_AFTER_BOOTS:
            logger.warning(
                "hosted boot recovery deferred for agent {agent_id} on {streak} consecutive "
                "boots: retained exec request evidence [{evidence}] is not clearing on its "
                "own. {hint}",
                event="hosted_boot_recovery_stalled",
                agent_id=agent_id,
                streak=streak,
                evidence="; ".join(entry.describe() for entry in evidence),
                hint=disposition_hint(agent_id),
            )
        else:
            logger.warning(
                "hosted boot recovery deferred for agent {agent_id} (boot {streak} of "
                "{limit}): retained exec request evidence [{evidence}]. {hint}",
                agent_id=agent_id,
                streak=streak,
                limit=boot_defer.ALERT_AFTER_BOOTS,
                evidence="; ".join(entry.describe() for entry in evidence),
                hint=disposition_hint(agent_id),
            )


async def _schedule_watcher_recovery(host: AgentHost) -> None:
    """Once per host boot, arm watcher owners for the paced pending scan."""
    await host.watcher_boot_wakes()


async def _open_host_pools(
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    machine: str,
) -> None:
    """Open both client pools, then recover before the scheduler can run."""
    await workload_pool.open()
    await control_pool.open()
    await _recover_hosted_forces_at_boot(control_pool, machine)


async def _close_host_pools(
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
) -> None:
    """Close both pools even if the control-pool close itself fails."""
    try:
        await control_pool.close()
    finally:
        await workload_pool.close()


def _is_running() -> bool:
    """Whether a host is already running. Pid-reuse-safe: a live pid whose argv
    does not name this module is a recycled pid, not an instance."""
    return pidfile_holds_daemon(_PIDFILE, _MODULE)


async def run() -> None:
    """Boot the host and serve wakes until cancelled. See the module docstring
    for why the order is what it is."""
    assert_clock_lattice()
    if _is_running():
        _log.info("[agent-host] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)
    if not acquire_pidfile(_PIDFILE, _MODULE):
        _log.info("[agent-host] could not acquire pidfile %s, exiting", _PIDFILE)
        sys.exit(1)

    from agent._process_boot import (
        init_process_scope,
        land_cluster_extensions,
        load_process_extensions,
    )

    # langgraph types its checkpointer parameter with an unparameterized generic,
    # so the imported symbol reads as partially unknown; the return type — the
    # only part this module uses — is fully known.
    from agent.graph import build_graph  # pyright: ignore[reportUnknownVariableType]

    init_process_scope()
    land_cluster_extensions()
    load_process_extensions()

    workload_pool, control_pool = (
        build_shared_pool(settings.data_plane.db_url),
        build_control_pool(settings.data_plane.db_url),
    )
    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    beat: asyncio.Task[None] | None = None
    health = None
    try:
        local_machine = machine_name()
        await _open_host_pools(workload_pool, control_pool, local_machine)
        checkpointer = await _build_checkpointer(workload_pool)
        # build_graph runs the builtin-plugin load and builds the dynamic state
        # class — process-global, and the reason there is ONE graph here rather
        # than one per agent (services/agent_host/host.py explains the cost).
        graph = build_graph(checkpointer)
        host = AgentHost(
            pool=workload_pool,
            control_pool=control_pool,
            checkpointer=checkpointer,
            graph=graph,
            machine=local_machine,
        )
        # The clock reader is injected, not imported by the scheduler: it owns no
        # pool, and this keeps the uncancellable-turn report able to say how long
        # a stuck agent has really been silent.
        scheduler = TurnScheduler(host.run_turn, activity_clock=host.last_active_at)
        beat = asyncio.create_task(_beat_forever(liveness, host, scheduler, local_machine))
        # Straggler-reap marks settle before anything else may look at the row
        # (task #4016): a predecessor boot reaped mid-wave left rows unrunnable;
        # their first-admission wakes are injected after the scheduler exists.
        settled_reaps = await settle_stranded_reaps_async(control_pool, local_machine)
        settled = await settle_stale_running_rows(control_pool, local_machine)
        logger.info("hosted boot settle: settled {n} stale running row(s)", n=len(settled))

        health = await start_health_server(
            "agent_host",
            liveness=liveness,
            extra_routes={
                ("GET", "/stats"): _stats_route(host, scheduler),
                ("POST", "/cancel-turn"): _cancel_turn_route(scheduler, host),
                ("POST", "/release-db-pools"): _release_pools_route(workload_pool, control_pool),
            },
        )
        logger.info(
            "hosted agent-runner started on :{port} "
            "(max concurrent turns {bound}, database pools {workload}/{control})",
            event="host_started",
            port=health_port("agent_host"),
            bound=settings.daemon.host_max_concurrent_turns or "unlimited",
            workload=workload_pool.max_size,
            control=control_pool.max_size,
        )
        # Task #2260: heartbeat-independent page-liveness scan for hosted
        # agents — busy agents get no heartbeats, and the hosted daemon runs
        # no per-agent page_reconcile_loop (loop.py:main() is process-only).
        background = _spawn_background_tasks(workload_pool)
        try:
            await _schedule_watcher_recovery(host)
            # Settled reap rows need one admission each: the cold build's
            # reconcile re-delivers the claimed ordinary work the reap cut
            # short, and the dangling-tool repair closes the truncated turn.
            host.arm_settled_reaps(settled_reaps)
            await InboundWakeDispatcher(
                settings.data_plane.redis_url,
                scheduler,
                pending_scan=host.pending_inbound_wakes,
                stale_after_s=float(settings.daemon.wedged_agent_inbound_age_seconds),
                recovery_wake_batch=settings.daemon.host_recovery_wake_batch,
                scan_interval_s=float(settings.agent.db_notify_wait_timeout_seconds),
                subscription_read_timeout_s=float(settings.agent.db_notify_wait_timeout_seconds),
            ).run()
        finally:
            try:
                await _close_host_runtime(host, scheduler, beat, background)
            finally:
                beat = None  # Runtime cleanup attempted its join even when another stage failed.
    finally:
        await _stop_ownership_beat(beat)
        if health is not None:
            await stop_health_server(health)
        await _close_host_pools(workload_pool, control_pool)
        remove_pidfile(_PIDFILE)
        _log.info("[agent-host] daemon stopped")


def _require_helper_parent_chain() -> None:
    """Exit a helper-spawned host whose ancestor chain no longer holds the helper."""
    if parent_chain_intact():
        return
    _log.warning(
        "[agent-host] permissions helper parent chain broken, self-terminating for helper respawn"
    )
    os._exit(70)


def _cancel_turn_route(scheduler: TurnScheduler, host: AgentHost):  # noqa: ANN202
    """A `POST /cancel-turn` handler — the hosted force-terminate / wedged
    recovery primitive.

    Body: `{"agent_id": <int>, "command_id": <int>}`. Cancels the captured task with the
    bounded unwind (a C-call-blocked turn is reported, not awaited forever) and
    answers `{"cancelled": true|false}` — false means no task was running,
    which the ops caller treats as "nothing to accelerate", never as an error.

    Loopback-only and unauthenticated, like `/healthz`: anything that can dial
    the host's localhost health port already owns the box. The durable
    terminate/restart inbound is always the correctness mechanism — this
    endpoint only accelerates a turn stuck inside a long await.
    """

    async def handler(body: bytes) -> tuple[int, bytes, str]:
        import json

        try:
            payload = json.loads(body or b"{}")
            agent_id, command_id = payload["agent_id"], payload["command_id"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return (
                400,
                json.dumps({"error": "positive agent_id and command_id required"}).encode(),
                "application/json",
            )
        if (
            type(agent_id) is not int
            or type(command_id) is not int
            or agent_id <= 0
            or command_id <= 0
        ):
            return 400, b'{"error":"positive integer identifiers required"}', "application/json"
        cancelled = await scheduler.cancel_exact_force(agent_id, command_id, host.accepts_force)
        return 200, json.dumps({"cancelled": cancelled}).encode(), "application/json"

    return handler


def _release_pools_route(
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
) -> RouteHandler:
    """A `POST /release-db-pools` handler — the pre-stop pool release.

    Called by the ops stop path once this unit's agents are drained: closes
    every idle connection in both pools and answers `{"released": {"workload":
    n, "control": m}}`. Nothing reconnects during the quiesced window (the
    beat and page loops are gated; the turn scan only through the stop leg)
    and the first borrow after resume opens a fresh connection lazily.
    Loopback-only and unauthenticated, like `/cancel-turn`.
    """
    import json

    async def handler(_body: bytes) -> tuple[int, bytes, str]:
        released = {
            "workload": await pool_release.release_idle_async(workload_pool),
            "control": await pool_release.release_idle_async(control_pool),
        }
        logger.info(
            "[agent-host] released idle db-pool connections on request: {released}",
            released=released,
        )
        return 200, json.dumps({"released": released}).encode(), "application/json"

    return handler


def _stats_route(host: AgentHost, scheduler: TurnScheduler):  # noqa: ANN202 — RouteHandler, declared in shared.daemon_health
    """Expose cache/activity counters and this running boot's maintenance identity."""
    import json

    async def handler(_body: bytes) -> tuple[int, bytes, str]:
        # Per-agent turn-progress age: the health signal that separates
        # "the host process is alive" from "this turn is alive". A busy agent
        # (progress every couple of minutes) reads small; an agent whose
        # invocation has been silent for the wedged budget reads large — the
        # turn-level fake-alive state a heartbeat probe alone cannot see.
        active_progress: dict[int, float] = {}
        for agent_id in sorted(scheduler.active_agents):
            age = turn_progress_age_s(agent_id)
            if age is not None:
                active_progress[agent_id] = round(age, 1)
        payload = {
            **host.stats.as_payload(),
            **host.admission.payload(),
            "maintenance_protocol": 1,
            "runtime_owner": str(host._owner),
            "home": str(paths.ava_home()),
            "pid": os.getpid(),
            "active_agents": sorted(scheduler.active_agents),
            "active_progress": active_progress,
        }
        return 200, json.dumps(payload).encode(), "application/json"

    return handler


def main() -> None:
    """Entry point: schema gate, logging, graceful shutdown, then the loop."""
    from shared.config import ensure_eager
    from shared.migrations import assert_schema_current

    # Task #3621: the agent host is on the full-validation whitelist — build
    # the eager config chain before anything else reads config.
    ensure_eager()
    _require_helper_parent_chain()
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="agent_host")
    install_graceful_shutdown("agent_host")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — the to_thread recorders among them — and a stop signal must
    # never wait on those (see `_hard_exit`). The runner is therefore never
    # closed: after the explicit drain below, teardown is skipped by the hard
    # exit.
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[agent-host] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: `run`'s finally still stops the health server,
        # drains turns, releases ownership, closes the pools and removes the
        # pidfile. The executor is deliberately NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[agent-host] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[agent-host] fatal error, shutting down")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main()
