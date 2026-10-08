"""Insights service daemon — uvicorn over the read models, on a Unix socket.

The run timeline rebuilds an agent's stitched history from its checkpoints on a cold
read (seconds of CPU for a long-lived agent). That work lives in this process so it
never competes with the gateway's event loop, thread pool or GIL. The service binds
`base.paths.insights_socket()` (mode 0600) and serves no TCP port; the gateway proxies
authenticated requests to it. `/healthz` is the standard daemon health endpoint on the
`insights` slot of the fixed port table.

A stopped service leaves the gateway answering 502 on the insights routes and nothing
else; the root supervisor restarts it through the roster's `/healthz` identity probe
(`ops/roster/healthz.py`). A cold build in a worker thread is never waited on at stop
(`_hard_exit`).

Usage:
    .venv/bin/python -m services.derived.insights.daemon
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from pathlib import Path

import uvicorn

from base.config import settings
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import Liveness, start_health_server, stop_health_server
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.log import init_gateway_process
from base.paths import insights_socket
from services.derived.insights.app import build_app
from services.derived.insights.config import InsightsConfig
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

_log = logging.getLogger("services.derived.insights.daemon")

_MODULE = "services.derived.insights.daemon"
# A loop that has not run its beat for this long is wedged (a GIL-starved loop is the
# case the health endpoint exists to expose); a request in a worker thread cannot hold it.
_LIVENESS_TIMEOUT_S = 60.0
_BEAT_INTERVAL_S = 5.0
# Short row lookups only (the agent's model and config for a context breakdown); history
# and audit reads open their own connections through `Database`.
_POOL_MAX_SIZE = 4


def insights_config() -> InsightsConfig:
    """The composition root: the one place this package reads `settings`."""
    return InsightsConfig(
        run_timeline_message_text_max=settings.display.run_timeline_message_text_max
    )


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("insights")


def bind_socket(path: Path) -> socket.socket:
    """A listening-ready Unix socket at `path`, readable and writable by the owner only.

    Only the one daemon the pidfile admits reaches this, so a file already at `path` is a
    predecessor's leftover and is replaced. The umask closes the window between bind and a
    later chmod in which another local user could connect.
    """
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o177)
    try:
        sock.bind(str(path))
    except BaseException:
        sock.close()
        raise
    finally:
        os.umask(previous)
    return sock


async def _beat(liveness: Liveness) -> None:
    # quiesce-exempt: stamps this process's own event-loop liveness; no database, no work to pause
    while True:
        liveness.beat()
        await asyncio.sleep(_BEAT_INTERVAL_S)


async def run() -> None:
    """Start the daemon: pidfile -> healthz -> database -> socket -> serve until stopped."""
    endpoint = _endpoint()
    if pidfile_holds_daemon(endpoint.pidfile, _MODULE) or not acquire_pidfile(
        endpoint.pidfile, _MODULE
    ):
        _log.info("[insights] daemon already running (pidfile=%s), exiting", endpoint.pidfile)
        sys.exit(1)
    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("insights", endpoint.health_port, liveness=liveness)
    _log.info("[insights] healthz listening on :%s", endpoint.health_port)
    path = insights_socket()
    db = Database.from_settings()
    pool = db.pool(max_size=_POOL_MAX_SIZE)
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                build_app(db, pool, insights_config()),
                log_level="warning",
                access_log=False,
                log_config=None,
            )
        )
        sock = bind_socket(path)
        _log.info("[insights] serving on %s", path)
        async with asyncio.TaskGroup() as group:
            beat = group.create_task(_beat(liveness))
            try:
                await server.serve(sockets=[sock])
            finally:
                beat.cancel()
    finally:
        pool.close()
        path.unlink(missing_ok=True)
        await stop_health_server(health)
        remove_pidfile(endpoint.pidfile)
        _log.info("[insights] daemon stopped")


def main() -> None:
    """Entry point: schema check -> log init -> serve -> bounded exit."""
    from base.deploy.schema.migrations import assert_schema_current

    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="insights")
    install_graceful_shutdown("insights")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that awaits
    # `shutdown_default_executor`, joining the workers — and a cold history build is a
    # multi-second worker that a stop signal must never wait on (see `_hard_exit`). On the
    # signal path uvicorn re-raises the signal after its own graceful stop, landing here.
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[insights] interrupted, shutting down")
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[insights] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[insights] fatal error, shutting down")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main()
