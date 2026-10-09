"""Agent-host daemon — the supervised process that runs every local agent's turns.

Phase 1 of `future/infra/lifecycle/agent-runner-as-server.md`, and the piece that makes the
other three real: `dispatcher.py` turns wakes into turn tasks, `host.py` runs a
turn, and this module is the long-running process they live in — pidfile,
healthz, process-scope boot, and the shutdown that drains them.

Every agent-runner starts one instance through its service roster.

Usage:
    .venv/bin/python -m services.agent_runner.agent_host.daemon

## Boot order, and why it is this one

1. **Pidfile**, so a second instance exits instead of racing the first for turns.
2. **Process-scope boot** — `init_process_scope` (trace export; must precede any
   model build so OpenLLMetry can instrument it), `land_cluster_extensions` (the
   cluster's installed skills onto this machine). The plugin load
   (`agent.extensions.load_extensions`, step 3) happens exactly once per process:
   repeating it is not an option, see issue #170 for the behavioural change that
   follows. The
   materialization is once per process for a milder reason — the skills
   directory belongs to the machine, not to any agent — but it lands here rather
   than per turn because the host is long-lived. Newly installed extensions
   take effect after its normal restart.
3. **The shared data plane** — isolated workload/control pools, checkpointer,
   graph. Before the scheduler exists, the control pool recovers any old
   applied hosted force whose durable exec evidence proves resource-free.
   The builtin-plugin load and the plugin registry built from it come first and
   feed the checkpointer, the graph and the host: the other half of "once per
   process".
4. **Healthz**, published only after the above, so a green probe means the host
   can actually take a turn.
5. **The dispatcher**, last: subscribing before the host can serve would drop
   wakes on the floor.

The daemon holds no agent identity. `init_gateway_process` leaves the log sink's
process agent unset. Ordinary logs belong to the process; agent-owned events
carry explicit attribution. The SDK process entry records shared-host startup
posture before boot work so inherited child identity cannot bind here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Collection, Coroutine, Iterable
from pathlib import Path
from typing import cast

# The executable entry fixes SDK posture before agent/plugin modules can inspect
# inherited environment identity. Importing the module as a library does not boot it.
if __name__ == "__main__":
    from base.native_process.child_env import inherited_process_env

    if "AVA_AGENT_ID" in inherited_process_env():
        raise RuntimeError("a shared agent host cannot inherit a launched-agent identity")
    import ava

    ava.bind_host_process()

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from agent.llm import execute_code
from agent.ownership.hosted import settle_stale_running_rows
from ava.sdk_surface.install import Installation
from ava.sdk_surface.process_context import process_clients
from base import paths
from base.agents.history.hierarchy.chunk_consumer import understanding_loop_forever
from base.agents.impersonation.terminal_notices import run_notice_delivery
from base.agents.incarnation.exec_request_evidence import disposition_hint
from base.agents.incarnation.hosted_force import recover_orphaned_hosted_forces
from base.agents.observation.db_wait import DatabaseWaits
from base.agents.observation.turn_progress import TurnProgress
from base.cluster.machine import machine_name
from base.config import settings
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import (
    Liveness,
    start_health_server,
    stop_health_server,
)
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.deploy.maintenance import admission
from base.deploy.progress_timeout import AGENT_LEASE_RENEW_INTERVAL_S
from base.deploy.timing import assert_clock_lattice
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.log import init_gateway_process, logger
from base.sessions.helper_chain_guard import parent_chain_intact

from ...pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from .dispatcher import InboundWakeDispatcher, TurnScheduler
from .force_termination import kill_terminating_agent_shells
from .host import AgentHost
from .pooled_checkpoint import PooledPostgresSaver
from .pools import build_control_pool, build_shared_pool
from .stdout_log import _rotate_stdout_log_forever

_log = logging.getLogger("services.agent_runner.agent_host.daemon")

_MODULE = "services.agent_runner.agent_host.daemon"


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("agent_host")


def _pidfile() -> Path:
    return _endpoint().pidfile


# A fixed timer proves liveness even when no agent has work. The same beat
# renews hosted agent leases, so its step IS the lattice's renewal interval.
_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = AGENT_LEASE_RENEW_INTERVAL_S
_OWNERSHIP_RENEW_TIMEOUT_S = 10.0
# Gateway key presence proves the 15s host loop runs; four missed beats expire it.
_TURN_PROGRESS_HEARTBEAT_TTL_S = 60
_TURN_PROGRESS_PUBLISH_TIMEOUT_S = 3.0


# Plugin-discovery watchdog (issue #170): the host loads external plugins
# exactly once per process (`agent.extensions.load_extensions`), so a plugin installed
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
    from base.deploy.release.runtime_interpreter import external_plugin_read_root

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
    baseline = _plugins_fingerprint()
    # quiesce-exempt: watches plugin file fingerprints; no database
    while True:
        await asyncio.sleep(_PLUGINS_POLL_INTERVAL_S)
        now = _plugins_fingerprint()
        if now == baseline:
            continue
        _log.info(
            "[agent-host] external plugins changed under $AVA_HOME/plugins — "
            "restarting to load them (issue #170)"
        )
        signal.raise_signal(signal.SIGTERM)
        return


async def _publish_turn_progress_heartbeat(
    bus: EventBus,
    machine: str,
    active_agents: Collection[int],
    database_waits: DatabaseWaits,
    turn_progress: TurnProgress,
) -> bool:
    """Best-effort Redis snapshot for the gateway's out-of-process breaker.

    Returns whether the publish landed; the caller logs the failing/recovered
    transitions once instead of once per beat.
    """
    snapshots = {}
    for agent_id in sorted(active_agents):
        snapshot = turn_progress.snapshot(agent_id)
        if snapshot is not None:
            waiting = database_waits.snapshot(agent_id, last_progress=snapshot["last_marks"][-1])
            snapshots[str(agent_id)] = {
                **snapshot,
                **({"db_wait": waiting} if waiting is not None else {}),
            }
    try:
        async with asyncio.timeout(_TURN_PROGRESS_PUBLISH_TIMEOUT_S):
            await bus.async_redis().set(
                f"host_turn_progress:{machine}",
                json.dumps(snapshots, separators=(",", ":")),
                ex=_TURN_PROGRESS_HEARTBEAT_TTL_S,
            )
    except TimeoutError:
        _log.warning(
            "[agent-host] turn-progress heartbeat publish exceeded %.1fs",
            _TURN_PROGRESS_PUBLISH_TIMEOUT_S,
        )
        return False
    except (RedisConnectionError, RedisTimeoutError, NoPermissionError, OSError):
        # A known Redis outage must not stall renewal; defects reach the owner.
        _log.warning(
            "[agent-host] turn-progress heartbeat publish failed — the gateway's "
            "breaker sees stale progress until it recovers",
            exc_info=True,
        )
        return False
    return True


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
    bus: EventBus,
) -> None:
    """Liveness and ownership renewal, independent of the idle dispatcher.
    beat() precedes DB renewal — process health must not depend on the DB."""
    heartbeat_ok = True
    while True:
        _require_helper_parent_chain()
        liveness.beat()
        # Liveness stays unconditional; database work does not. A quiesced unit
        # is between stop and resume — renewing here would keep agent-row
        # leases alive across the whole window and add DB work the window
        # exists to stop. The leases lapse with their TTL; the first beat after
        # resume refreshes every row this host still owns.
        if not admission.quiesced():
            try:
                await asyncio.wait_for(host.renew_ownership(), timeout=_OWNERSHIP_RENEW_TIMEOUT_S)
            except TimeoutError:
                _log.warning("[agent-host] ownership renewal timed out")
            except (psycopg.OperationalError, PoolTimeout):
                _log.exception("[agent-host] ownership renewal failed — retrying next beat")
        published = await _publish_turn_progress_heartbeat(
            bus, machine, scheduler.active_agents, host.database_waits, host.turn_progress
        )
        if published and not heartbeat_ok:
            _log.warning("[agent-host] turn-progress heartbeat publish recovered")
        heartbeat_ok = published
        _report_long_admission_waits(host)
        await asyncio.sleep(_LIVENESS_BEAT_STEP_S)


async def _ownership_beat_completion(
    liveness: Liveness, host: AgentHost, scheduler: TurnScheduler, machine: str, bus: EventBus
) -> Exception | None:
    """Retain a failed heartbeat for the service's stop/join boundary.

    A heartbeat failure must not cancel boot settlement, dispatch or turn drain.
    Its owning task retains the original exception; shutdown raises it after join.
    """
    try:
        await _beat_forever(liveness, host, scheduler, machine, bus)
    except Exception as error:
        return error
    return None


async def _start_ownership_beat(
    liveness: Liveness, host: AgentHost, scheduler: TurnScheduler, machine: str, bus: EventBus
) -> tuple[asyncio.TaskGroup, asyncio.Task[Exception | None]]:
    """Enter the service-owned heartbeat group before boot settlement starts."""
    tasks = asyncio.TaskGroup()
    await tasks.__aenter__()
    beat = tasks.create_task(
        _ownership_beat_completion(liveness, host, scheduler, machine, bus),
        name="host-ownership-heartbeat",
    )
    return tasks, beat


async def _stop_ownership_beat(beat: asyncio.Task[Exception | None] | None) -> None:
    if beat is not None:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            error = await beat
            if error is not None:
                raise error


async def _close_ownership_beat(
    beat: asyncio.Task[Exception | None] | None, tasks: asyncio.TaskGroup | None
) -> None:
    """Join the heartbeat and close its group without wrapping its retained error."""
    try:
        await _stop_ownership_beat(beat)
    finally:
        if tasks is not None:
            await tasks.__aexit__(None, None, None)


async def _close_host_runtime(
    host: AgentHost,
    scheduler: TurnScheduler,
    beat: asyncio.Task[Exception | None] | None,
    beat_tasks: asyncio.TaskGroup,
) -> None:
    """Drain turns and release settled ownership even if a stage fails.

    The background loops are already joined: their `TaskGroup` in `run` exits
    before this runs. Every cleanup stage must run before a failure propagates;
    closing the pools first strands ownership and active turns. Callbacks unwind
    in reverse.
    """
    async with contextlib.AsyncExitStack() as cleanup:
        cleanup.push_async_callback(host.aclose)
        cleanup.push_async_callback(_close_ownership_beat, beat, beat_tasks)
        cleanup.push_async_callback(scheduler.aclose)


async def _exec_memory_guard_forever() -> None:
    """Relieve critical memory pressure by killing the largest exec domain.

    Where the OS reports no pressure state (Linux), the guard does not run.
    """
    from base.host.memory_pressure import host_memory_source

    from .exec_memory_guard import (
        ExecMemoryGuard,
        find_exec_domains,
    )

    source = host_memory_source()
    if source is None:
        _log.info("[agent-host] exec memory guard idle — this OS reports no memory pressure state")
        return
    host_pid = os.getpid()
    await ExecMemoryGuard(source, domains=lambda: find_exec_domains(host_pid, source)).run_forever()


def _background_loops(
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    db: Database,
    *,
    catalog: ModelCatalog,
) -> dict[str, Coroutine[object, object, None]]:
    """The daemon's background loops for plugins, logs, exec memory and understanding chunks.

    Split out of `run()` so the wiring is testable without booting the
    dispatcher: the rotator's existence is what keeps a traceback storm from
    filling the disk through the uncapped raw transcript (task #2356), and a
    regression that dropped it must turn a test red rather than silently reopen
    the gap. `run` starts them in one `TaskGroup`.
    """
    return {
        "plugins_watch": _watch_plugins_for_restart(),
        "stdout_log_rotate": _rotate_stdout_log_forever(),
        "exec_memory_guard": _exec_memory_guard_forever(),
        "understanding_chunks": understanding_loop_forever(
            control_pool, db, [execute_code], catalog=catalog, llm_override=settings.lm.llm_override
        ),
    }


def _load_plugin_installation() -> Installation:
    """Load one installation: its registry feeds the graph and its configs feed each turn.

    The shared graph is built once and cannot take a new registry. The host
    retains this installation's boot image until its normal process restart.
    """
    from agent.extensions import load_extensions
    from ava.sdk_surface.install import installed
    from base.config import Settings
    from base.config.service_read import ConfigAuthority
    from base.lm.plugin_providers import build_model_catalog

    env_path = paths.ava_home() / ".env"
    if settings.profile is None:
        authority = ConfigAuthority(runtime=settings, all_domains=settings, env_path=env_path)
    else:
        authority = ConfigAuthority.deferred(
            runtime=settings, build_all_domains=lambda: Settings(profile=None), env_path=env_path
        )
    load_extensions(catalog=build_model_catalog(), authority=authority)
    installation = installed()
    if installation is None:
        raise RuntimeError("the plugin load did not install its SDK surface")
    return installation


async def _build_checkpointer(
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
    plugin_state_classes: Iterable[type[BaseModel]] = (),
) -> AsyncPostgresSaver:
    """One saver for the whole host, over the workload pool.

    `plugin_state_classes` are the BaseModels the loaded plugins declare as state: the checkpoint
    serde must be allowed to decode them.

    No `setup()` call: the runner role holds no CREATE on the schema by design
    (task #1236), and the gateway owns langgraph's own migrations. A host booting
    against a schema that lacks the checkpoint tables fails on first use, loudly,
    which is the correct outcome for a runner that should never have been
    pointed at an unmigrated database.
    """
    from agent.startup import (
        wrap_saver_writes_with_loud_failure,
        wrap_saver_writes_with_nstep_interval,
    )
    from agent.state import build_checkpoint_serde
    from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction

    saver_pool = cast(AsyncConnectionPool[psycopg.AsyncConnection[DictRow]], pool)
    checkpointer = PooledPostgresSaver(
        conn=saver_pool, serde=build_checkpoint_serde(plugin_state_classes)
    )
    wrap_saver_writes_with_loud_failure(checkpointer)
    wrap_saver_writes_with_nstep_interval(checkpointer, lambda: settings.agent.checkpoint_interval)
    # Transition layer (tasks #3180/#3181): vanilla-era readers must see
    # delta-written threads' messages. Inert on vanilla-written data.
    wrap_saver_reads_with_delta_reconstruction(checkpointer)
    return checkpointer


async def _recover_hosted_forces_at_boot(
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection], machine: str
) -> None:
    """Recover only resource-free predecessor forces before scheduling starts."""
    recovered, deferred = await recover_orphaned_hosted_forces(
        control_pool, machine, kill_shell_sessions=kill_terminating_agent_shells
    )
    logger.info("hosted boot recovery: observed {n} orphaned force(s)", n=len(recovered))
    for agent_id, evidence in deferred.items():
        logger.warning(
            "hosted boot recovery deferred for agent {agent_id}: retained exec request "
            "evidence [{evidence}] is not clearing on its own. {hint}",
            event="hosted_boot_recovery_deferred",
            agent_id=agent_id,
            evidence="; ".join(entry.describe() for entry in evidence),
            hint=disposition_hint(agent_id),
        )


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
    return pidfile_holds_daemon(_pidfile(), _MODULE)


def _boot_handles() -> tuple[
    AsyncConnectionPool[psycopg.AsyncConnection],
    AsyncConnectionPool[psycopg.AsyncConnection],
    EventBus,
    Database,
]:
    """The turn/checkpoint pool, the reserved control pool, the event bus and the database of one host."""
    db = Database.from_settings()
    return build_shared_pool(db), build_control_pool(db), EventBus.from_settings(), db


async def _dispatch_host(
    host: AgentHost,
    scheduler: TurnScheduler,
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    db: Database,
    bus: EventBus,
    local_machine: str,
    *,
    catalog: ModelCatalog,
) -> None:
    """Run dispatcher siblings, joined before the owned heartbeat and turn drain."""
    async with asyncio.TaskGroup() as background:
        background.create_task(
            run_notice_delivery(db, local_machine), name="impersonation_terminal_notices"
        )
        for name, loop in _background_loops(control_pool, db, catalog=catalog).items():
            background.create_task(loop, name=name)
        await InboundWakeDispatcher(
            bus,
            scheduler,
            pending_scan=host.pending_inbound_wakes,
            database_waits=host.database_waits,
            turn_progress=host.turn_progress,
            turn_admission=host.admission,
            stale_after_s=float(settings.daemon.wedged_agent_inbound_age_seconds),
            recovery_wake_batch=settings.daemon.host_recovery_wake_batch,
            recovery_wake_inflight=settings.daemon.host_recovery_wake_inflight,
            scan_interval_s=float(settings.agent.db_notify_wait_timeout_seconds),
            subscription_read_timeout_s=float(settings.agent.db_notify_wait_timeout_seconds),
        ).run()
        # The dispatcher runs until cancelled; a return would leave the
        # group waiting on loops that never end, hanging the stop.
        raise RuntimeError("wake dispatcher exited without cancellation")


async def run() -> None:
    """Boot the host and serve wakes until cancelled. See the module docstring
    for why the order is what it is."""
    assert_clock_lattice()
    if _is_running():
        _log.info("[agent-host] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)
    if not acquire_pidfile(_pidfile(), _MODULE):
        _log.info("[agent-host] could not acquire pidfile %s, exiting", _pidfile())
        sys.exit(1)

    # langgraph types its checkpointer parameter with an unparameterized generic,
    # so the imported symbol reads as partially unknown; the return type — the
    # only part this module uses — is fully known.
    from agent.graph import build_graph  # pyright: ignore[reportUnknownVariableType]
    from agent.process_boot import (
        init_process_scope,
        land_cluster_extensions,
    )

    workload_pool, control_pool, bus, db = _boot_handles()
    init_process_scope()
    land_cluster_extensions(db)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    beat: asyncio.Task[Exception | None] | None = None
    beat_tasks: asyncio.TaskGroup | None = None
    health = None
    try:
        local_machine = machine_name()
        await _open_host_pools(workload_pool, control_pool, local_machine)
        installation = _load_plugin_installation()
        checkpointer = await _build_checkpointer(
            workload_pool, [cls for _plugin, cls in installation.registry.state_classes()]
        )
        # The dynamic state class the graph builds is process-global, and the reason there is
        # ONE graph here rather than one per agent (services/agent_runner/agent_host/host.py explains the cost).
        host = AgentHost(
            pool=workload_pool,
            control_pool=control_pool,
            checkpointer=checkpointer,
            graph=build_graph(checkpointer, installation.registry),
            machine=local_machine,
            bus=bus,
            db=db,
            catalog=installation.require_catalog(),
            clients=process_clients(database=lambda: db),
            extensions=installation.registry,
            plugin_configs=installation.configs,
        )
        # The clock reader is injected, not imported by the scheduler: it owns no
        # pool, and this keeps the uncancellable-turn report able to say how long
        # a stuck agent has really been silent.
        scheduler = TurnScheduler(
            host.run_turn,
            activity_clock=host.last_active_at,
            config_fingerprint=host.turn_fingerprints.get,
        )
        # The service owns the group across boot settle and turn drain. The
        # heartbeat's retained completion is raised only at the existing join.
        beat_tasks, beat = await _start_ownership_beat(
            liveness, host, scheduler, local_machine, bus
        )
        settled = await settle_stale_running_rows(control_pool, local_machine)
        logger.info("hosted boot settle: settled {n} stale running row(s)", n=len(settled))

        endpoint = _endpoint()
        health = await start_health_server(
            "agent_host",
            endpoint.health_port,
            liveness=liveness,
            extra_routes={
                ("GET", "/stats"): _stats_route(host, scheduler),
                ("POST", "/cancel-turn"): _cancel_turn_route(scheduler, host),
            },
        )
        logger.info(
            "hosted agent-runner started on :{port} "
            "(max concurrent turns {bound}, database pools {workload}/{control})",
            event="host_started",
            port=endpoint.health_port,
            bound=settings.daemon.host_max_concurrent_turns or "unlimited",
            workload=workload_pool.max_size,
            control=control_pool.max_size,
        )
        # One TaskGroup owns the loops beside the dispatcher: a loop that
        # raises cancels the dispatcher and its siblings, and the exception
        # leaves `run` so the process exits for `ava-root` to restart it. The
        # group exits, every loop joined, before the runtime drains turns.
        try:
            await _dispatch_host(
                host, scheduler, control_pool, db, bus, local_machine,
                catalog=installation.require_catalog(),
            )
        finally:
            try:
                await _close_host_runtime(host, scheduler, beat, beat_tasks)
            finally:
                beat = None  # Runtime cleanup attempted its join even when another stage failed.
                beat_tasks = None
    finally:
        # A retained heartbeat failure during boot must still close pools and
        # remove the pidfile, just as one raised after dispatch/drain does.
        async with contextlib.AsyncExitStack() as cleanup:
            cleanup.callback(remove_pidfile, _pidfile())
            cleanup.push_async_callback(_close_host_pools, workload_pool, control_pool)
            if health is not None:
                cleanup.push_async_callback(stop_health_server, health)
            cleanup.push_async_callback(_close_ownership_beat, beat, beat_tasks)
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


def _stats_route(host: AgentHost, scheduler: TurnScheduler):  # noqa: ANN202 — RouteHandler, declared in base.daemon.health
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
            age = host.turn_progress.age_s(agent_id)
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
    """Entry point: SDK posture, schema gate, logging, graceful shutdown, then the loop."""
    import ava

    ava.bind_host_process()
    from base.config import ensure_eager
    from base.deploy.schema.migrations import assert_schema_current

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
