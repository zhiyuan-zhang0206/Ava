"""Schedule manager daemon — keeps one resident session alive per enabled schedule.

Two resident sequential loops under one `TaskGroup` (`_run_loops`):

- `reconcile` (`ScheduleManager.reconcile`): every few seconds, desired (enabled
  `schedules` rows) against actual (live `ava-schedule-<id>` sessions) — launch
  the missing ones under the crash backoff and breaker, reap the unwanted, close
  orphaned run rows, alert on a schedule sessionless for two hours.
- `requests` (`services.wake.schedule_manager.requests`): every second, converge the
  schedules the API asked for (start / stop / restart / edit / delete).

The crash backoff, the launch counter and the stall-alert clock are columns of
the schedule row, so a restart of this service resumes them. A loop that raises
cancels its sibling and the exception leaves `run`: the process exits and the
supervisor restarts it; the schedule sessions themselves survive the service.
An unreachable database skips a round. Each loop reports its own progress to
`/healthz`.

Runs on the gateway, one per cluster, from the home's own checkout: a checkout
that does not own the home refuses to supervise schedules, because the sessions
run that checkout's code (issue #194). Kept alive by the root supervisor's health
monitor through the roster's `/healthz` identity probe (`ops/roster/healthz.py`).

Usage:
    .venv/bin/python -m services.wake.schedule_manager.daemon
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from base.config import settings
from base.daemon import round_loop
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.loop_health import LivenessGroup
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.log import init_gateway_process
from base.paths import prod_service_checkout_error
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.wake.schedule_manager import requests
from services.wake.schedule_manager.manager import POLL_INTERVAL_S, REPO_ROOT, ScheduleManager

_log = logging.getLogger("services.wake.schedule_manager.daemon")

# One reconcile can reap several sessions and launch several runners, each a
# bounded backend call; a loop that has finished no round for this long is wedged.
_LIVENESS_TIMEOUT_S = 300.0
_REQUESTS_INTERVAL_S = 1.0
# The two loops' concurrent statements, each in its own worker thread.
_POOL_MAX_SIZE = 4


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("schedule_manager")


def _pidfile() -> Path:
    return _endpoint().pidfile


def _is_running() -> bool:
    """Whether a daemon is already running (via its pidfile); pid-reuse-safe."""
    return pidfile_holds_daemon(_pidfile(), "services.wake.schedule_manager.daemon")


async def provision_builtins(pool: ConnectionPool[Any]) -> None:
    """Seed missing built-in schedules and resync drifted ones when requested.

    Runs before the first reconcile, so a built-in launches on the checkout's template, not on
    the snapshot its row was created with. Only ``script`` / ``command`` of an existing row move."""
    if not settings.gateway.provision_builtin_schedules:
        return
    from base.daemon.schedules.builtin_schedules import (
        ProvisionResult,
        provision_builtin_schedules,
    )

    def provision() -> ProvisionResult:
        # Pool acquisition and provisioning both block; keep them off the event loop.
        with pool.connection() as conn:
            return provision_builtin_schedules(conn)

    try:
        result = await asyncio.to_thread(provision)
        if result.created:
            _log.info("provisioned built-in schedules: %s", ", ".join(result.created))
        if result.resynced:
            _log.info(
                "resynced built-in schedule scripts to the checkout: %s", ", ".join(result.resynced)
            )
    except Exception:
        _log.warning("built-in schedule provisioning failed", exc_info=True)


async def _run_loops(pool: ConnectionPool, liveness: LivenessGroup) -> None:
    """Own the two resident loops; one that raises ends the process."""
    manager = ScheduleManager(pool)
    reconcile_progress = liveness.register("reconcile", _LIVENESS_TIMEOUT_S)
    requests_progress = liveness.register("requests", _LIVENESS_TIMEOUT_S)

    async def reconcile_round() -> None:
        await asyncio.to_thread(manager.reconcile)

    async def requests_round() -> None:
        await asyncio.to_thread(requests.consume_requests, pool, manager)

    async with asyncio.TaskGroup() as loops:
        loops.create_task(
            round_loop.run_rounds("reconcile", reconcile_progress, POLL_INTERVAL_S, reconcile_round)
        )
        loops.create_task(
            round_loop.run_rounds(
                "requests", requests_progress, _REQUESTS_INTERVAL_S, requests_round
            )
        )


async def run() -> None:
    """Start the daemon: checkout guard -> pidfile -> healthz -> DB -> loops."""
    refusal = prod_service_checkout_error(REPO_ROOT)
    if refusal is not None:
        raise RuntimeError(f"schedule supervision refused: {refusal}")
    if _is_running() or not acquire_pidfile(_pidfile(), "services.wake.schedule_manager.daemon"):
        _log.info("[schedule-manager] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)
    _log.info("[schedule-manager] pidfile written: %s", _pidfile())

    liveness = LivenessGroup()
    endpoint = _endpoint()
    health = await start_health_server("schedule_manager", endpoint.health_port, liveness=liveness)
    _log.info("[schedule-manager] healthz listening on :%s", endpoint.health_port)

    pool = Database.from_settings().pool(max_size=_POOL_MAX_SIZE)
    try:
        # Automatic seeding is explicit configuration; unseeded previews still use
        # the schedule APIs without launching background workloads.
        await provision_builtins(pool)
        await _run_loops(pool, liveness)
    finally:
        pool.close()
        await stop_health_server(health)
        remove_pidfile(_pidfile())
        _log.info("[schedule-manager] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop."""
    from base.deploy.schema.migrations import assert_schema_current

    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="schedule_manager")
    install_graceful_shutdown("schedule_manager")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — and a stop signal must never wait on those (see `_hard_exit`).
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[schedule-manager] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loops'
        # tasks explicitly: `run`'s finally still stops the health server,
        # closes the DB pool and removes the pidfile.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[schedule-manager] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[schedule-manager] fatal error, shutting down")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main()
