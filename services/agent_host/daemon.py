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
from pathlib import Path
from typing import cast

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

import shared.redis_client
from agent._turn_progress import turn_progress_age_s, turn_progress_snapshot
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.agent_host.dispatcher import InboundWakeDispatcher, TurnScheduler
from services.agent_host.host import AgentHost, settle_stale_running_rows
from services.agent_host.pools import build_control_pool, build_shared_pool
from shared import paths
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.helper_chain_guard import parent_chain_intact
from shared.hosted_force import recover_orphaned_hosted_forces
from shared.log import init_gateway_process, logger
from shared.machine import machine_name
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

# The launcher redirects fd 1/2 straight at $AVA_HOME/logs/ava-agent-host.out.log
# — no pty transcript cap, no loguru rotation, nothing owns the file but the
# process itself — so a crash storm that floods the transcript with tracebacks
# can balloon it to a disk-filling size (2026-09-03: 13.5 GB in ~6 minutes,
# task #2356). The daemon re-points its own fds once the file crosses a
# ceiling; see `_rotate_stdout_log_if_needed`.
_STDOUT_LOG_ROTATE_BYTES = 1 << 30  # 1 GiB — rotate the raw transcript past this
_STDOUT_LOG_ROTATE_POLL_S = 60.0  # cadence of the size check
_STDOUT_LOG_NAME = "ava-agent-host"  # names $AVA_HOME/logs/<name>.out.log


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
        try:
            await asyncio.wait_for(host.renew_ownership(), timeout=_OWNERSHIP_RENEW_TIMEOUT_S)
        except TimeoutError:
            _log.warning("[agent-host] ownership renewal timed out")
        except Exception:
            _log.exception("[agent-host] ownership renewal failed — retrying next beat")
        await _publish_turn_progress_heartbeat(machine, scheduler.active_agents)
        await asyncio.sleep(_LIVENESS_BEAT_STEP_S)


async def _stop_ownership_beat(beat: asyncio.Task[None] | None) -> None:
    if beat is not None:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat


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
        try:
            await reconcile_all_open_pages(pool, interval_s=interval_s, event_publisher=publisher)
        except Exception:
            _log.exception(
                "[agent-host] periodic page reconcile pass failed — retrying next interval"
            )
        await asyncio.sleep(interval_s)


def _stdout_log_path() -> Path:
    """The daemon's raw stdout/stderr transcript.

    The launcher opens this file and redirects fd 1/2 onto it before the
    daemon starts (`$AVA_HOME/logs/<name>.out.log`, the posix-session
    convention); nothing in the logging stack owns it, which is exactly why it
    needs the size rotation below.
    """
    return paths.logs_dir() / f"{_STDOUT_LOG_NAME}.out.log"


def _rotate_stdout_log_files(log_path: Path) -> None:
    """Roll `log_path` over to `log_path.1`, keeping one rotated generation.

    The previous `.1` chunk is atomically replaced (a rename over it), and a
    stray `.2` — left by an older generation scheme or a manual ops roll — is
    dropped. Two generations on disk total: the live file plus the last
    rotated chunk. Replacement rather than a `.1 -> .2` shift is what bounds a
    crash storm: every rotation swaps the previous chunk instead of stacking
    generations, so a flood that would otherwise fill the disk keeps at most
    `ceiling + one poll overshoot` per file.
    """
    one = log_path.with_name(log_path.name + ".1")
    two = log_path.with_name(log_path.name + ".2")
    with contextlib.suppress(OSError):
        two.unlink()
    log_path.replace(one)


def _rotate_stdout_log_if_needed() -> int | None:
    """Rotate the raw transcript once fd 1's file crosses the ceiling.

    Returns the size the file had reached when it was rotated, or None when no
    rotation happened. The rename runs first and fd 1/2 are then dup2'ed onto a
    freshly opened file at the original path, so every later write — `os.write`
    and file objects bound to fd 1/2 alike — lands in the new file; the window
    between the two steps is at most one line, which is logged and accepted. A
    safe no-op when fd 1 is not a regular file (a pipe or tty reports size 0)
    or when the path has gone away.
    """
    try:
        size = os.fstat(1).st_size
    except OSError:
        return None
    if size < _STDOUT_LOG_ROTATE_BYTES:
        return None
    log_path = _stdout_log_path()
    rotated = False
    try:
        _rotate_stdout_log_files(log_path)
        rotated = True
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        if rotated:
            # fd 1/2 still name the moved chunk; put it back under the
            # original path so the next pass can retry — without this a
            # failed open (disk full / EMFILE) strands the transcript under
            # `.1` forever, growing past every bound and never self-healing.
            with contextlib.suppress(OSError):
                one = log_path.with_name(log_path.name + ".1")
                one.replace(log_path)
        _log.exception(
            "[agent-host] stdout log rotation failed — fd 1 still points at the old file"
        )
        return None
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        if fd > 2:
            os.close(fd)
    return size


async def _rotate_stdout_log_forever() -> None:
    """Bound the raw stdout transcript by size for the daemon's whole life.

    The first pass runs immediately so a file that already crossed the ceiling
    (a storm before this code shipped, or between a crash and the watchdog's
    respawn) is absorbed at boot; afterwards the size is checked on a fixed
    cadence. Self-protecting like the other daemon loops: a failed pass is
    logged and the loop waits for the next interval.
    """
    _rotate_stdout_log_if_needed()
    while True:
        await asyncio.sleep(_STDOUT_LOG_ROTATE_POLL_S)
        try:
            rotated_at = _rotate_stdout_log_if_needed()
        except Exception:
            _log.exception("[agent-host] stdout log rotation pass failed — retrying next interval")
            continue
        if rotated_at is not None:
            logger.info(
                "[agent-host] rotated the raw stdout transcript at {size} bytes "
                "(ceiling {ceiling})",
                event="host_stdout_log_rotated",
                size=rotated_at,
                ceiling=_STDOUT_LOG_ROTATE_BYTES,
            )


def _spawn_background_tasks(pool: AsyncConnectionPool) -> dict[str, asyncio.Task[object]]:
    """Create the daemon's three long-lived background tasks — the plugins
    watch, the page reconciler, and the raw-stdout size rotation.

    Split out of `run()` so the wiring is testable without booting the
    dispatcher: the reconciler's existence is what closes the
    busy-hosted-agent dead-page gap (task #2260), the rotator's is what keeps a
    traceback storm from filling the disk through the uncapped raw transcript
    (task #2356), and a regression that dropped either creation must turn a
    test red rather than silently reopen the gap.
    """
    return {
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
    from shared.config.turn_view import turn_settings

    saver_pool = cast(AsyncConnectionPool[psycopg.AsyncConnection[DictRow]], pool)
    checkpointer = AsyncPostgresSaver(conn=saver_pool, serde=build_checkpoint_serde())
    _wrap_saver_writes_with_loud_failure(checkpointer)
    _wrap_saver_writes_with_nstep_interval(
        checkpointer,
        lambda: turn_settings.agent.checkpoint_interval,
    )
    return checkpointer


async def _recover_hosted_forces_at_boot(
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection], machine: str
) -> None:
    """Recover only resource-free predecessor forces before scheduling starts."""
    recovered, deferred = await recover_orphaned_hosted_forces(control_pool, machine)
    logger.info("hosted boot recovery: observed {n} orphaned force(s)", n=len(recovered))
    for agent_id, evidence in deferred.items():
        logger.warning(
            "hosted boot recovery deferred for agent {agent_id}: "
            "persistent exec request evidence {evidence}",
            agent_id=agent_id,
            evidence=[str(path) for path in evidence],
        )


async def _schedule_watcher_recovery(host: AgentHost, scheduler: TurnScheduler) -> None:
    """Once per host boot, prepare watcher owners even with an empty inbox."""
    for agent_id in await host.watcher_boot_wakes():
        scheduler.wake(agent_id)


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
        settled = await settle_stale_running_rows(control_pool, local_machine)
        logger.info("hosted boot settle: settled {n} stale running row(s)", n=len(settled))

        health = await start_health_server(
            "agent_host",
            liveness=liveness,
            extra_routes={
                ("GET", "/stats"): _stats_route(host, scheduler),
                ("POST", "/cancel-turn"): _cancel_turn_route(scheduler, host),
            },
        )
        logger.info(
            "hosted agent-runner started on :{port} (max concurrent turns {bound})",
            event="host_started",
            port=health_port("agent_host"),
            bound=settings.daemon.host_max_concurrent_turns,
        )
        # Task #2260: heartbeat-independent page-liveness scan for hosted
        # agents — busy agents get no heartbeats, and the hosted daemon runs
        # no per-agent page_reconcile_loop (loop.py:main() is process-only).
        background = _spawn_background_tasks(workload_pool)
        try:
            await _schedule_watcher_recovery(host, scheduler)
            await InboundWakeDispatcher(
                settings.data_plane.redis_url,
                scheduler,
                pending_scan=host.pending_inbound_wakes,
                stale_after_s=float(settings.daemon.wedged_agent_inbound_age_seconds),
                scan_interval_s=float(settings.agent.db_notify_wait_timeout_seconds),
                subscription_read_timeout_s=float(settings.agent.db_notify_wait_timeout_seconds),
            ).run()
        finally:
            for task in background.values():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                for task in background.values():
                    await task
            # Turns are checkpointed, so cancelling one loses at most the
            # in-flight step — the same recovery path a runner restart already
            # exercises, and the reason a rolling restart is cheap here.
            await scheduler.aclose()
            await _stop_ownership_beat(beat)
            beat = None
            await host.aclose()
    finally:
        await _stop_ownership_beat(beat)
        if health is not None:
            await stop_health_server(health)
        await _close_host_pools(workload_pool, control_pool)
        remove_pidfile(_PIDFILE)
        _log.info("[agent-host] daemon stopped")


def _require_helper_parent_chain() -> None:
    """Exit a helper-spawned host whose direct-parent chain was broken."""
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
    from shared.migrations import assert_schema_current

    _require_helper_parent_chain()
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="agent_host")
    install_graceful_shutdown("agent_host")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[agent-host] interrupted, shutting down")
    except Exception:
        _log.exception("[agent-host] fatal error, shutting down")


if __name__ == "__main__":
    main()
