"""Gateway-owned scheduler for the daily local Postgres dump.

The dump runs in this supervised process so its one-hour subprocess deadline
cannot delay a watchdog round. Its health component reports the age of the
last successful backup; the watchdog therefore only probes and restarts this
daemon when its schedule has stopped making progress.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time

from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.backup import BACKUP_HOUR, _cluster_tz, is_due, run_backup
from services.backup_scheduler.recovery_drill import (
    load_local_dump_restore_success,
    local_dump_restore_due,
    record_local_dump_restore_success,
)
from shared import telemetry
from shared.config import settings
from shared.daemon_health import health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.health_schema import DEGRADED, OK, component
from shared.log import init_gateway_process
from shared.paths import legacy_pid_path

_log = logging.getLogger("services.backup_scheduler.daemon")

BACKUP_RETRY_INTERVAL_S = 1800
BACKUP_STALE_AFTER_S = 26 * 3600
_SLEEP_CHUNK_S = 60
_PIDFILE = settings.services.pg_backup_pidfile


@dataclass
class _BackupState:
    """Scheduler state, owned by its event-loop thread and read by `/healthz`."""

    started_at: float = field(default_factory=time.monotonic)
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


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.backup_scheduler.daemon"):
        _log.info("[pg-backup] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether this daemon already owns its new or legacy pidfile."""
    return pidfile_holds_daemon(
        _PIDFILE, "services.backup_scheduler.daemon"
    ) or pidfile_holds_daemon(legacy_pid_path("pg_backup"), "services.backup_scheduler.daemon")


def _backup_components(state: _BackupState) -> list[dict[str, object]]:
    """Report scheduler progress; a running dump is healthy until it returns.

    `run_backup` has its own one-hour bound. A just-booted cluster gets one
    schedule-plus-slack interval before it is judged overdue, preserving the
    first-run catch-up behaviour without declaring a new service unhealthy.
    """
    if state.running:
        status = OK
        started_at = state.last_attempt if state.last_attempt is not None else time.time()
        progress = f"running {time.time() - started_at:.0f}s"
        detail = None
    elif state.last_error is not None:
        status = DEGRADED
        progress = "idle"
        detail = state.detail
    elif state.last_success is not None:
        age_s = time.time() - state.last_success
        status = OK if age_s <= BACKUP_STALE_AFTER_S else DEGRADED
        progress = "idle"
        detail = None if status == OK else f"last successful backup {age_s:.0f}s ago"
    else:
        age_s = time.monotonic() - state.started_at
        status = OK if age_s <= BACKUP_STALE_AFTER_S else DEGRADED
        progress = "idle"
        detail = None if status == OK else f"no successful backup within {age_s:.0f}s of start"

    record = component(
        "backup",
        status,
        last_success=state.last_success,
        progress=progress,
        detail=detail,
        now=time.time() if state.last_success is not None else None,
    )
    if state.last_error is not None:
        record["last_error"] = state.last_error
    return [record]


async def _sleep(seconds: float) -> None:
    """Sleep in bounded chunks so SIGTERM reaches the scheduler promptly."""
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, _SLEEP_CHUNK_S)
        await asyncio.sleep(chunk)
        remaining -= chunk


async def _sleep_until_next_backup_hour(now: datetime) -> None:
    """Sleep until the next configured backup hour on the cluster clock."""
    local_now = now.astimezone(_cluster_tz())
    next_date = local_now.date()
    target = datetime.combine(next_date, clock_time(hour=BACKUP_HOUR), tzinfo=local_now.tzinfo)
    if local_now >= target:
        target = datetime.combine(
            next_date + timedelta(days=1),
            clock_time(hour=BACKUP_HOUR),
            tzinfo=local_now.tzinfo,
        )
    await _sleep((target.astimezone(UTC) - now).total_seconds())


def run_local_dump_restore() -> None:
    """Restore the newest local dump into an isolated Postgres instance."""
    from scripts.restore_drill import run_drill

    run_drill()


async def _run_due_local_dump_restore(now: datetime) -> None:
    """Run one weekly local restore proof without re-running the daily dump."""
    try:
        if not local_dump_restore_due(now, last_success=load_local_dump_restore_success()):
            return
        await asyncio.to_thread(run_local_dump_restore)
        record_local_dump_restore_success(now)
    except Exception as exc:
        telemetry.emit(
            "telemetry",
            "recovery_drill_failed",
            level="error",
            attributes={"drill": "logical_dump", "detail": str(exc)},
        )
        _log.exception("[pg-backup] local restore drill failed")


async def _backup_loop(state: _BackupState) -> None:
    """Run due dumps and retry a failed dump sooner than the next schedule."""
    _log.info("[pg-backup] scheduler started, pid=%s", os.getpid())
    while True:
        now = datetime.now(UTC)
        if not is_due(now):
            await _sleep_until_next_backup_hour(now)
            continue

        state.record_attempt(now)
        state.running = True
        try:
            await asyncio.to_thread(run_backup, now)
            state.record_success(now)
            await _run_due_local_dump_restore(now)
        except Exception as exc:
            state.record_error(str(exc))
            _log.exception("[pg-backup] dump failed; retrying in %ss", BACKUP_RETRY_INTERVAL_S)
            await _sleep(BACKUP_RETRY_INTERVAL_S)
            continue
        finally:
            state.running = False
        await _sleep_until_next_backup_hour(now)


async def run() -> None:
    """Own the pidfile and health server for the backup scheduler."""
    if _is_running():
        _log.info("[pg-backup] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)

    _write_pidfile()
    state = _BackupState()
    health = await start_health_server(
        "pg_backup",
        components=lambda: _backup_components(state),
    )
    _log.info("[pg-backup] healthz listening on :%s", health_port("pg_backup"))
    try:
        await _backup_loop(state)
    finally:
        await stop_health_server(health)
        _remove_pidfile()
        _log.info("[pg-backup] daemon stopped")


def main() -> None:
    """Entry point for the gateway service session."""
    from shared.migrations import assert_schema_current

    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="pg-backup")
    install_graceful_shutdown("pg-backup")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[pg-backup] interrupted, shutting down")
    except Exception:
        _log.exception("[pg-backup] daemon crashed — uncaught exception escaped run()")
        raise
    finally:
        _remove_pidfile()


if __name__ == "__main__":
    main()
