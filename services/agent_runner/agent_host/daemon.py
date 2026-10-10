"""Agent-host daemon — the supervised process that runs every local agent's turns.

The service roster starts this process to own wake dispatch, agent turns,
health, process boot and their shutdown.

Usage:
    .venv/bin/python -m services.agent_runner.agent_host.daemon

## Boot order, and why it is this one

1. **Pidfile**, so a second instance cannot race the first for turns.
2. **Process-scope boot** — trace export precedes models and skills materialize
   once. Plugins load once (issue #170); new extensions require a host restart.
3. **The shared data plane** — workload/control pools, checkpointer and graph.
   Recover predecessor forces only with durable resource-free exec evidence.
4. **Healthz**, after boot, so a green probe means the host can take a turn.
5. **The dispatcher**, after serving is ready, so subscribed wakes are not lost.

Shutdown joins dispatcher siblings and drains turns before collecting the
installation's sampling refresh. Health and ownership callbacks still unwind;
joined host pools close even if sampling reports an original failure.
The host has no agent identity: process logs remain unattributed, agent events
carry explicit identity, and the SDK entry rejects inherited child identity
before boot work.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Callable, Collection, Coroutine, Iterable
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
from base.agents.context.clients import ClientSet
from base.agents.history.hierarchy.chunk_consumer import understanding_loop_forever
from base.agents.impersonation.terminal_notices import run_notice_delivery
from base.agents.incarnation.exec_request_evidence import disposition_hint
from base.agents.incarnation.hosted_force import recover_orphaned_hosted_forces
from base.agents.observation.db_wait import DatabaseWaits
from base.agents.observation.turn_progress import TurnProgress
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import (
    Liveness,
    start_health_server,
    stop_health_server,
)
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.deploy.maintenance import admission
from base.deploy.progress_timeout import AGENT_LEASE_RENEW_INTERVAL_S
from base.deploy.stop_timing import CANCEL_UNWIND_TIMEOUT_S
from base.deploy.timing import assert_clock_lattice
from base.events.live.bus import EventBus, EventBusConfig
from base.lm.catalog import ModelCatalog
from base.log import logger
from base.native_process.code_version import CodeVersion
from base.sessions.helper_chain_guard import parent_chain_intact

from ...pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from .dispatcher import InboundWakeDispatcher, TurnScheduler
from .exec_memory_guard import run_memory_guard_forever
from .force_termination import kill_terminating_agent_shells
from .host import AgentHost
from .lifecycle.configuration import (
    host_machine,
    host_policy,
    initialize_logging,
    load_installation,
    understanding_inputs,
)
from .pooled_checkpoint import PooledPostgresSaver
from .pools import build_control_pool, build_shared_pool
from .pools import close_host_pools as _close_host_pools
from .scheduling.health_routes import cancel_turn_route, stats_route
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


def _report_long_admission_waits(
    host: AgentHost, *, read_alert_seconds: Callable[[], float]
) -> None:
    """Report a long queue episode without cancelling its intentionally silent turn.

    The live alert bound diagnoses admission pressure, not a stalled agent.
    """
    threshold = read_alert_seconds()
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
    *,
    read_alert_seconds: Callable[[], float],
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
        _report_long_admission_waits(host, read_alert_seconds=read_alert_seconds)
        await asyncio.sleep(_LIVENESS_BEAT_STEP_S)


async def _ownership_beat_completion(
    liveness: Liveness,
    host: AgentHost,
    scheduler: TurnScheduler,
    machine: str,
    bus: EventBus,
    *,
    read_alert_seconds: Callable[[], float],
) -> Exception | None:
    """Retain a failed heartbeat for the service's stop/join boundary.

    A heartbeat failure must not cancel boot settlement, dispatch or turn drain.
    Its owning task retains the original exception; shutdown raises it after join.
    """
    try:
        await _beat_forever(
            liveness, host, scheduler, machine, bus, read_alert_seconds=read_alert_seconds
        )
    except Exception as error:
        return error
    return None


async def _start_ownership_beat(
    liveness: Liveness,
    host: AgentHost,
    scheduler: TurnScheduler,
    machine: str,
    bus: EventBus,
    *,
    read_alert_seconds: Callable[[], float],
) -> tuple[asyncio.TaskGroup, asyncio.Task[Exception | None]]:
    """Enter the service-owned heartbeat group before boot settlement starts."""
    tasks = asyncio.TaskGroup()
    await tasks.__aenter__()
    beat = tasks.create_task(
        _ownership_beat_completion(
            liveness, host, scheduler, machine, bus, read_alert_seconds=read_alert_seconds
        ),
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
    """Drain turns and release settled ownership after background loops join.
    Reverse callbacks attempt every cleanup before failure propagates; pools
    must outlive active turns.
    """
    # Share the existing unwind allowance; late resource join must not add a
    # second five-second window after the scheduler has spent its own budget.
    resource_deadline = asyncio.get_running_loop().time() + CANCEL_UNWIND_TIMEOUT_S
    async with contextlib.AsyncExitStack() as cleanup:
        cleanup.push_async_callback(host.aclose, resource_deadline=resource_deadline)
        cleanup.push_async_callback(_close_ownership_beat, beat, beat_tasks)
        cleanup.push_async_callback(scheduler.aclose)


def _background_loops(
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    db: Database,
    *,
    catalog: ModelCatalog,
    config: ConfigBoot,
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
        "exec_memory_guard": run_memory_guard_forever(_log),
        "understanding_chunks": understanding_loop_forever(
            control_pool,
            db,
            [execute_code],
            catalog=catalog,
            llm_override=config.view.lm.llm_override,
            inputs=understanding_inputs(config),
        ),
    }


async def _build_checkpointer(
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
    plugin_state_classes: Iterable[type[BaseModel]] = (),
    *,
    read_checkpoint_interval: Callable[[], int],
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
    wrap_saver_writes_with_nstep_interval(checkpointer, read_checkpoint_interval)
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


async def _close_joined_host_pools(
    host: AgentHost | None,
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
) -> None:
    if host is None or host.resources_joined:
        await _close_host_pools(workload_pool, control_pool)
    else:
        _log.error("[agent-host] resource join unfinished; keeping pools open until hard exit")


async def _close_process_owners(
    host: AgentHost | None,
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    installation: Installation | None,
    health: asyncio.Server | None,
    beat: asyncio.Task[Exception | None] | None,
    beat_tasks: asyncio.TaskGroup | None,
    *,
    clients: ClientSet | None = None,
    primary: BaseException | None = None,
) -> None:
    """Collect process owners without replacing a failure from boot or turn drain."""
    try:
        async with contextlib.AsyncExitStack() as cleanup:
            cleanup.callback(remove_pidfile, _pidfile())
            cleanup.push_async_callback(_close_joined_host_pools, host, workload_pool, control_pool)
            if host is None and clients is not None:
                cleanup.callback(clients.close)
            if installation is not None:
                cleanup.callback(installation.sampling.close)
            if health is not None:
                cleanup.push_async_callback(stop_health_server, health)
            cleanup.push_async_callback(_close_ownership_beat, beat, beat_tasks)
    except BaseException as secondary:
        if primary is None:
            raise
        primary.add_note(f"Host process cleanup also failed: {secondary!r}")


def _is_running() -> bool:
    """Whether a host is already running. Pid-reuse-safe: a live pid whose argv
    does not name this module is a recycled pid, not an instance."""
    return pidfile_holds_daemon(_pidfile(), _MODULE)


def _boot_handles(
    db: Database, config: ConfigBoot
) -> tuple[
    AsyncConnectionPool[psycopg.AsyncConnection],
    AsyncConnectionPool[psycopg.AsyncConnection],
    EventBus,
    Database,
]:
    """The turn/checkpoint pool, the reserved control pool, the event bus and the database of one host."""
    bus = EventBus(
        EventBusConfig(
            redis_url=config.view.data_plane.redis_url,
            events_channel=config.view.data_plane.events_channel,
        )
    )
    return build_shared_pool(db), build_control_pool(db), bus, db


async def _dispatch_host(
    host: AgentHost,
    scheduler: TurnScheduler,
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    db: Database,
    bus: EventBus,
    local_machine: str,
    *,
    catalog: ModelCatalog,
    config: ConfigBoot,
) -> None:
    """Run dispatcher siblings, joined before the owned heartbeat and turn drain."""
    async with asyncio.TaskGroup() as background:
        background.create_task(
            run_notice_delivery(db, local_machine), name="impersonation_terminal_notices"
        )
        for name, loop in _background_loops(
            control_pool, db, catalog=catalog, config=config
        ).items():
            background.create_task(loop, name=name)
        await InboundWakeDispatcher(
            bus,
            scheduler,
            pending_scan=host.pending_inbound_wakes,
            database_waits=host.database_waits,
            turn_progress=host.turn_progress,
            turn_admission=host.admission,
            stale_after_s=float(config.view.daemon.wedged_agent_inbound_age_seconds),
            recovery_wake_batch=config.view.daemon.host_recovery_wake_batch,
            recovery_wake_inflight=config.view.daemon.host_recovery_wake_inflight,
            scan_interval_s=float(config.view.agent.db_notify_wait_timeout_seconds),
            subscription_read_timeout_s=float(config.view.agent.db_notify_wait_timeout_seconds),
        ).run()
        # The dispatcher runs until cancelled; a return would leave the
        # group waiting on loops that never end, hanging the stop.
        raise RuntimeError("wake dispatcher exited without cancellation")


async def run(
    *,
    config: ConfigBoot,
    database: Callable[[], Database],
    machine: MachineIdentity,
    clients: ClientSet | None = None,
    on_clients_owned: Callable[[], None] | None = None,
) -> None:
    """Boot the host and serve wakes until cancelled. See the module docstring
    for why the order is what it is."""
    assert_clock_lattice()
    if _is_running():
        _log.info("[agent-host] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)
    if not acquire_pidfile(_pidfile(), _MODULE):
        _log.info("[agent-host] could not acquire pidfile %s, exiting", _pidfile())
        sys.exit(1)

    from agent.graph import build_graph  # pyright: ignore[reportUnknownVariableType]
    from agent.process_boot import (
        init_process_scope,
        land_cluster_extensions,
    )

    workload_pool, control_pool, bus, db = _boot_handles(database(), config)
    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    beat: asyncio.Task[Exception | None] | None = None
    beat_tasks: asyncio.TaskGroup | None = None
    health, installation = None, None
    host: AgentHost | None = None
    try:
        if on_clients_owned is not None:
            on_clients_owned()
        init_process_scope()
        land_cluster_extensions(db)
        local_machine = machine.name()
        await _open_host_pools(workload_pool, control_pool, local_machine)
        if clients is None:
            clients = process_clients(database=lambda: db, config=config)
        from agent.extensions import load_extensions

        installation = load_installation(
            config, producer=clients.event_pipeline, load_extensions=load_extensions
        )
        checkpointer = await _build_checkpointer(
            workload_pool,
            [cls for _plugin, cls in installation.registry.state_classes()],
            read_checkpoint_interval=lambda: config.view.agent.checkpoint_interval,
        )
        # One graph shares the dynamic state schema for all hosted turns.
        host = AgentHost(
            pool=workload_pool,
            control_pool=control_pool,
            checkpointer=checkpointer,
            graph=build_graph(checkpointer, installation.registry),
            machine=local_machine,
            bus=bus,
            db=db,
            catalog=installation.require_catalog(),
            policy=host_policy(config),
            clients=clients,
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
            liveness,
            host,
            scheduler,
            local_machine,
            bus,
            read_alert_seconds=lambda: config.view.daemon.host_admission_wait_alert_seconds,
        )
        settled = await settle_stale_running_rows(control_pool, local_machine)
        logger.info("hosted boot settle: settled {n} stale running row(s)", n=len(settled))

        endpoint = _endpoint()
        health = await start_health_server(
            "agent_host",
            endpoint.health_port,
            liveness=liveness,
            extra_routes={
                ("GET", "/stats"): stats_route(host, scheduler),
                ("POST", "/cancel-turn"): cancel_turn_route(scheduler, host),
            },
        )
        logger.info(
            "hosted agent-runner started on :{port} "
            "(max concurrent turns {bound}, database pools {workload}/{control})",
            event="host_started",
            port=endpoint.health_port,
            bound=config.view.daemon.host_max_concurrent_turns or "unlimited",
            workload=workload_pool.max_size,
            control=control_pool.max_size,
        )
        # One TaskGroup owns the loops beside the dispatcher: a loop that
        # raises cancels the dispatcher and its siblings, and the exception
        # leaves `run` so the process exits for `ava-root` to restart it. The
        # group exits, every loop joined, before the runtime drains turns.
        try:
            await _dispatch_host(
                host,
                scheduler,
                control_pool,
                db,
                bus,
                local_machine,
                catalog=installation.require_catalog(),
                config=config,
            )
        finally:
            try:
                await _close_host_runtime(host, scheduler, beat, beat_tasks)
            finally:
                beat = None  # Runtime cleanup attempted its join even when another stage failed.
                beat_tasks = None
    finally:
        await _close_process_owners(
            host,
            workload_pool,
            control_pool,
            installation,
            health,
            beat,
            beat_tasks,
            clients=clients,
            primary=sys.exception(),
        )
        _log.info("[agent-host] daemon stopped")


def _require_helper_parent_chain() -> None:
    """Exit a helper-spawned host whose ancestor chain no longer holds the helper."""
    if parent_chain_intact():
        return
    _log.warning(
        "[agent-host] permissions helper parent chain broken, self-terminating for helper respawn"
    )
    os._exit(70)


def main() -> None:
    """Entry point: SDK posture, schema gate, logging, graceful shutdown, then the loop."""
    import ava

    ava.bind_host_process()
    from base.deploy.schema.migrations import assert_schema_current

    config = ConfigBoot()
    config.boot()
    config.ensure_eager()
    _require_helper_parent_chain()
    assert_schema_current(config.view.data_plane.db_url)
    machine = host_machine(config)
    version = CodeVersion(ava.loaded_code_image())
    gate = ProcessDbGate(version=version.get, process="agent_host")

    def database() -> Database:
        return Database(db_config_from_boot(config), gate=gate, local_host=machine.host)

    clients = process_clients(database=database, config=config)
    clients_handed_off = False

    def hand_off_clients() -> None:
        nonlocal clients_handed_off
        clients_handed_off = True

    initialize_logging(clients, machine, ava.loaded_code_image())
    code = 0
    # Runner avoids automatic shutdown_default_executor joins of to_thread
    # recorders. Explicit drain reaches run's cleanup; hard exit skips executor teardown.
    runner = asyncio.Runner()
    try:
        install_graceful_shutdown("agent_host")
        runner.run(
            run(
                config=config,
                database=database,
                machine=machine,
                clients=clients,
                on_clients_owned=hand_off_clients,
            )
        )
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[agent-host] interrupted, shutting down")
        # Explicit task drain reaches run's health/turn/pool/pidfile cleanup
        # without joining default-executor workers.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[agent-host] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[agent-host] fatal error, shutting down")
        code = 1
    finally:
        # A running host retains its clients when a turn cannot join. Only a
        # failure before that handoff belongs to this boot scope.
        if not clients_handed_off:
            try:
                clients.close()
            except BaseException as secondary:
                primary = sys.exception()
                if primary is not None:
                    primary.add_note(f"Host boot cleanup also failed: {secondary!r}")
                _log.exception("[agent-host] process client cleanup failed")
                code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main()
