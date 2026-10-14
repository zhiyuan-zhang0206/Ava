"""Gateway-owned scheduler for the daily local Postgres dump.

The dump runs in this supervised process so its one-hour subprocess deadline
cannot delay a watchdog round. Its health component reports the age of the
last successful backup; the watchdog therefore only probes and restarts this
daemon when its schedule has stopped making progress.

A failed dump is attempted once per cluster day: the daemon reports it through
the `backup_dump_failed` event and its health detail, then waits for the next
backup hour. A degraded health component would make the watchdog restart the
daemon, and a restarted daemon dumps again at once, so a failure is deliberately
not reported as a degraded status; only a missing success for
`BACKUP_STALE_AFTER_S` is.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

from base import telemetry
from base.clock import SYSTEM_TIMERS, Clock, Timers, clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.health_schema import DEGRADED, OK, component
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.exit_codes import hard_exit as _hard_exit
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import ava_home
from base.telemetry.pipeline_build import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.backup.dump import cluster_tz, is_due
from services.backup.scheduler.operation.staging import OperationBusyError
from services.backup.scheduler.recovery_drill import (
    load_local_dump_restore_success,
    local_dump_restore_due,
    record_local_dump_restore_success,
)
from services.backup.scheduler.worker import run_job
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

_log = logging.getLogger("services.backup.scheduler.daemon")

BACKUP_STALE_AFTER_S = 26 * 3600
_SLEEP_CHUNK_S = 60
_FAILURE_DETAIL_CHARS = 500


def _endpoint(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
    runtime.mkdir(parents=True, exist_ok=True)
    return ServiceEndpoints.from_boot(config, runtime=runtime).of("pg_backup")


def _pidfile(config: ConfigBoot, *, runtime: Path) -> Path:
    return _endpoint(config, runtime=runtime).pidfile


@dataclass
class BackupState:
    """Scheduler state, owned by its event-loop thread and read by `/healthz`."""

    started_at: float
    running: bool = False
    last_attempt: float | None = None
    last_success: float | None = None
    last_error: str | None = None
    detail: str | None = None

    def record_attempt(self, now: datetime) -> None:
        self.last_attempt = now.timestamp()

    def record_success(self, now: datetime) -> None:
        self.last_success = now.timestamp()
        self.last_error = None
        self.detail = None

    def record_error(self, detail: str) -> None:
        self.last_error = detail
        self.detail = detail


def write_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    if not acquire_pidfile(_pidfile(config, runtime=runtime), "services.backup.scheduler.daemon"):
        _log.info(
            "[pg-backup] daemon already running (pidfile=%s), exiting",
            _pidfile(config, runtime=runtime),
        )
        sys.exit(1)


def _remove_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    remove_pidfile(_pidfile(config, runtime=runtime))


def _is_running(config: ConfigBoot, *, runtime: Path) -> bool:
    """Whether this daemon already owns its pidfile."""
    return pidfile_holds_daemon(
        _pidfile(config, runtime=runtime), "services.backup.scheduler.daemon"
    )


def backup_components(state: BackupState, *, clock: Clock) -> list[dict[str, object]]:
    """Report scheduler progress; a running dump is healthy until it returns.

    `run_backup` has its own one-hour bound. A just-booted cluster gets one
    schedule-plus-slack interval before it is judged overdue, preserving the
    first-run catch-up behaviour without declaring a new service unhealthy.
    """
    wall_time = clock.now().timestamp()
    if state.running:
        status = OK
        started_at = state.last_attempt if state.last_attempt is not None else wall_time
        progress = f"running {wall_time - started_at:.0f}s"
        detail = None
    elif state.last_error is not None:
        status = OK
        progress = "idle"
        detail = f"last dump failed: {state.detail}; next attempt at the next backup hour"
    elif state.last_success is not None:
        age_s = wall_time - state.last_success
        status = OK if age_s <= BACKUP_STALE_AFTER_S else DEGRADED
        progress = "idle"
        detail = None if status == OK else f"last successful backup {age_s:.0f}s ago"
    else:
        age_s = clock.monotonic() - state.started_at
        status = OK if age_s <= BACKUP_STALE_AFTER_S else DEGRADED
        progress = "idle"
        detail = None if status == OK else f"no successful backup within {age_s:.0f}s of start"

    record = component(
        "backup",
        status,
        last_success=state.last_success,
        progress=progress,
        detail=detail,
        now=wall_time if state.last_success is not None else None,
    )
    if state.last_error is not None:
        record["last_error"] = state.last_error
    return [record]


async def sleep_in_chunks(seconds: float, *, timers: Timers) -> None:
    """Sleep in bounded chunks so SIGTERM reaches the scheduler promptly."""
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, _SLEEP_CHUNK_S)
        await timers.asleep(chunk)
        remaining -= chunk


def seconds_until_next_backup_hour(now: datetime, *, config: ConfigBoot, clock: Clock) -> float:
    """Seconds from `now` to the next configured backup hour on the cluster clock."""
    local_now = now.astimezone(cluster_tz(clock_factory=lambda: clock))
    backup_hour = config.view.services.backup_hour
    next_date = local_now.date()
    target = datetime.combine(next_date, clock_time(hour=backup_hour), tzinfo=local_now.tzinfo)
    if local_now >= target:
        target = datetime.combine(
            next_date + timedelta(days=1),
            clock_time(hour=backup_hour),
            tzinfo=local_now.tzinfo,
        )
    return (target.astimezone(UTC) - now).total_seconds()


async def run_due_local_dump_restore(now: datetime, *, config: ConfigBoot, clock: Clock) -> None:
    """Run one weekly local restore proof without re-running the daily dump."""
    try:
        if not local_dump_restore_due(
            now,
            last_success=load_local_dump_restore_success(),
            clock_factory=lambda: clock,
            hour_reader=lambda: config.view.services.backup_hour,
        ):
            return
        await run_job("restore", config=config)
        record_local_dump_restore_success(now)
    except OperationBusyError as exc:
        _log.info("[pg-backup] local restore drill deferred: %s", exc)
    except Exception as exc:
        telemetry.emit(
            "telemetry",
            "recovery_drill_failed",
            level="error",
            attributes={"drill": "logical_dump", "detail": str(exc)},
        )
        _log.exception("[pg-backup] local restore drill failed")


async def backup_loop(state: BackupState, *, config: ConfigBoot, clock: Clock) -> None:
    """Run due dumps; a failed dump is reported and not attempted again before the next backup hour."""
    _log.info("[pg-backup] scheduler started, pid=%s", os.getpid())
    # quiesce-exempt: pg_dump dials the direct URL read-only, not the pool; a held pause must not stop backups
    while True:
        now = clock.now()
        if not is_due(
            now,
            clock_factory=lambda: clock,
            hour_reader=lambda: config.view.services.backup_hour,
        ):
            await sleep_in_chunks(
                seconds_until_next_backup_hour(now, config=config, clock=clock), timers=clock
            )
            continue

        state.record_attempt(now)
        state.running = True
        try:
            await run_job("dump", now=now, config=config)
            state.record_success(now)
            await run_due_local_dump_restore(now, config=config, clock=clock)
        except Exception as exc:
            state.record_error(str(exc))
            telemetry.emit(
                "telemetry",
                "backup_dump_failed",
                level="error",
                attributes={"detail": str(exc)[:_FAILURE_DETAIL_CHARS]},
            )
            _log.exception("[pg-backup] dump failed; next attempt at the next backup hour")
        finally:
            state.running = False
        await sleep_in_chunks(
            seconds_until_next_backup_hour(clock.now(), config=config, clock=clock), timers=clock
        )


async def run(*, config: ConfigBoot, image: LoadedCommit, runtime: Path, clock: Clock) -> None:
    """Own the pidfile and health server for the backup scheduler."""
    if _is_running(config, runtime=runtime):
        _log.info(
            "[pg-backup] daemon already running (pidfile=%s), exiting",
            _pidfile(config, runtime=runtime),
        )
        sys.exit(1)

    write_pidfile(config, runtime=runtime)
    state = BackupState(started_at=clock.monotonic())
    endpoint = _endpoint(config, runtime=runtime)
    health = await start_health_server(
        "pg_backup",
        endpoint.health_port,
        components=lambda: backup_components(state, clock=clock),
        image=image,
    )
    _log.info("[pg-backup] healthz listening on :%s", endpoint.health_port)
    try:
        await backup_loop(state, config=config, clock=clock)
    finally:
        await stop_health_server(health)
        _remove_pidfile(config, runtime=runtime)
        _log.info("[pg-backup] daemon stopped")


def main() -> None:
    """Entry point for the gateway service session."""
    from base.deploy.schema.migrations import assert_schema_current

    config = ConfigBoot()
    config.boot()
    machine = machine_from_boot(config)
    process_home = ava_home().resolve()
    image = LoadedCommit.capture()
    assert_schema_current(config.view.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="pg_backup")

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(
                machine.host, gateway_url_reader=lambda: config.view.gateway.gateway_url
            ),
        )

    pipeline = build_pipeline(database=database, machine=machine, home=process_home)
    init_gateway_process(
        name="pg_backup",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
    )
    install_graceful_shutdown("pg_backup")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — and a stop signal must never wait on those (see `_hard_exit`).
    # The runner is therefore never closed: after the explicit drain below,
    # teardown is skipped by the hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(
            run(
                config=config,
                image=image,
                runtime=process_home / "run",
                clock=Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
            )
        )
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[pg-backup] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: `run`'s finally still stops the health server and
        # removes the pidfile, and an in-flight dump job is cancelled and reaped
        # with its own cleanup. The executor is deliberately NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[pg-backup] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[pg-backup] daemon crashed — uncaught exception escaped run()")
        code = 1
    finally:
        _remove_pidfile(config, runtime=process_home / "run")
    _hard_exit(code)


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
