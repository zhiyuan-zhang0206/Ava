"""Repeated cancellation cannot extend a cleanup deadline or cancel its child."""

import asyncio

import pytest

from services.agent_runner.agent_host import settlement
from services.agent_runner.agent_host.dispatcher import HostRestartRequiredError


async def test_repeated_cancel_does_not_reset_cleanup_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settlement, "CANCEL_UNWIND_TIMEOUT_S", 0.03)
    child = asyncio.create_task(asyncio.Event().wait())
    waiting = asyncio.create_task(settlement.wait_shielded_task(child))
    try:
        for _ in range(5):
            await asyncio.sleep(0.005)
            waiting.cancel()
        with pytest.raises(HostRestartRequiredError, match="finite deadline"):
            await asyncio.wait_for(waiting, 0.15)
        assert not child.done()
    finally:
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
