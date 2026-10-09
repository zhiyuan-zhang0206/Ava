"""Turn teardown retains its original tasks and resource evidence through cancellation."""

import asyncio
from pathlib import Path

from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host.settlement import wait_retained_resources, wait_shielded_task


async def test_repeated_cancellation_cannot_release_owned_settlement() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def settle() -> None:
        entered.set()
        await release.wait()

    owned = asyncio.create_task(settle())
    custody = asyncio.create_task(wait_shielded_task(owned))
    await entered.wait()
    custody.cancel()
    await asyncio.sleep(0)
    custody.cancel()
    await asyncio.sleep(0)
    assert not owned.cancelled() and not custody.done()
    release.set()
    assert await custody
    assert owned.done() and owned.exception() is None


async def test_cancelled_waiter_keeps_exact_unresolved_scope() -> None:
    resources = HostedTurnResources()
    request, original = Path("request"), object()
    resources.unresolved[request] = original
    custody = asyncio.create_task(wait_retained_resources(resources))
    await asyncio.sleep(0)
    custody.cancel()
    await asyncio.sleep(0)
    assert not resources.complete(request, object())
    assert not custody.done()
    assert resources.unresolved[request] is original
    assert resources.complete(request, original)
    assert await custody
    assert not resources.unresolved
