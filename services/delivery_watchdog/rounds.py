"""The sequential round loop and the bounded per-agent fan-out the watchdog's
recovery loops share.

Each recovery loop runs one round at a time, so an agent can never be in two
attempts at once: single flight needs no registry. Within a round the per-agent
RPCs run under a `TaskGroup` scoped to that round, bounded by a semaphore.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable

import psycopg

from base.daemon.loop_health import LoopProgress

_log = logging.getLogger("services.delivery_watchdog.rounds")

# Liveness slack above one job's deadline: a loop that has completed no round
# step for deadline + this reads as wedged on /healthz.
_LIVENESS_SLACK_S = 60.0
_BEAT_STEP_S = 15.0


def rpc_deadline_s() -> float:
    """Overall deadline for one per-agent job. A job makes at most two cluster
    dispatches (hosted-turn recovery: terminate, then resurrect), each bounded by
    the RPC client's own timeout and retry budget, so the deadline is twice the
    client's worst case: it only cuts a job the client's budgets did not."""
    from ops.cluster_rpc import worst_case_dispatch_seconds

    return 2 * worst_case_dispatch_seconds()


def loop_liveness_timeout_s() -> float:
    """How long a recovery loop may go without completing a round step before
    `/healthz` reads it as wedged: a step is a round boundary or one finished
    per-agent job, so one job's deadline plus slack."""
    return rpc_deadline_s() + _LIVENESS_SLACK_S


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
            _log.warning("[delivery] %s round skipped: database unavailable", name, exc_info=group)
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
    """Run the round's per-agent jobs, at most `concurrency` at a time, and
    return when all are done.

    A job handles its own expected failures and bounds its RPC with
    `rpc_deadline_s()`; an exception it lets escape cancels the round and ends
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
