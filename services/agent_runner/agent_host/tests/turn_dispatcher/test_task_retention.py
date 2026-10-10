"""Scheduler stop keeps real stragglers and the original unknown errors."""

import asyncio

import pytest

from services.agent_runner.agent_host import dispatcher
from services.agent_runner.agent_host.dispatcher import TurnScheduler


async def test_stop_keeps_the_original_unfinished_turn_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.01)
    entered, release = asyncio.Event(), asyncio.Event()

    async def turn(_agent: int) -> None:
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    scheduler = TurnScheduler(turn)
    original = scheduler.wake(7)
    assert original is not None
    try:
        await entered.wait()
        closing = asyncio.create_task(scheduler.aclose())
        done, _ = await asyncio.wait({closing}, timeout=1)
        assert closing in done
        closing.result()
        assert scheduler.task_for(7) is original
        assert scheduler.active_agents == frozenset({7})
        assert not original.done()
        assert scheduler.wake(7) is None
        assert scheduler.task_for(7) is original
    finally:
        release.set()
        await original
        await scheduler.aclose()
    assert scheduler.active_agents == frozenset()


async def test_turn_unknown_error_survives_until_join_without_stopping_other_agents() -> None:
    error = ValueError("original scheduler turn failure")
    release, sibling_entered = asyncio.Event(), asyncio.Event()

    async def turn(agent: int) -> None:
        if agent == 7:
            raise error
        sibling_entered.set()
        await release.wait()

    scheduler = TurnScheduler(turn)
    original, sibling = scheduler.wake(7), scheduler.wake(8)
    assert original is not None and sibling is not None
    try:
        await sibling_entered.wait()
        await original
        assert scheduler.task_for(7) is None
        assert scheduler.task_for(8) is sibling
        assert not sibling.done()
        assert not sibling.cancelling()
        release.set()
        await sibling
        with pytest.raises(ValueError) as joined:
            await scheduler.aclose()
        assert joined.value is error
    finally:
        release.set()
        await sibling
