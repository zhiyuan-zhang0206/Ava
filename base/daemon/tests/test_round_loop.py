"""The shared round loop: sequential rounds, a skipped round on an unreachable
database, and the bounded per-item fan-out."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.deploy.maintenance import admission


def _progress() -> LoopProgress:
    return LoopProgress("test", 60.0)


async def test_a_round_that_raises_ends_the_loop() -> None:
    async def one_round() -> None:
        raise ValueError("unexpected")

    with pytest.raises(ValueError):
        await round_loop.run_rounds("t", _progress(), 0.01, one_round)


async def test_an_unreachable_database_skips_the_round_and_the_loop_goes_on() -> None:
    progress = _progress()
    outcomes = [psycopg.OperationalError("db down"), None, None]
    seen = 0

    async def one_round() -> None:
        nonlocal seen
        seen += 1
        outcome = outcomes[seen - 1]
        if outcome is not None:
            raise outcome

    task = asyncio.create_task(round_loop.run_rounds("t", progress, 0.01, one_round))
    try:
        await asyncio.sleep(0.2)
        assert seen >= 3
        assert progress.snapshot()["last_error"] is not None
        assert progress.snapshot()["last_success_at"] is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rounds_are_strictly_sequential() -> None:
    running = 0
    peak = 0
    done = 0

    async def one_round() -> None:
        nonlocal running, peak, done
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.03)
        running -= 1
        done += 1

    task = asyncio.create_task(round_loop.run_rounds("t", _progress(), 0.0, one_round))
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert done >= 3
    assert peak == 1


async def test_fan_out_bounds_concurrency_and_returns_when_all_are_done() -> None:
    running = 0
    peak = 0
    finished: list[int] = []

    def job(index: int):
        async def run() -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            finished.append(index)

        return run

    await round_loop.fan_out([job(i) for i in range(6)], concurrency=2, progress=_progress())

    assert sorted(finished) == list(range(6))
    assert peak == 2


async def test_fan_out_failure_cancels_the_round() -> None:
    cancelled = asyncio.Event()

    async def failing() -> None:
        await asyncio.sleep(0.01)
        raise RuntimeError("job failed")

    async def parked() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(ExceptionGroup):
        await round_loop.fan_out([failing, parked], concurrency=2, progress=_progress())

    assert cancelled.is_set()


async def test_a_callable_interval_is_read_after_every_round() -> None:
    reads = 0

    def interval() -> float:
        nonlocal reads
        reads += 1
        return 0.001

    async def one_round() -> None:
        return None

    task = asyncio.create_task(round_loop.run_rounds("t", _progress(), interval, one_round))
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert reads >= 2


async def test_an_interval_that_raises_ends_the_loop() -> None:
    def interval() -> float:
        raise RuntimeError("config unreadable")

    async def one_round() -> None:
        return None

    with pytest.raises(RuntimeError):
        await round_loop.run_rounds("t", _progress(), interval, one_round)


async def test_a_quiesced_unit_skips_every_round_and_resumes_when_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quiesced = True
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    rounds = 0

    async def one_round() -> None:
        nonlocal rounds
        rounds += 1

    progress = _progress()
    task = asyncio.create_task(round_loop.run_rounds("t", progress, 0.001, one_round))
    try:
        await asyncio.sleep(0.1)
        assert rounds == 0, "a quiesced unit borrows nothing: the round never ran"
        assert progress.snapshot()["last_success_at"] is None
        quiesced = False
        await asyncio.sleep(0.1)
        assert rounds >= 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
