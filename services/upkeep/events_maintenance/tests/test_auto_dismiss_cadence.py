"""Daily history scans retain their error timing and belong to individual loops."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.daemon.loop_health import LoopProgress
from services.upkeep.events_maintenance import daemon, resolution
from services.upkeep.events_maintenance.tests.slices import events_maintenance_config
from services.upkeep.events_maintenance.tests.test_events_maintenance_resolution import (
    _capture_events,
    _event_class,
    _Pool,
    _record,
)


class _ScanConnection:
    """Count actual history SQL while allowing a failed fetch without a DB rollback."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self.connection = connection
        self.scans = 0
        self.fail_fetch = False

    def execute(self, query: LiteralString, *args: Any) -> Any:
        if "HAVING count(DISTINCT" in query:
            self.scans += 1
            if self.fail_fetch:
                raise RuntimeError("history fetch unavailable")
        return self.connection.execute(query, *args)

    def cursor(self, **kwargs: Any) -> Any:
        return cast(Any, self.connection).cursor(**kwargs)

    def commit(self) -> None:
        self.connection.commit()


def _history(connection: psycopg.Connection[Any]) -> datetime:
    connection.execute("TRUNCATE event_dismissals")
    connection.commit()
    for slot in range(4):
        _record(connection, event_name="steady", minutes_ago=slot * 360 + 1)
    return datetime.now(UTC)


def test_fetch_failure_retries_but_write_failure_consumes_the_day(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _history(db_conn)
    config = events_maintenance_config(events_auto_dismiss_enabled=True, events_auto_dismiss_days=1)
    cadence = resolution.AutoDismissCadence()
    connection = _ScanConnection(db_conn)
    pool = cast(ConnectionPool, _Pool(cast(Any, connection)))
    _capture_events(monkeypatch)
    connection.fail_fetch = True
    assert resolution.run_resolution_slice(pool, config, cadence=cadence, now=now) is None
    assert cadence.due(now)
    connection.fail_fetch = False

    def write_failure(*_args: object) -> bool:
        raise RuntimeError("dismissal write unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(resolution, "_insert_auto_dismissal", write_failure)
        with pytest.raises(RuntimeError, match="dismissal write unavailable"):
            resolution.run_resolution_slice(pool, config, cadence=cadence, now=now)
    assert not cadence.due(now)
    assert resolution.run_resolution_slice(pool, config, cadence=cadence, now=now) is not None
    assert connection.scans == 2
    assert db_conn.execute("SELECT count(*) FROM event_dismissals").fetchone() == (0,)
    tomorrow = now + timedelta(days=1)
    resolution._stable_auto_classes(tomorrow, connection, {}, config, cadence)
    assert connection.scans == 3
    assert not cadence.due(tomorrow)


async def test_each_resolution_loop_owns_a_fresh_daily_scan(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(db_conn)
    connection = _ScanConnection(db_conn)
    pool = cast(ConnectionPool, _Pool(cast(Any, connection)))
    config = events_maintenance_config(events_auto_dismiss_enabled=True, events_auto_dismiss_days=1)
    _capture_events(monkeypatch)
    monkeypatch.setattr(daemon.admission, "quiesced", lambda: False)
    passes = 0

    async def execute_pass(
        target_pool: ConnectionPool, _progress: LoopProgress, run: Any, *, tasks: asyncio.TaskGroup
    ) -> None:
        nonlocal passes
        run(target_pool)
        passes += 1

    async def stop_after_two(_progress: LoopProgress, _seconds: float) -> None:
        if passes % 2 == 0:
            raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "_maintenance_with_liveness", execute_pass)
    monkeypatch.setattr(daemon, "_sleep_with_liveness", stop_after_two)
    for expected_scans in (1, 2):
        async with asyncio.TaskGroup() as tasks:
            with pytest.raises(asyncio.CancelledError):
                await daemon._resolution_loop(
                    pool, LoopProgress("test", timeout_s=5), config, tasks=tasks
                )
        assert connection.scans == expected_scans
    assert passes == 4
    assert db_conn.execute("SELECT count(*) FROM event_dismissals").fetchone() == (1,)


def test_disabled_scan_does_not_consume_cadence(db_conn: psycopg.Connection[Any]) -> None:
    now = _history(db_conn)
    connection = _ScanConnection(db_conn)
    cadence = resolution.AutoDismissCadence()
    assert (
        resolution._stable_auto_classes(
            now, connection, {_event_class(): 1}, events_maintenance_config(), cadence
        )
        == set()
    )
    assert connection.scans == 0
    assert cadence.due(now)
