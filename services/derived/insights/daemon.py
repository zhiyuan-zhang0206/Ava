"""Insights service daemon — uvicorn over the read models, on a Unix socket.

The run timeline rebuilds an agent's stitched history from its checkpoints on a cold
read (seconds of CPU for a long-lived agent). That work lives in this process so it
never competes with the gateway's event loop, thread pool or GIL. The service binds
`base.paths.insights_socket()` (mode 0600) and serves no TCP port; the gateway proxies
authenticated requests to it. Its `/healthz` answers on the same socket, so it needs no
port slot; the supervisor probes it through `services.supervision.healthchecks.insights`.

A stopped service leaves the gateway answering 502 on the insights routes and nothing
else; the root supervisor restarts it through the roster's socket identity probe
(`ops/roster/__init__.py`). A cold build in a worker thread is never waited on at stop
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
from collections.abc import Callable
from pathlib import Path

import uvicorn

from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import Clock, clock_config_from_boot
from base.cluster.machine import validate_machine_name
from base.config import ConfigBoot
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.lm.plugin_providers import build_model_catalog
from base.log import init_gateway_process
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import insights_pidfile, insights_socket
from base.telemetry import build_pipeline
from services.derived.insights.app import build_app
from services.derived.insights.config import InsightsConfig
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

_log = logging.getLogger("services.derived.insights.daemon")

_MODULE = "services.derived.insights.daemon"
# Short row lookups only (the agent's model and config for a context breakdown); history
# and audit reads open their own connections through `Database`.
_POOL_MAX_SIZE = 4


def insights_config(*, config: ConfigBoot) -> InsightsConfig:
    """The display slice from this daemon's configuration owner."""
    return InsightsConfig(
        run_timeline_message_text_max=config.view.display.run_timeline_message_text_max
    )


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


async def run(*, config: ConfigBoot, database: Callable[[], Database]) -> None:
    """Start the daemon: pidfile -> database -> socket -> serve until stopped."""
    pidfile = insights_pidfile()
    if pidfile_holds_daemon(pidfile, _MODULE) or not acquire_pidfile(pidfile, _MODULE):
        _log.info("[insights] daemon already running (pidfile=%s), exiting", pidfile)
        sys.exit(1)
    path = insights_socket()
    db = database()
    pool = db.pool(max_size=_POOL_MAX_SIZE)
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                build_app(
                    db,
                    pool,
                    insights_config(config=config),
                    catalog=build_model_catalog(),
                    default_model_reader=lambda: config.view.lm.llm_model,
                    timeline_inputs=TimelineReadInputs(
                        clock_factory=lambda: Clock(clock_config_from_boot(config)),
                        timestamps_enabled=lambda: config.view.general.message_timestamps,
                    ),
                ),
                log_level="warning",
                access_log=False,
                log_config=None,
            )
        )
        sock = bind_socket(path)
        _log.info("[insights] serving on %s", path)
        await server.serve(sockets=[sock])
    finally:
        pool.close()
        path.unlink(missing_ok=True)
        remove_pidfile(pidfile)
        _log.info("[insights] daemon stopped")


def main() -> None:
    """Entry point: schema check -> log init -> serve -> bounded exit."""
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()
    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    config = ConfigBoot()
    config.boot()
    assert_schema_current(config.view.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="insights")

    def database() -> Database:
        return Database(db_config_from_boot(config), gate=gate)

    pipeline = build_pipeline(database=database)
    init_gateway_process(
        name="insights",
        producer=lambda: pipeline,
        machine_reader=lambda: validate_machine_name(config.view.general.machine_name),
        image=image,
    )
    install_graceful_shutdown("insights")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that awaits
    # `shutdown_default_executor`, joining the workers — and a cold history build is a
    # multi-second worker that a stop signal must never wait on (see `_hard_exit`). On the
    # signal path uvicorn re-raises the signal after its own graceful stop, landing here.
    runner = asyncio.Runner()
    try:
        runner.run(run(config=config, database=database))
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
