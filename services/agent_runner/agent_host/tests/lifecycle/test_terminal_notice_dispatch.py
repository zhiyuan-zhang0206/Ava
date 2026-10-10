"""Owned terminal delivery never blocks native dispatch or detaches its worker."""

import asyncio
from unittest.mock import Mock

import pytest

from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.tests.test_agent_host import _host, _PendingScanPool


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("fails", [False, True])
async def test_owned_notice_activity_cannot_block_native_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    held: bool,
    fails: bool,
    model_catalog: ModelCatalog,
) -> None:
    from threading import Event

    from base.agents.impersonation import terminal_notices
    from services.agent_runner.agent_host.dispatcher import (
        InboundWakeDispatcher,
        PendingInboundWake,
    )

    pool = _PendingScanPool([(17, False, False)])
    host = _host(
        pool=pool,
        checkpointer=object(),
        graph=object(),
        catalog=model_catalog,
    )
    expected = [PendingInboundWake(agent_id=17, stale=False, recovery=False)]
    monkeypatch.setattr(
        "services.agent_runner.agent_host.lifecycle.maintenance.pending_wakes",
        Mock(return_value=expected if held else None),
    )
    entered, release, exited = Event(), Event(), Event()

    def notice(*_args: object) -> bool:
        entered.set()
        try:
            assert release.wait(5)
            if fails:
                raise RuntimeError("Notice receipt/transport failed during cancellation")
            return True
        finally:
            exited.set()

    monkeypatch.setattr(terminal_notices, "deliver_pending_notice", notice)
    woken: list[int] = []

    admitted = asyncio.Event()

    async def turn(aid: int) -> None:
        woken.append(aid)
        admitted.set()

    scheduler = TurnScheduler(turn)
    scan = InboundWakeDispatcher(
        EventBus.from_settings(),
        scheduler,
        pending_scan=host.pending_inbound_wakes,
        stale_after_s=180.0,
    )
    try:
        async with asyncio.TaskGroup() as owner:
            activity = owner.create_task(
                terminal_notices.run_notice_delivery(Database.from_settings(), "this-box")
            )
            assert await asyncio.to_thread(entered.wait, 5)
            await asyncio.wait_for(scan.scan_once(), timeout=1)
            await asyncio.wait_for(admitted.wait(), timeout=1)
            assert woken == [17]  # Actual dispatcher admitted work while RPC is blocked.
            activity.cancel()
            await asyncio.sleep(0.01)
            assert not activity.done()  # Cancellation joins the physical worker.
            assert not exited.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(activity, timeout=1)
    finally:
        release.set()
        await scheduler.aclose()
    assert exited.is_set()
