from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import socket
import subprocess
import sys
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from base.clock import Clock, ClockConfig
from base.clock.configuration import clock_config_from_boot
from base.clock.tests.manual_timers import ManualTimers
from base.config import ConfigBoot, settings
from base.daemon import health, health_schema
from base.daemon.tests.health_support import unknown_image
from base.native_process.os_platform import LockTimeoutError
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.backup.scheduler import daemon


@pytest.fixture(autouse=True)
def config_boot_environment() -> Generator[None]:
    with patch.dict(os.environ):
        yield


def _at(hour: int = 3, minute: int = 0) -> datetime:
    return datetime(2026, 8, 25, hour, minute, tzinfo=UTC)


def _always_due(_now: datetime, **_inputs: object) -> bool:
    return True


def _clock(timers: ManualTimers) -> Clock:
    return Clock(ClockConfig("UTC", "UTC", False), timers=timers, now=timers.now)


class _StopAfterSleeping(ManualTimers):
    def __init__(self, budget: float, *, wall_start: datetime) -> None:
        super().__init__(wall_start=wall_start)
        self._budget = budget

    async def asleep(self, seconds: float) -> None:
        await super().asleep(seconds)
        if sum(self.sleeps) >= self._budget:
            raise asyncio.CancelledError


def _components(state: daemon.BackupState, timers: ManualTimers) -> list[dict[str, object]]:
    return daemon.backup_components(state, clock=_clock(timers))


def _find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_module_entrypoint_runs_the_scheduler(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "AVA_DB_URL=postgresql://ava:test@127.0.0.1:1/ava\n"
        "AVA_REDIS_URL=redis://ava:test@127.0.0.1:1/0\n"
    )
    env = dict(os.environ)
    env.update(
        {
            "AVA_HOME": str(tmp_path),
            "AVA_CONFIG_FETCH": "skip",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "services.backup.scheduler.daemon"],
        cwd=Path(__file__).parents[4],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "Traceback" in result.stderr
    assert "assert_schema_current" in result.stderr, result.stderr


async def _http_get(port: int) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    headers, body = response.split(b"\r\n\r\n", 1)
    return int(headers.split()[1]), body


def test_backup_components_hold_a_boot_grace_then_require_a_fresh_success() -> None:
    timers = ManualTimers(start=100.0, wall_start=_at())
    state = daemon.BackupState(started_at=100.0)
    timers.advance(daemon.BACKUP_STALE_AFTER_S)

    assert _components(state, timers) == [
        {
            "name": "backup",
            "status": "ok",
            "progress": "idle",
        }
    ]

    timers.advance(1.0)
    assert _components(state, timers) == [
        {
            "name": "backup",
            "status": "degraded",
            "detail": "no successful backup within 93601s of start",
            "progress": "idle",
        }
    ]

    succeeded_at = timers.now()
    state.record_success(succeeded_at)
    timers.advance(10.0)
    assert _components(state, timers) == [
        {
            "name": "backup",
            "status": "ok",
            "last_success": succeeded_at.timestamp(),
            "age_s": 10.0,
            "progress": "idle",
        }
    ]
    timers.advance(daemon.BACKUP_STALE_AFTER_S - 9.0)
    assert _components(state, timers)[0]["status"] == "degraded"


def test_backup_components_keep_running_dump_healthy_after_previous_failure() -> None:
    timers = ManualTimers(wall_start=_at())
    state = daemon.BackupState(started_at=0.0, running=True)
    state.record_attempt(_at())
    state.record_error("disk full")
    timers.advance(1.0)

    assert _components(state, timers) == [
        {
            "name": "backup",
            "status": "ok",
            "last_error": "disk full",
            "progress": "running 1s",
        }
    ]


@pytest.mark.asyncio
async def test_healthz_returns_503_for_an_overdue_backup() -> None:
    state = daemon.BackupState(started_at=0.0)
    timers = ManualTimers()
    timers.advance(daemon.BACKUP_STALE_AFTER_S + 1.0)
    port = _find_free_port()
    server = await health.start_health_server(
        "pg_backup",
        port=port,
        components=lambda: _components(state, timers),
        image=unknown_image(),
    )
    try:
        status, body = await _http_get(port)
        assert status == 503
        assert json.loads(body)["degraded_reasons"] == [
            "backup: no successful backup within 93601s of start"
        ]
    finally:
        await health.stop_health_server(server)


def test_next_backup_hour_uses_the_cluster_timezone() -> None:
    backup_hour = settings.services.backup_hour

    seconds = daemon.seconds_until_next_backup_hour(
        _at(hour=(backup_hour - 1) % 24, minute=30),
        config=ConfigBoot(),
        clock=Clock(ClockConfig("UTC", "UTC", False), timers=ManualTimers()),
    )

    assert seconds == 30 * 60


def test_due_backup_runs_once_then_waits_for_tomorrow(monkeypatch: pytest.MonkeyPatch) -> None:
    timers = _StopAfterSleeping(budget=1.0, wall_start=_at())
    state = daemon.BackupState(started_at=timers.monotonic())
    ran: list[datetime] = []

    monkeypatch.setattr(daemon, "is_due", _always_due)

    async def record_run(kind: str, *, now: datetime, config: ConfigBoot) -> None:
        assert kind == "dump"
        ran.append(now)

    monkeypatch.setattr(daemon, "run_job", record_run)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon.backup_loop(state, config=ConfigBoot(), clock=_clock(timers)))

    assert len(ran) == 1
    assert state.running is False
    assert state.last_success == ran[0].timestamp()
    assert timers.sleeps == [24 * 3600.0]


def test_due_backup_sleeps_from_completion_time(monkeypatch: pytest.MonkeyPatch) -> None:
    started = _at(hour=settings.services.backup_hour)
    job_took = timedelta(minutes=53, seconds=52)
    until_tomorrow = 24 * 3600 - 53 * 60 - 52
    timers = _StopAfterSleeping(budget=until_tomorrow, wall_start=started)

    async def run_dump(kind: str, *, now: datetime, config: ConfigBoot) -> None:
        assert kind == "dump"
        assert now == started
        timers.advance(job_took.total_seconds())

    async def skip_restore(_now: datetime, *, config: ConfigBoot, clock: Clock) -> None:
        pass

    monkeypatch.setattr(daemon, "is_due", _always_due)
    monkeypatch.setattr(daemon, "run_job", run_dump)
    monkeypatch.setattr(daemon, "run_due_local_dump_restore", skip_restore)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            daemon.backup_loop(
                daemon.BackupState(started_at=timers.monotonic()),
                config=ConfigBoot(),
                clock=_clock(timers),
            )
        )

    assert sum(timers.sleeps) == until_tomorrow


def test_failed_backup_is_reported_and_not_retried_before_the_next_backup_hour(
    monkeypatch: pytest.MonkeyPatch, emitted_events: EmittedEvents
) -> None:
    config = ConfigBoot()
    probe = ManualTimers(wall_start=_at())
    until_next_hour = daemon.seconds_until_next_backup_hour(
        probe.now(), config=config, clock=_clock(probe)
    )
    timers = _StopAfterSleeping(budget=until_next_hour, wall_start=_at())
    state = daemon.BackupState(started_at=timers.monotonic())
    ran: list[str] = []

    monkeypatch.setattr(daemon, "is_due", _always_due)

    async def fail(kind: str, *, now: datetime, config: ConfigBoot) -> None:
        ran.append(kind)
        raise RuntimeError("temporary failure")

    monkeypatch.setattr(daemon, "run_job", fail)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon.backup_loop(state, config=config, clock=_clock(timers)))

    assert ran == ["dump"]
    assert sum(timers.sleeps) == until_next_hour
    assert state.running is False
    assert state.last_error == "temporary failure"
    [event] = emitted_events
    assert (event.category, event.event_name, event.level) == (
        "telemetry",
        "backup_dump_failed",
        "error",
    )
    assert event.attributes == {"detail": "temporary failure"}


def test_failed_dump_does_not_make_healthz_ask_the_supervisor_for_a_restart() -> None:
    timers = ManualTimers()
    state = daemon.BackupState(started_at=timers.monotonic())
    state.record_error("disk full")

    components = _components(state, timers)

    assert components[0]["status"] == "ok"
    assert components[0]["last_error"] == "disk full"
    assert "last dump failed: disk full" in str(components[0]["detail"])
    status, _body = health_schema.render({"name": "pg_backup"}, components)
    assert status == 200


@pytest.mark.asyncio
async def test_due_local_restore_drill_runs_after_a_successful_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 30, 3, tzinfo=UTC)
    calls: list[str] = []

    monkeypatch.setattr(daemon, "load_local_dump_restore_success", lambda: None)

    def due(current: datetime, *, last_success: datetime | None, **_inputs: object) -> bool:
        return current == now and last_success is None

    def record_success(current: datetime) -> None:
        calls.append(current.isoformat())

    monkeypatch.setattr(
        daemon,
        "local_dump_restore_due",
        due,
    )

    async def restore(kind: str, *, config: ConfigBoot) -> None:
        calls.append(kind)

    monkeypatch.setattr(daemon, "run_job", restore)
    monkeypatch.setattr(daemon, "record_local_dump_restore_success", record_success)

    await daemon.run_due_local_dump_restore(
        now,
        config=ConfigBoot(),
        clock=Clock(ClockConfig("UTC", "UTC", False), timers=ManualTimers()),
    )

    assert calls == ["restore", now.isoformat()]


@pytest.mark.asyncio
async def test_due_local_restore_drill_reports_failure_without_publishing_success(
    monkeypatch: pytest.MonkeyPatch, emitted_events: EmittedEvents
) -> None:
    now = datetime(2026, 8, 30, 3, tzinfo=UTC)

    monkeypatch.setattr(daemon, "load_local_dump_restore_success", lambda: None)

    def due(_now: datetime, *, last_success: datetime | None, **_inputs: object) -> bool:
        return True

    async def fail_restore(_kind: str, *, config: ConfigBoot) -> None:
        raise RuntimeError("scratch restore failed")

    def unexpected_success(_now: datetime) -> None:
        pytest.fail("failed restore must not publish success")

    monkeypatch.setattr(daemon, "local_dump_restore_due", due)
    monkeypatch.setattr(daemon, "run_job", fail_restore)
    monkeypatch.setattr(daemon, "record_local_dump_restore_success", unexpected_success)

    await daemon.run_due_local_dump_restore(
        now,
        config=ConfigBoot(),
        clock=Clock(ClockConfig("UTC", "UTC", False), timers=ManualTimers()),
    )

    [event] = emitted_events
    assert (event.category, event.event_name, event.level) == (
        "telemetry",
        "recovery_drill_failed",
        "error",
    )
    assert event.attributes == {"drill": "logical_dump", "detail": "scratch restore failed"}


@pytest.mark.asyncio
async def test_invalid_local_restore_marker_reports_failure_without_running_restore(
    monkeypatch: pytest.MonkeyPatch, emitted_events: EmittedEvents
) -> None:
    now = datetime(2026, 8, 30, 3, tzinfo=UTC)

    def invalid_marker() -> None:
        raise RuntimeError("logical restore drill success marker is invalid")

    monkeypatch.setattr(daemon, "load_local_dump_restore_success", invalid_marker)
    monkeypatch.setattr(
        daemon,
        "run_job",
        lambda: pytest.fail("invalid marker must not run the restore"),
    )

    await daemon.run_due_local_dump_restore(
        now,
        config=ConfigBoot(),
        clock=Clock(ClockConfig("UTC", "UTC", False), timers=ManualTimers()),
    )

    [event] = emitted_events
    assert (event.category, event.event_name, event.level) == (
        "telemetry",
        "recovery_drill_failed",
        "error",
    )
    assert event.attributes == {
        "drill": "logical_dump",
        "detail": "logical restore drill success marker is invalid",
    }


def _staged_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    from services.backup import dump as backup

    staged = tmp_path / "controls" / "ava-20260926T000000Z.dump.enc"
    staged.parent.mkdir()
    staged.write_bytes(b"encrypted")
    published = tmp_path / "db"
    monkeypatch.setattr(backup, "backup_dir", lambda: published)

    def keep_all(_directory: Path, **_inputs: object) -> list[Path]:
        return []

    monkeypatch.setattr(backup, "prune", keep_all)
    return staged, published, hashlib.sha256(b"encrypted").hexdigest()


def test_cross_filesystem_commit_publishes_a_verified_private_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.backup.scheduler import worker

    staged, published, digest = _staged_dump(tmp_path, monkeypatch)
    link = os.link

    def cross_device(source: Path, target: Path) -> None:
        if Path(source) == staged:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        link(source, target)

    monkeypatch.setattr(worker.os, "link", cross_device)
    target = worker.commit_scheduled_backup(staged, digest, keep_reader=lambda: 7)
    assert target.read_bytes() == b"encrypted" and target.stat().st_mode & 0o777 == 0o600
    assert not staged.exists() and [path.name for path in published.iterdir()] == [target.name]
    staged.write_bytes(b"encrypted")
    with pytest.raises(FileExistsError):
        worker.commit_scheduled_backup(staged, digest, keep_reader=lambda: 7)
    assert staged.exists() and [path.name for path in published.iterdir()] == [target.name]


def test_commit_defers_pruning_while_another_backup_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.backup import dump as backup
    from services.backup.scheduler import worker

    staged, published, digest = _staged_dump(tmp_path, monkeypatch)
    pruned: list[Path] = []

    def prune(directory: Path, **_inputs: object) -> list[Path]:
        pruned.append(directory)
        return []

    monkeypatch.setattr(backup, "prune", prune)

    @contextlib.contextmanager
    def busy(*, timeout_s: float | None = None) -> Generator[None]:
        assert timeout_s == 0
        raise LockTimeoutError("held by a base capture")
        yield

    monkeypatch.setattr(backup, "backup_lock", busy)
    target = worker.commit_scheduled_backup(staged, digest, keep_reader=lambda: 7)
    assert target.parent == published and target.read_bytes() == b"encrypted"
    assert pruned == []


def test_cross_filesystem_copy_survives_a_concurrent_sweep_until_linked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.backup.artifact.intermediates import sweep_closed_partials
    from services.backup.scheduler import worker

    staged, published, digest = _staged_dump(tmp_path, monkeypatch)
    link = os.link

    def cross_device_with_sweep(source: Path, target: Path) -> None:
        if Path(source) == staged:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        sweep_closed_partials(published)
        link(source, target)

    monkeypatch.setattr(worker.os, "link", cross_device_with_sweep)
    target = worker.commit_scheduled_backup(staged, digest, keep_reader=lambda: 7)
    assert target.read_bytes() == b"encrypted"
    assert [path.name for path in published.iterdir()] == [target.name]


def test_next_backup_hour_reads_its_owner_instead_of_ambient_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ConfigBoot()
    config.view.general.timezone = "UTC"
    config.view.services.backup_hour = 7
    monkeypatch.setattr(settings.services, "backup_hour", 3)
    clock = Clock(clock_config_from_boot(config), timers=ManualTimers())
    when = _at(hour=6, minute=30)

    assert daemon.seconds_until_next_backup_hour(when, config=config, clock=clock) == 1800
    config.view.services.backup_hour = 8
    assert daemon.seconds_until_next_backup_hour(when, config=config, clock=clock) == 5400
