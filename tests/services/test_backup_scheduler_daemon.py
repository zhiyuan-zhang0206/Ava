"""Behaviour locks for the gateway-owned Postgres backup scheduler."""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import UTC, datetime

import pytest

from services.backup_scheduler import daemon
from shared import daemon_health


def _at(hour: int = 3, minute: int = 0) -> datetime:
    return datetime(2026, 8, 25, hour, minute, tzinfo=UTC)


def _always_due(_now: datetime) -> bool:
    return True


def _find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
    server = await daemon_health.start_health_server(
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
        await daemon_health.stop_health_server(server)


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

    monkeypatch.setattr(daemon, "_cluster_tz", lambda: UTC)
    monkeypatch.setattr(daemon, "_sleep", fake_sleep)

    asyncio.run(daemon._sleep_until_next_backup_hour(_at(hour=2, minute=30)))

    assert slept == [30 * 60]


def test_due_backup_runs_once_then_waits_for_tomorrow(monkeypatch: pytest.MonkeyPatch) -> None:
    state = daemon._BackupState(started_at=0.0)
    ran: list[datetime] = []

    monkeypatch.setattr(daemon, "is_due", _always_due)

    def record_run(now: datetime) -> None:
        ran.append(now)

    monkeypatch.setattr(daemon, "run_backup", record_run)

    async def stop_after_success(_now: datetime) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "_sleep_until_next_backup_hour", stop_after_success)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._backup_loop(state))

    assert len(ran) == 1
    assert state.running is False
    assert state.last_success == ran[0].timestamp()


def test_failed_backup_retries_before_tomorrow(monkeypatch: pytest.MonkeyPatch) -> None:
    state = daemon._BackupState(started_at=0.0)
    sleeps: list[float] = []

    monkeypatch.setattr(daemon, "is_due", _always_due)

    def fail(_now: datetime) -> None:
        raise RuntimeError("temporary failure")

    async def stop_after_retry(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "run_backup", fail)
    monkeypatch.setattr(daemon, "_sleep", stop_after_retry)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daemon._backup_loop(state))

    assert sleeps == [daemon.BACKUP_RETRY_INTERVAL_S]
    assert state.running is False
    assert state.last_error == "temporary failure"
