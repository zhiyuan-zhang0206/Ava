"""Behaviour locks for the gateway-owned Postgres backup scheduler."""

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
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from unittest.mock import patch

import pytest

from base.config import ConfigBoot, settings
from base.daemon import health
from base.native_process.os_platform import LockTimeoutError
from services.backup.scheduler import daemon


@pytest.fixture(autouse=True)
def config_boot_environment() -> Generator[None]:
    """Restore process delivery from each independent operation's boot."""
    with patch.dict(os.environ):
        yield


def _at(hour: int = 3, minute: int = 0) -> datetime:
    return datetime(2026, 8, 25, hour, minute, tzinfo=UTC)


def _always_due(_now: datetime, **_inputs: object) -> bool:
    return True


def _find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_module_entrypoint_runs_the_scheduler(tmp_path: Path) -> None:
    """The exact ``python -m`` ServiceSpec path must enter ``main``.

    Point schema startup at a deliberate connection refusal: a missing module
    guard would silently exit 0, while a real entrypoint reaches the schema
    assertion and reports its traceback. The scheduler is a gateway process: its
    home ``.env`` is the authority for cluster-pinned keys, so that file carries
    the data-plane URLs instead of the inherited environment.
    """
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


def test_backup_components_hold_a_boot_grace_then_require_a_fresh_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = daemon._BackupState(started_at=100.0)
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 100.0 + daemon.BACKUP_STALE_AFTER_S)
    monkeypatch.setattr(daemon.time, "time", lambda: 1_000.0)

    assert daemon._backup_components(state) == [
        {
            "name": "backup",
            "status": "ok",
            "progress": "idle",
        }
    ]

    monkeypatch.setattr(daemon.time, "monotonic", lambda: 101.0 + daemon.BACKUP_STALE_AFTER_S)
    assert daemon._backup_components(state) == [
        {
            "name": "backup",
            "status": "degraded",
            "detail": "no successful backup within 93601s of start",
            "progress": "idle",
        }
    ]

    state.record_success(_at())
    monkeypatch.setattr(daemon.time, "time", lambda: _at().timestamp() + 10.0)
    assert daemon._backup_components(state) == [
        {
            "name": "backup",
            "status": "ok",
            "last_success": _at().timestamp(),
            "age_s": 10.0,
            "progress": "idle",
        }
    ]
    monkeypatch.setattr(
        daemon.time,
        "time",
        lambda: _at().timestamp() + daemon.BACKUP_STALE_AFTER_S + 1.0,
    )
    assert daemon._backup_components(state)[0]["status"] == "degraded"


def test_backup_components_keep_running_dump_healthy_after_previous_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = daemon._BackupState(started_at=0.0, running=True)
    state.record_attempt(_at())
    state.record_error("disk full")
    monkeypatch.setattr(daemon.time, "time", lambda: _at().timestamp() + 1.0)

    assert daemon._backup_components(state) == [
        {
            "name": "backup",
            "status": "ok",
            "last_error": "disk full",
            "progress": "running 1s",
        }
    ]


@pytest.mark.asyncio
async def test_healthz_returns_503_for_an_overdue_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    state = daemon._BackupState(started_at=0.0)
    monkeypatch.setattr(daemon.time, "monotonic", lambda: daemon.BACKUP_STALE_AFTER_S + 1.0)
    port = _find_free_port()
    server = await health.start_health_server(
        "pg_backup",
        port=port,
        components=lambda: daemon._backup_components(state),
    )
    try:
        status, body = await _http_get(port)
        assert status == 503
        assert json.loads(body)["degraded_reasons"] == [
            "backup: no successful backup within 93601s of start"
        ]
    finally:
        await health.stop_health_server(server)


def test_sleep_breaks_long_waits_into_shutdown_responsive_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(daemon.asyncio, "sleep", fake_sleep)

    asyncio.run(daemon._sleep(125.0))

    assert slept == [60.0, 60.0, 5.0]


def test_next_backup_hour_uses_the_cluster_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    def utc_zone(**_inputs: object) -> tzinfo:
        return UTC

    monkeypatch.setattr(daemon, "_cluster_tz", utc_zone)
    monkeypatch.setattr(daemon, "_sleep", fake_sleep)

    backup_hour = settings.services.backup_hour
    asyncio.run(
        daemon._sleep_until_next_backup_hour(
            _at(hour=(backup_hour - 1) % 24, minute=30), config=ConfigBoot()
        )
    )

    assert slept == [30 * 60]


def test_due_backup_runs_once_then_waits_for_tomorrow(monkeypatch: pytest.MonkeyPatch) -> None:
    state = daemon._BackupState(started_at=0.0)
    ran: list[datetime] = []

    monkeypatch.setattr(daemon, "is_due", _always_due)

    async def record_run(kind: str, *, now: datetime, config: ConfigBoot) -> None:
        assert kind == "dump"
        ran.append(now)

    monkeypatch.setattr(daemon, "run_job", record_run)

    async def stop_after_success(_now: datetime, *, config: ConfigBoot) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "_sleep_until_next_backup_hour", stop_after_success)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._backup_loop(state, config=ConfigBoot()))

    assert len(ran) == 1
    assert state.running is False
    assert state.last_success == ran[0].timestamp()


def test_due_backup_sleeps_from_completion_time(monkeypatch: pytest.MonkeyPatch) -> None:
    started = _at(hour=settings.services.backup_hour)
    completed = started + timedelta(minutes=53, seconds=52)
    times = iter((started, completed))
    sleeps: list[float] = []

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            assert tz is UTC
            return next(times)

    async def run_dump(kind: str, *, now: datetime, config: ConfigBoot) -> None:
        assert kind == "dump"
        assert now == started

    async def skip_restore(_now: datetime, *, config: ConfigBoot) -> None:
        pass

    async def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "datetime", FrozenDatetime)

    def utc_zone(**_inputs: object) -> tzinfo:
        return UTC

    monkeypatch.setattr(daemon, "_cluster_tz", utc_zone)
    monkeypatch.setattr(daemon, "is_due", _always_due)
    monkeypatch.setattr(daemon, "run_job", run_dump)
    monkeypatch.setattr(daemon, "_run_due_local_dump_restore", skip_restore)
    monkeypatch.setattr(daemon, "_sleep", stop_after_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._backup_loop(daemon._BackupState(), config=ConfigBoot()))

    assert sleeps == [24 * 3600 - 53 * 60 - 52]


def test_failed_backup_retries_before_tomorrow(monkeypatch: pytest.MonkeyPatch) -> None:
    state = daemon._BackupState(started_at=0.0)
    sleeps: list[float] = []

    monkeypatch.setattr(daemon, "is_due", _always_due)

    async def fail(_kind: str, *, now: datetime, config: ConfigBoot) -> None:
        raise RuntimeError("temporary failure")

    async def stop_after_retry(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "run_job", fail)
    monkeypatch.setattr(daemon, "_sleep", stop_after_retry)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._backup_loop(state, config=ConfigBoot()))

    assert sleeps == [daemon.BACKUP_RETRY_INTERVAL_S]
    assert state.running is False
    assert state.last_error == "temporary failure"


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

    await daemon._run_due_local_dump_restore(now, config=ConfigBoot())

    assert calls == ["restore", now.isoformat()]


@pytest.mark.asyncio
async def test_due_local_restore_drill_reports_failure_without_publishing_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 30, 3, tzinfo=UTC)
    emitted: list[tuple[object, ...]] = []

    monkeypatch.setattr(daemon, "load_local_dump_restore_success", lambda: None)

    def due(_now: datetime, *, last_success: datetime | None, **_inputs: object) -> bool:
        return True

    async def fail_restore(_kind: str, *, config: ConfigBoot) -> None:
        raise RuntimeError("scratch restore failed")

    def unexpected_success(_now: datetime) -> None:
        pytest.fail("failed restore must not publish success")

    def record_emit(category: str, event_name: str, **kwargs: object) -> None:
        emitted.append((category, event_name, kwargs))

    monkeypatch.setattr(daemon, "local_dump_restore_due", due)
    monkeypatch.setattr(daemon, "run_job", fail_restore)
    monkeypatch.setattr(daemon, "record_local_dump_restore_success", unexpected_success)
    monkeypatch.setattr(daemon.telemetry, "emit", record_emit)

    await daemon._run_due_local_dump_restore(now, config=ConfigBoot())

    assert emitted == [
        (
            "telemetry",
            "recovery_drill_failed",
            {
                "level": "error",
                "attributes": {"drill": "logical_dump", "detail": "scratch restore failed"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_invalid_local_restore_marker_reports_failure_without_running_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 30, 3, tzinfo=UTC)
    emitted: list[tuple[object, ...]] = []

    def invalid_marker() -> None:
        raise RuntimeError("logical restore drill success marker is invalid")

    def record_emit(category: str, event_name: str, **kwargs: object) -> None:
        emitted.append((category, event_name, kwargs))

    monkeypatch.setattr(daemon, "load_local_dump_restore_success", invalid_marker)
    monkeypatch.setattr(
        daemon,
        "run_job",
        lambda: pytest.fail("invalid marker must not run the restore"),
    )
    monkeypatch.setattr(daemon.telemetry, "emit", record_emit)

    await daemon._run_due_local_dump_restore(now, config=ConfigBoot())

    assert emitted == [
        (
            "telemetry",
            "recovery_drill_failed",
            {
                "level": "error",
                "attributes": {
                    "drill": "logical_dump",
                    "detail": "logical restore drill success marker is invalid",
                },
            },
        )
    ]


def _staged_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    from services.backup import dump as backup

    staged = tmp_path / "controls" / "ava-20260926T000000Z.dump.enc"
    staged.parent.mkdir()
    staged.write_bytes(b"encrypted")
    published = tmp_path / "db"
    monkeypatch.setattr(backup, "backup_dir", lambda: published)

    def keep_all(_directory: Path, **_inputs: object) -> list[Path]:
        return []

    monkeypatch.setattr(backup, "_prune", keep_all)
    return staged, published, hashlib.sha256(b"encrypted").hexdigest()


def test_cross_filesystem_commit_publishes_a_verified_private_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup directory on another mount gets the same exclusive publication."""
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
    with pytest.raises(FileExistsError):  # a prior artifact is never replaced
        worker.commit_scheduled_backup(staged, digest, keep_reader=lambda: 7)
    assert staged.exists() and [path.name for path in published.iterdir()] == [target.name]


def test_commit_defers_pruning_while_another_backup_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A weekly base capture can hold the lock for hours; the commit never waits."""
    from services.backup import dump as backup
    from services.backup.scheduler import worker

    staged, published, digest = _staged_dump(tmp_path, monkeypatch)
    pruned: list[Path] = []

    def prune(directory: Path, **_inputs: object) -> list[Path]:
        pruned.append(directory)
        return []

    monkeypatch.setattr(backup, "_prune", prune)

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
    """The private copy stays open until it is linked: a backup run sweeping the
    directory at that moment never takes it, and nothing is left behind."""
    from services.backup.artifact.intermediates import sweep_closed_partials
    from services.backup.scheduler import worker

    staged, published, digest = _staged_dump(tmp_path, monkeypatch)
    link = os.link

    def cross_device_with_sweep(source: Path, target: Path) -> None:
        if Path(source) == staged:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        sweep_closed_partials(published)  # another run holds the backup lock now
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
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(daemon, "_sleep", record_sleep)
    asyncio.run(daemon._sleep_until_next_backup_hour(_at(hour=6, minute=30), config=config))
    config.view.services.backup_hour = 8
    asyncio.run(daemon._sleep_until_next_backup_hour(_at(hour=6, minute=30), config=config))
    assert slept == [1800, 5400]
