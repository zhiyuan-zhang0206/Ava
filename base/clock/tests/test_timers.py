"""`Timers`: the system implementation waits for real, `ManualTimers` never does, `Clock` has both."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig, Timers
from base.clock.tests.manual_timers import ManualTimers

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _backoff(timers: Timers, attempts: int) -> None:
    """A component written against `Timers`: it waits 1, 2, 4... seconds between attempts."""
    for attempt in range(attempts):
        timers.sleep(2.0**attempt)


async def _abackoff(timers: Timers, attempts: int) -> float:
    started = timers.monotonic()
    for attempt in range(attempts):
        await timers.asleep(2.0**attempt)
    return timers.monotonic() - started


def test_system_timers_wait_for_real() -> None:
    before = time.monotonic()
    SYSTEM_TIMERS.sleep(0.02)
    assert time.monotonic() - before >= 0.019
    assert SYSTEM_TIMERS.monotonic() >= before


async def test_system_timers_suspend_the_task_without_blocking_the_loop() -> None:
    ticks: list[int] = []

    async def other() -> None:
        ticks.append(1)

    task = asyncio.create_task(other())
    await SYSTEM_TIMERS.asleep(0.01)
    await task
    assert ticks == [1]


def test_the_clock_is_a_timers_that_delegates_to_the_timers_it_was_built_with() -> None:
    manual = ManualTimers(start=10.0, wall_start=_EPOCH)
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=manual, now=manual.now)
    timers: Timers = clock
    assert timers.monotonic() == 10.0
    timers.sleep(5.0)
    assert (manual.sleeps, timers.monotonic()) == ([5.0], 15.0)
    assert clock.now() == _EPOCH + timedelta(seconds=5)  # wall time and elapsed time share a source


async def test_the_clock_awaits_through_its_timers_without_waiting() -> None:
    manual = ManualTimers()
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=manual, now=manual.now)
    real_start = time.monotonic()
    await clock.asleep(3600.0)
    assert manual.sleeps == [3600.0]
    assert time.monotonic() - real_start < 1.0


def test_a_clock_on_the_system_timers_reads_the_real_monotonic_clock() -> None:
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=lambda: _EPOCH)
    first = clock.monotonic()
    clock.sleep(0.0)
    assert clock.monotonic() >= first
    assert clock.now() == _EPOCH  # `now=` replaces only the wall clock


def test_manual_timers_start_where_told_and_only_move_when_moved() -> None:
    timers = ManualTimers(start=100.0)
    assert timers.monotonic() == 100.0
    assert timers.monotonic() == 100.0
    timers.advance(2.5)
    assert timers.monotonic() == 102.5
    assert timers.sleeps == []


def test_manual_sleep_records_the_request_and_moves_the_virtual_clock() -> None:
    timers = ManualTimers()
    real_start = time.monotonic()
    _backoff(timers, 4)
    assert timers.sleeps == [1.0, 2.0, 4.0, 8.0]
    assert timers.monotonic() == 15.0
    assert time.monotonic() - real_start < 1.0  # nothing waited


async def test_manual_asleep_records_moves_and_yields_to_the_loop() -> None:
    timers = ManualTimers()
    ran: list[str] = []

    async def other() -> None:
        ran.append("other")

    task = asyncio.create_task(other())
    elapsed = await _abackoff(timers, 3)
    await task
    assert elapsed == 7.0
    assert timers.sleeps == [1.0, 2.0, 4.0]
    assert ran == ["other"]


def test_a_manual_sleep_cannot_be_negative_like_time_sleep() -> None:
    timers = ManualTimers()
    with pytest.raises(ValueError, match="non-negative"):
        timers.sleep(-1.0)
    assert timers.sleeps == []
    assert timers.monotonic() == 0.0


async def test_a_manual_asleep_treats_a_negative_wait_as_none_like_asyncio_sleep() -> None:
    timers = ManualTimers()
    await timers.asleep(-3.0)
    assert timers.sleeps == [-3.0]
    assert timers.monotonic() == 0.0


def test_time_cannot_be_advanced_backwards() -> None:
    timers = ManualTimers()
    with pytest.raises(ValueError, match="backwards"):
        timers.advance(-0.1)
    assert timers.monotonic() == 0.0


def test_the_wall_clock_moves_with_the_virtual_time_and_feeds_a_clock() -> None:
    timers = ManualTimers(wall_start=datetime(2026, 5, 6, 6, 32, 5, tzinfo=UTC))
    clock = Clock(ClockConfig("Asia/Shanghai", None, False), timers=timers, now=timers.now)
    assert clock.now_timestamp() == "[2026-05-06 14:32:05]"
    timers.advance(60.0)
    timers.sleep(5.0)
    assert clock.now_timestamp() == "[2026-05-06 14:33:10]"
    assert timers.now() == datetime(2026, 5, 6, 6, 33, 10, tzinfo=UTC)


def test_a_manual_wall_clock_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualTimers(wall_start=datetime(2026, 1, 1))  # noqa: DTZ001 -- the naive value is the case


def test_a_test_drives_a_deadline_by_advancing_the_virtual_clock() -> None:
    timers = ManualTimers()
    deadline = timers.monotonic() + 30.0
    assert timers.monotonic() < deadline
    timers.advance(30.0)
    assert timers.monotonic() >= deadline
