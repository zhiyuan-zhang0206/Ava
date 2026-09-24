"""A cancelled dispatcher unwinds even when a pool check swallows the cancel.

A cancelled dispatcher must unwind even when the cancellation lands inside a
psycopg_pool connection check. Up to 3.3.1 the pool absorbed that cancellation
(it returned the connection and retried without re-raising — upstream
psycopg#1345), leaving the task running with an outstanding cancellation that
nothing would ever deliver again; the dispatcher's scan-boundary re-assertion
turned that into an unwind. From psycopg_pool 3.3.2 the pool propagates the
cancellation itself (upstream #1401). This test parks a connection check so the
cancel lands in exactly that window, and requires the cancelled dispatcher to
unwind either way (task #3513).
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool

from services.agent_host.dispatcher import InboundWakeDispatcher, PendingInboundWake
from shared.config import settings


class _IdleScheduler:
    """The scheduler slice the scan loop touches when it finds no candidates."""

    @property
    def restart_required(self) -> bool:
        return False

    @property
    def active_agents(self) -> frozenset[int]:
        return frozenset()

    def wake(self, agent_id: int) -> None:
        raise AssertionError("no pending wake expected in this test")

    def task_for(self, agent_id: int) -> asyncio.Task[None] | None:
        return None

    def reaped_successor(self, agent_id: int) -> asyncio.Task[None] | None:
        return None

    async def cancel_agent(self, agent_id: int) -> bool:
        raise AssertionError("no active turn expected in this test")


@pytest_asyncio.fixture
async def blocked_first_check(
    aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[asyncio.Event]:
    """Park the pool's first connection check so a cancel lands inside it.

    The parked call is cancelled; whether the pool propagates that cancellation
    (psycopg_pool >= 3.3.2) or a boundary swallow would strand the task
    (<= 3.3.1), the dispatcher must still unwind. Later checks pass through to
    the real method so the pool stays usable.
    """
    real = AsyncConnectionPool._check_connection
    in_check = asyncio.Event()
    calls = 0

    async def gated(self: AsyncConnectionPool[Any], conn: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            in_check.set()
            await asyncio.Event().wait()
        await real(self, conn)

    monkeypatch.setattr(AsyncConnectionPool, "_check_connection", gated)
    yield in_check


async def test_cancelled_dispatcher_unwinds_after_a_swallowed_pool_check_cancel(
    aops_pool: AsyncConnectionPool,
    blocked_first_check: asyncio.Event,
) -> None:
    async def pending(stale_after_s: float) -> list[PendingInboundWake]:
        async with aops_pool.connection():
            return []

    dispatcher = InboundWakeDispatcher(
        settings.data_plane.redis_url,
        _IdleScheduler(),
        pending_scan=pending,
        stale_after_s=30,
        scan_interval_s=0.05,
        subscription_read_timeout_s=5,
    )
    task = asyncio.create_task(dispatcher.run())
    try:
        await asyncio.wait_for(blocked_first_check.wait(), 5)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=10)
        if task not in done:
            # The run loop survived its own cancellation. Extra cancels usually
            # land in the redis read and kill the task; fail either way.
            for _ in range(20):
                task.cancel()
                await asyncio.sleep(0.05)
                if task.done():
                    break
            pytest.fail(
                "the cancelled dispatcher kept running; the swallowed cancel was not re-asserted"
            )
        with pytest.raises(asyncio.CancelledError):
            task.result()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, 5)
