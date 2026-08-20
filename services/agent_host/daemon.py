"""Agent-host daemon — the supervised process that runs every local agent's turns.

Phase 1 of `future/infra/agent-runner-as-server.md`, and the piece that makes the
other three real: `dispatcher.py` turns wakes into turn tasks, `host.py` runs a
turn, and this module is the long-running process they live in — pidfile,
healthz, process-scope boot, and the shutdown that drains them.

Runs only where `AVA_RUNNER_MODE` is `hosted`; the ops roster gates it out
everywhere else (`ops/spec.py`), and `_refuse_in_process_mode` below refuses a
hand-started one for the same reason. Both checks matter: the roster gate keeps
it off the start path, and the in-process check keeps a stray `python -m
services.agent_host.daemon` from quietly double-serving agents that already have
processes of their own.

Usage:
    .venv/bin/python -m services.agent_host.daemon

## Boot order, and why it is this one

1. **Refuse unless hosted**, before anything expensive.
2. **Pidfile**, so a second instance exits instead of racing the first for turns.
3. **Process-scope boot** — `init_process_scope` (trace export; must precede any
   model build so OpenLLMetry can instrument it) then `load_process_extensions`
   (the external-plugin load). Exactly once per process: see
   `agent/_process_boot.py:load_process_extensions` for why repeating it is not
   an option, and issue #170 for the behavioural change that follows.
4. **The shared data plane** — pool, checkpointer, graph. `build_graph` runs the
   builtin-plugin load and the state-class build, which is the other half of
   "once per process".
5. **Healthz**, published only after the above, so a green probe means the host
   can actually take a turn.
6. **The dispatcher**, last: subscribing before the host can serve would drop
   wakes on the floor.

The daemon holds no agent identity. `init_gateway_process` leaves the log sink's
process agent unset, so every line is attributed by the turn contextvar the host
binds — one process, correct per-agent log files.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from typing import cast

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.agent_host.dispatcher import InboundWakeDispatcher, TurnScheduler
from services.agent_host.host import AgentHost, build_shared_pool
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.log import init_gateway_process, logger

_log = logging.getLogger("services.agent_host.daemon")

_MODULE = "services.agent_host.daemon"
_PIDFILE = settings.services.agent_host_pidfile

# The host is event-driven — it can legitimately sit for hours with no wake —
# so liveness is beaten on a fixed timer rather than by work. The ceiling only
# has to exceed the beat step.
_LIVENESS_TIMEOUT_S = 60.0
_LIVENESS_BEAT_STEP_S = 15.0


def _refuse_in_process_mode() -> None:
    """Exit unless this cluster asked for hosted mode.

    The roster gate in `ops/spec.py` is the primary control; this is the second
    line, and it exists because the two failure directions are not symmetric.
    Failing to start a host on a hosted cluster is visible immediately (agents
    stop taking turns). Starting one on a PROCESS cluster is not: every agent
    already has its own process, so the host would quietly become a second
    claimant for the same inbound rows, and the claim CAS would hide the
    duplication as ordinary contention.

    Read the same defensive way the gate reads it — anything that is not exactly
    `hosted` means process — so a config surprise stops the daemon rather than
    starting it.
    """
    mode = _runner_mode()
    if mode != "hosted":
        _log.info(
            "[agent-host] AVA_RUNNER_MODE is %r, not 'hosted' — this cluster runs a process "
            "per agent and does not want a host; exiting",
            mode,
        )
        sys.exit(0)


def _runner_mode() -> str:
    """The cluster's runner mode, read so that it cannot raise.

    A byte-identical copy of `ops/spec.py:_runner_mode`, deliberately duplicated
    rather than imported: that one runs inside `_gate_reason`'s fail-OPEN
    wrapper, so an import failure there would start the service instead of
    gating it out. Four lines with no dependencies cannot fail that way. Any
    failure to read the setting resolves to `"process"`, which is the safe
    answer in both places — the roster keeps the host out, and this daemon
    refuses to start.
    """
    try:
        return str(settings.daemon.runner_mode)
    except Exception:  # see the docstring: unreadable means process, always
        return "process"


async def _beat_forever(liveness: Liveness) -> None:
    """Keep /healthz fresh while the host waits for wakes.

    The host's main loop is the dispatcher's subscription, which blocks for as
    long as the cluster is quiet. Without this the probe would read a healthy
    idle host as a wedged one.
    """
    while True:
        liveness.beat()
        await asyncio.sleep(_LIVENESS_BEAT_STEP_S)


async def _build_checkpointer(
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
) -> AsyncPostgresSaver:
    """One saver for the whole host, over the shared pool.

    No `setup()` call: the runner role holds no CREATE on the schema by design
    (task #1236), and the gateway owns langgraph's own migrations. A host booting
    against a schema that lacks the checkpoint tables fails on first use, loudly,
    which is the correct outcome for a runner that should never have been
    pointed at an unmigrated database.
    """
    from agent.state import build_checkpoint_serde

    saver_pool = cast(AsyncConnectionPool[psycopg.AsyncConnection[DictRow]], pool)
    return AsyncPostgresSaver(conn=saver_pool, serde=build_checkpoint_serde())


def _is_running() -> bool:
    """Whether a host is already running. Pid-reuse-safe: a live pid whose argv
    does not name this module is a recycled pid, not an instance."""
    return pidfile_holds_daemon(_PIDFILE, _MODULE)


async def run() -> None:
    """Boot the host and serve wakes until cancelled. See the module docstring
    for why the order is what it is."""
    _refuse_in_process_mode()
    if _is_running():
        _log.info("[agent-host] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)
    if not acquire_pidfile(_PIDFILE, _MODULE):
        _log.info("[agent-host] could not acquire pidfile %s, exiting", _PIDFILE)
        sys.exit(1)

    from agent._process_boot import init_process_scope, load_process_extensions

    # langgraph types its checkpointer parameter with an unparameterized generic,
    # so the imported symbol reads as partially unknown; the return type — the
    # only part this module uses — is fully known.
    from agent.graph import build_graph  # pyright: ignore[reportUnknownVariableType]

    init_process_scope()
    load_process_extensions()

    pool = build_shared_pool(settings.data_plane.db_url)
    await pool.open()
    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    beat = asyncio.create_task(_beat_forever(liveness))
    health = None
    try:
        checkpointer = await _build_checkpointer(pool)
        # build_graph runs the builtin-plugin load and builds the dynamic state
        # class — process-global, and the reason there is ONE graph here rather
        # than one per agent (services/agent_host/host.py explains the cost).
        graph = build_graph(checkpointer)
        host = AgentHost(pool=pool, checkpointer=checkpointer, graph=graph)
        # The clock reader is injected, not imported by the scheduler: it owns no
        # pool, and this keeps the uncancellable-turn report able to say how long
        # a stuck agent has really been silent.
        scheduler = TurnScheduler(host.run_turn, activity_clock=host.last_active_at)

        health = await start_health_server(
            "agent_host",
            liveness=liveness,
            extra_routes={("GET", "/stats"): _stats_route(host, scheduler)},
        )
        logger.info(
            "hosted agent-runner started on :{port} (max concurrent turns {bound})",
            event="host_started",
            port=health_port("agent_host"),
            bound=settings.daemon.host_max_concurrent_turns,
        )
        try:
            await InboundWakeDispatcher(settings.data_plane.redis_url, scheduler).run()
        finally:
            # Turns are checkpointed, so cancelling one loses at most the
            # in-flight step — the same recovery path a runner restart already
            # exercises, and the reason a rolling restart is cheap here.
            await scheduler.aclose()
            await host.aclose()
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        if health is not None:
            await stop_health_server(health)
        await pool.close()
        remove_pidfile(_PIDFILE)
        _log.info("[agent-host] daemon stopped")


def _stats_route(host: AgentHost, scheduler: TurnScheduler):  # noqa: ANN202 — RouteHandler, declared in shared.daemon_health
    """A `/stats` handler exposing the cache counters and who is running.

    Cheap to serve and the only place the cold-build hit/miss ratio is
    observable as a level rather than as a stream of events — a host whose
    misses track its turns is thrashing its cache, which the events alone make
    you count.
    """
    import json

    async def handler(_body: bytes) -> tuple[int, bytes, str]:
        payload = {
            **host.stats.as_payload(),
            "active_agents": sorted(scheduler.active_agents),
        }
        return 200, json.dumps(payload).encode(), "application/json"

    return handler


def main() -> None:
    """Entry point: schema gate, logging, graceful shutdown, then the loop."""
    from shared.migrations import assert_schema_current

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
