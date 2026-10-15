from __future__ import annotations

import asyncio

import pytest

from services.desktop.computer.session import ScreenSession


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
    await asyncio.sleep(0.05)
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
    await asyncio.sleep(0.05)
    await s.release(7)
    await asyncio.gather(t8, t9)
    assert order == [8, 9]


async def test_lease_expiry_frees_the_screen() -> None:
    s = ScreenSession(lease_s=0.05)
    await s.acquire(7)
    assert s.holder == 7
    await asyncio.sleep(0.1)
    assert await s.acquire(8) is True
    assert s.holder == 8


async def test_touch_renews_lease() -> None:
    s = ScreenSession(lease_s=0.2, queue_timeout_s=0.05)
    await s.acquire(7)
    await asyncio.sleep(0.06)
    await s.touch(7)
    await asyncio.sleep(0.06)
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
    assert await s.release(None) is None


async def test_expired_holder_is_replaced_by_first_waiter() -> None:
    s = ScreenSession(lease_s=0.05)
    await s.acquire(7)
    t8 = asyncio.create_task(s.acquire(8))
    await asyncio.sleep(0.1)
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

    t8 = asyncio.create_task(waiter(8))
    t9 = asyncio.create_task(waiter(9, "high"))
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
    assert order == [8, 9]


async def test_high_waiter_timeout_still_fails() -> None:
    s = ScreenSession(queue_timeout_s=0.05)
    await s.acquire(7)
    assert await s.acquire(8, priority="high") is False
    assert s.holder == 7
