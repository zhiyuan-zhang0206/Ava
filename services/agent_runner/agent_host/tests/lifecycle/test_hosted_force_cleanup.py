"""Force cleanup evidence and cancellation handoff retain their actual owners."""

import asyncio

from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.tests.lifecycle.test_hosted_force_quiescence import (
    _host_wakes_need_no_provider_credentials as _host_wakes_need_no_provider_credentials,
)


async def test_cancel_validation_spanning_task_handoff_never_cancels_new_turn() -> None:
    first_entered, first_release = asyncio.Event(), asyncio.Event()
    second_entered, second_release = asyncio.Event(), asyncio.Event()
    validating, validated = asyncio.Event(), asyncio.Event()
    calls = 0

    async def run_turn(agent_id: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            await first_release.wait()
        else:
            second_entered.set()
            await second_release.wait()

    async def validate(agent_id: int, command_id: int) -> bool:
        validating.set()
        await validated.wait()
        return True

    scheduler = TurnScheduler(run_turn)
    scheduler.wake(1)
    await first_entered.wait()
    cancellation = asyncio.create_task(scheduler.cancel_exact_force(1, 7, validate))
    await validating.wait()
    scheduler.wake(1)
    first_release.set()
    await second_entered.wait()
    validated.set()
    try:
        assert not await cancellation
        assert 1 in scheduler.active_agents
        assert calls == 2
    finally:
        second_release.set()
        await scheduler.aclose()
