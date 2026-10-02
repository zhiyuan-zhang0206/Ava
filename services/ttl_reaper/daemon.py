"""TTL reaper daemon — enforces every wall-clock deadline the cluster hands out.

Two resident sequential loops under one `TaskGroup` (`_run_loops`):

- `sweep` (`services.ttl_reaper.sweep`): database-only reclaims — expired pages,
  browser sessions, notices and impersonation leases — plus the slow maintenance
  phases (fire-log prune, lifecycle-pointer scan, absent-machine fence settle)
  on cadences kept in `maintenance_state`.
- `remote` (`services.ttl_reaper.remote`): the phases that dial other machines —
  TTL-expired persistent shell sessions are killed on their home machines
  (concurrently across machines), and stale work-failure deliveries are retried.

A loop that raises cancels its sibling and the exception leaves `run`: the
process exits and the supervisor restarts it. An unreachable database skips a
round. Each loop reports its own progress to `/healthz`, so a wedged loop reads
as a failing probe even while its sibling stays busy.

Runs on the gateway, one per cluster. Kept alive by the root supervisor's health
monitor through the roster's `/healthz` identity probe (`ops/roster/healthz.py`).

Usage:
    .venv/bin/python -m services.ttl_reaper.daemon
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

from psycopg_pool import ConnectionPool

from base.config import settings
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.loop_health import LivenessGroup
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.log import init_gateway_process
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.ttl_reaper import remote, shells, sweep

_log = logging.getLogger("services.ttl_reaper.daemon")

# Liveness slack above one step's deadline: a loop that has completed no step for
# the deadline plus this reads as wedged on /healthz.
_LIVENESS_SLACK_S = 60.0
# A sweep step is one bounded batch of database statements.
_SWEEP_LIVENESS_TIMEOUT_S = 300.0
# Connections the two loops' concurrent statements can hold at once.
_POOL_MAX_SIZE = 4


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("ttl_reaper")


def _pidfile() -> Path:
    return _endpoint().pidfile


def _is_running() -> bool:
    """Whether a daemon is already running (via its pidfile); pid-reuse-safe."""
    return pidfile_holds_daemon(_pidfile(), "services.ttl_reaper.daemon")


async def _run_loops(pool: ConnectionPool, liveness: LivenessGroup) -> None:
    """Own the two resident loops; one that raises ends the process."""
    sweep_progress = liveness.register("sweep", _SWEEP_LIVENESS_TIMEOUT_S)
    remote_progress = liveness.register("remote", shells.dispatch_deadline_s() + _LIVENESS_SLACK_S)
    async with asyncio.TaskGroup() as loops:
        loops.create_task(sweep.sweep_loop(pool, sweep_progress))
        loops.create_task(remote.remote_loop(pool, remote_progress))


async def run() -> None:
    """Start the daemon: pidfile -> healthz server -> connect DB -> loops."""
    if _is_running() or not acquire_pidfile(_pidfile(), "services.ttl_reaper.daemon"):
        _log.info("[ttl-reaper] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)
    _log.info("[ttl-reaper] pidfile written: %s", _pidfile())

    liveness = LivenessGroup()
    endpoint = _endpoint()
    health = await start_health_server("ttl_reaper", endpoint.health_port, liveness=liveness)
    _log.info("[ttl-reaper] healthz listening on :%s", endpoint.health_port)

    pool = Database.from_settings().pool(max_size=_POOL_MAX_SIZE)
    try:
        await _run_loops(pool, liveness)
    finally:
        pool.close()
        await stop_health_server(health)
        remove_pidfile(_pidfile())
        _log.info("[ttl-reaper] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop."""
    from base.deploy.schema.migrations import assert_schema_current

    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="ttl_reaper")
    install_graceful_shutdown("ttl_reaper")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — and a stop signal must never wait on those (see `_hard_exit`).
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[ttl-reaper] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loops'
        # tasks explicitly: `run`'s finally still stops the health server,
        # closes the DB pool and removes the pidfile.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[ttl-reaper] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[ttl-reaper] fatal error, shutting down")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main()
