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
from base.deploy.maintenance import admission

_log = logging.getLogger("base.daemon.round_loop")

_BEAT_STEP_S = 15.0

# Consecutive rounds of a log-and-continue resident loop that may fail before the
# process exits for its supervisor. Rounds are minutes to an hour apart, so five
# in a row is a persistent fault rather than a blip, and the exit is bounded by
# the supervisor's own restart budget.
CONSECUTIVE_ROUND_FAILURE_LIMIT = 5


class RoundFailureLimitError(RuntimeError):
    """A resident loop failed too many rounds in a row; the process must exit."""


class RoundFailureBudget:
    """Consecutive-failure budget for a loop that logs a failed round and carries on.

    `run_rounds` ends the loop on the first unexpected exception. A loop whose own
    structure keeps it out of `run_rounds` spends this budget instead: `succeeded()`
    after each completed round clears the count, `failed(exc)` after an unexpected
    exception counts it and, at the limit, raises `RoundFailureLimitError` chained
    to the last exception. Raised inside the owning `TaskGroup`, that ends the
    process like any other loop exit. An unreachable database is not an unexpected
    failure (see `run_rounds`): `failed` does not count it.
    """

    def __init__(self, name: str, limit: int = CONSECUTIVE_ROUND_FAILURE_LIMIT) -> None:
        self._name = name
        self._limit = limit
        self._failures = 0

    def succeeded(self) -> None:
        self._failures = 0

    def failed(self, error: BaseException) -> None:
        if isinstance(error, psycopg.OperationalError):
            return
        self._failures += 1
        if self._failures < self._limit:
            return
        _log.critical(
            "[%s] %d consecutive rounds failed — exiting for supervisor recovery",
            self._name,
            self._failures,
            exc_info=error,
        )
        raise RoundFailureLimitError(
            f"{self._name}: {self._failures} consecutive rounds failed"
        ) from error


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
    interval_s: float | Callable[[], float],
    one_round: Callable[[], Awaitable[None]],
) -> None:
    """Run `one_round` forever, `interval_s` apart. A callable `interval_s` is
    read after each round, for a cadence the operator can change live.

    A unit inside its stop window (`admission.quiesced()`) skips the round: every
    round borrows the pool, and the window exists so a held unit stops doing
    database work. Rounds resume on their own when `ava start` releases the hold.

    An unreachable database skips the round (the next one retries). Any other
    exception ends the loop and, through the owning `TaskGroup`, the process:
    the supervisor restarts it.
    """
    while True:
        if not admission.quiesced():
            try:
                await one_round()
            except* psycopg.OperationalError as group:
                progress.mark_error(str(group.exceptions[0]))
                _log.warning("[%s] round skipped: database unavailable", name, exc_info=group)
            else:
                progress.beat()
                progress.mark_success()
        await sleep_with_progress(progress, interval_s() if callable(interval_s) else interval_s)


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
