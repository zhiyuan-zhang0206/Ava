"""Unit tests for services/computer/session.py — screen ownership.

Covers the holder/lease/FIFO-queue state machine: implicit acquisition,
same-agent pass-through, queued wait + takeover, queue timeout, lease
expiry, lease renewal, explicit release, and the operator kick. Times are
kept short (millisecond-scale leases) so the async tests stay fast.
"""

from __future__ import annotations

import asyncio

import pytest

from services.computer.session import ScreenSession


async def test_idle_screen_implicitly_acquired() -> None:
    s = ScreenSession()
    assert await s.acquire(7) is True
    assert s.holder == 7


async def test_holder_pass_through() -> None:
    s = ScreenSession()
    await s.acquire(7)
    assert await s.acquire(7) is True
    assert s.holder == 7


async def test_waiter_takes_over_after_release() -> None:
    s = ScreenSession()
    await s.acquire(7)

    async def waiter() -> bool:
        return await s.acquire(8)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)  # let 8 queue up
    assert s.holder == 7
    assert await s.release(7) == 7
    assert await task is True
    assert s.holder == 8


async def test_queue_timeout_fails_with_busy() -> None:
    s = ScreenSession(queue_timeout_s=0.05)
    await s.acquire(7)
    assert await s.acquire(8) is False
    assert s.holder == 7


async def test_fifo_order() -> None:
    s = ScreenSession()
    await s.acquire(7)
    order: list[int] = []

    async def waiter(aid: int) -> None:
        assert await s.acquire(aid) is True
        order.append(aid)
        await s.release(aid)

    t8 = asyncio.create_task(waiter(8))
    t9 = asyncio.create_task(waiter(9))
    await asyncio.sleep(0.05)  # both queue: [8, 9]
    await s.release(7)
    await asyncio.gather(t8, t9)
    assert order == [8, 9]


async def test_lease_expiry_frees_the_screen() -> None:
    s = ScreenSession(lease_s=0.05)
    await s.acquire(7)
    assert s.holder == 7
    await asyncio.sleep(0.1)  # lease expires
    assert await s.acquire(8) is True
    assert s.holder == 8


async def test_touch_renews_lease() -> None:
    s = ScreenSession(lease_s=0.2, queue_timeout_s=0.05)
    await s.acquire(7)
    await asyncio.sleep(0.06)
    await s.touch(7)
    await asyncio.sleep(0.06)
    # lease renewed at 0.06 -> expires at ~0.26; the waiter times out at ~0.17
    assert await s.acquire(8) is False
    assert s.holder == 7


async def test_release_by_non_holder_is_noop() -> None:
    s = ScreenSession()
    await s.acquire(7)
    assert await s.release(8) is None
    assert s.holder == 7


async def test_operator_kick_releases_any_holder() -> None:
    s = ScreenSession()
    await s.acquire(7)
    assert await s.release(None) == 7
    assert s.holder is None
    # second kick on a free screen is a no-op
    assert await s.release(None) is None


async def test_expired_holder_is_replaced_by_first_waiter() -> None:
    s = ScreenSession(lease_s=0.05)
    await s.acquire(7)
    t8 = asyncio.create_task(s.acquire(8))
    await asyncio.sleep(0.1)  # lease expires while 8 waits
    assert await t8 is True
    assert s.holder == 8


async def test_cancelled_waiter_cleans_its_queue_slot() -> None:
    s = ScreenSession()
    await s.acquire(7)
    t8 = asyncio.create_task(s.acquire(8))
    await asyncio.sleep(0.05)
    t8.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t8
    # the queue is empty again — a fresh waiter gets the screen after release
    t9 = asyncio.create_task(s.acquire(9))
    await asyncio.sleep(0.05)
    await s.release(7)
    assert await t9 is True
    assert s.holder == 9


async def test_high_priority_jumps_normal_waiters() -> None:
    s = ScreenSession()
    await s.acquire(7)
    order: list[int] = []

    async def waiter(aid: int, priority: str = "normal") -> None:
        assert await s.acquire(aid, priority=priority) is True
        order.append(aid)
        await s.release(aid)

    t8 = asyncio.create_task(waiter(8))  # normal, queues first
    t9 = asyncio.create_task(waiter(9, "high"))  # high, jumps ahead
    await asyncio.sleep(0.05)
    await s.release(7)
    await asyncio.gather(t8, t9)
    assert order == [9, 8]


async def test_high_priority_fifo_among_highs() -> None:
    s = ScreenSession()
    await s.acquire(7)
    order: list[int] = []

    async def waiter(aid: int) -> None:
        assert await s.acquire(aid, priority="high") is True
        order.append(aid)
        await s.release(aid)

    t8 = asyncio.create_task(waiter(8))
    t9 = asyncio.create_task(waiter(9))
    await asyncio.sleep(0.05)
    await s.release(7)
    await asyncio.gather(t8, t9)
    assert order == [8, 9]  # high waiters keep FIFO among themselves


async def test_high_waiter_timeout_still_fails() -> None:
    s = ScreenSession(queue_timeout_s=0.05)
    await s.acquire(7)
    assert await s.acquire(8, priority="high") is False
    assert s.holder == 7
