"""The sync-request queue: what the API leaves and what the service consumes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from gateway.schedules import session_control
from services.schedule_manager import requests
from services.schedule_manager.manager import ScheduleManager

# The autouse API guard replaces the wait for every test; the two tests of the real
# wait put it back from this reference taken at import.
_REAL_WAIT = session_control.wait_consumed


@pytest.fixture
def pool() -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=2)
    try:
        yield p
    finally:
        p.close()


class _Manager:
    """Stands in for ScheduleManager: records syncs, can hold or intercept."""

    def __init__(self) -> None:
        self.synced: list[int] = []
        self.held = False
        self.on_sync: dict[int, object] = {}

    def sync_one(self, schedule_id: int) -> bool:
        if self.held:
            return False
        self.synced.append(schedule_id)
        hook = self.on_sync.get(schedule_id)
        if callable(hook):
            hook()
        return True


def _queue(conn: psycopg.Connection, *schedule_ids: int) -> None:
    for sid in schedule_ids:
        conn.execute("INSERT INTO schedule_sync_requests (schedule_id) VALUES (%s)", (sid,))
    conn.commit()


def _queued(conn: psycopg.Connection) -> list[int]:
    rows = conn.execute("SELECT schedule_id FROM schedule_sync_requests ORDER BY 1").fetchall()
    return [r[0] for r in rows]


def test_a_queued_request_is_synced_and_removed(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    manager = _Manager()
    _queue(db_conn, 7, 3)

    handled = requests.consume_requests(pool, cast(ScheduleManager, manager))

    assert handled == 2
    assert sorted(manager.synced) == [3, 7]
    assert _queued(db_conn) == []


def test_a_request_for_a_deleted_schedule_is_still_synced(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    """No foreign key: the delete route queues the kill of the orphaned session
    after the schedule row is already gone."""
    manager = _Manager()
    _queue(db_conn, 999_999)  # no such schedule

    requests.consume_requests(pool, cast(ScheduleManager, manager))

    assert manager.synced == [999_999]
    assert _queued(db_conn) == []


def test_a_newer_request_made_during_the_sync_survives(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    """The row is deleted only if it is still the one that was read, so a request
    that lands while the sync runs is not lost."""
    manager = _Manager()
    _queue(db_conn, 5)

    def refresh() -> None:
        db_conn.execute(
            "UPDATE schedule_sync_requests SET requested_at = clock_timestamp() "
            "WHERE schedule_id = 5"
        )
        db_conn.commit()

    manager.on_sync[5] = refresh

    requests.consume_requests(pool, cast(ScheduleManager, manager))

    assert manager.synced == [5]
    assert _queued(db_conn) == [5]  # the next pass syncs it again


def test_requests_stay_queued_while_a_maintenance_hold_is_up(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    manager = _Manager()
    manager.held = True
    _queue(db_conn, 1, 2)

    assert requests.consume_requests(pool, cast(ScheduleManager, manager)) == 0
    assert _queued(db_conn) == [1, 2]

    manager.held = False
    assert requests.consume_requests(pool, cast(ScheduleManager, manager)) == 2
    assert _queued(db_conn) == []


async def test_the_api_side_queues_one_row_and_waits_until_it_is_consumed(
    db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`request_sync` upserts the row and returns once the service has removed it."""
    # the autouse guard neutralizes the wait; this test needs the real one
    monkeypatch.setattr(session_control, "wait_consumed", _REAL_WAIT)
    manager = _Manager()

    async def consumer() -> None:
        while not _queued(db_conn):
            await asyncio.sleep(0.01)
        await asyncio.to_thread(requests.consume_requests, pool, cast(ScheduleManager, manager))

    task = asyncio.create_task(consumer())
    await asyncio.wait_for(session_control.request_sync(pool, 11), timeout=10)
    await task

    assert manager.synced == [11]
    assert _queued(db_conn) == []


async def test_the_api_side_gives_up_waiting_when_no_service_consumes(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A down service must not hang start / stop: the request stays queued and the
    call returns after the bounded wait."""
    monkeypatch.setattr(session_control, "wait_consumed", _REAL_WAIT)
    monkeypatch.setattr(session_control, "CONSUME_WAIT_S", 0.3)

    with caplog.at_level(logging.WARNING, logger=session_control.__name__):
        await session_control.request_sync(pool, 12)

    assert _queued(db_conn) == [12]
    assert "not consumed" in caplog.text


def test_repeated_requests_for_one_schedule_collapse_to_one_row(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    session_control.enqueue_blocking(pool, 4)
    session_control.enqueue_blocking(pool, 4)

    assert _queued(db_conn) == [4]
