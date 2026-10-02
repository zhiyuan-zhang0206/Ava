"""The sequential round loop and the bounded per-item fan-out the resident service
loops share (delivery watchdog, TTL reaper, schedule manager, ...).

Each loop runs one round at a time, so an item can never be in two attempts at
once: single flight needs no registry. Within a round the per-item work runs under
a `TaskGroup` scoped to that round, bounded by a semaphore.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable

import psycopg

from base.daemon.loop_health import LoopProgress

_log = logging.getLogger("base.daemon.round_loop")

_BEAT_STEP_S = 15.0


async def sleep_with_progress(progress: LoopProgress, total_s: float) -> None:
    """Sleep `total_s`, beating `progress` so a quiet wait is not read as a wedge."""
    remaining = total_s
    while remaining > 0:
        progress.beat()
        step = min(_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def run_rounds(
    name: str,
    progress: LoopProgress,
    interval_s: float,
    one_round: Callable[[], Awaitable[None]],
) -> None:
    """Run `one_round` forever, `interval_s` apart.

    An unreachable database skips the round (the next one retries). Any other
    exception ends the loop and, through the owning `TaskGroup`, the process:
    the supervisor restarts it.
    """
    while True:
        try:
            await one_round()
        except* psycopg.OperationalError as group:
            progress.mark_error(str(group.exceptions[0]))
            _log.warning("[%s] round skipped: database unavailable", name, exc_info=group)
        else:
            progress.beat()
            progress.mark_success()
        await sleep_with_progress(progress, interval_s)


async def fan_out(
    jobs: Iterable[Callable[[], Awaitable[None]]],
    *,
    concurrency: int,
    progress: LoopProgress,
) -> None:
    """Run the round's per-item jobs, at most `concurrency` at a time, and
    return when all are done.

    A job handles its own expected failures and bounds its own remote calls; an exception it lets escape cancels the round and ends
    the loop.
    """
    gate = asyncio.Semaphore(concurrency)

    async def run_one(job: Callable[[], Awaitable[None]]) -> None:
        async with gate:
            await job()
        progress.beat()

    async with asyncio.TaskGroup() as group:
        for job in jobs:
            group.create_task(run_one(job))
