"""The stop window: the bridge's notice loop skips its poll while the unit is quiesced, so it
borrows no connection from a pool the stop is about to close."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from base.deploy.maintenance import admission
from services.entrypoints.im_bridge import daemon


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_polls_no_notices(
    monkeypatch: pytest.MonkeyPatch, quiesced: bool
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    real_sleep = asyncio.sleep

    async def short_sleep(_seconds: float) -> None:
        await real_sleep(0.01)

    monkeypatch.setattr(daemon.asyncio, "sleep", short_sleep)
    core = MagicMock()
    core.notice_bridge.poll_once = AsyncMock()
    task = asyncio.create_task(daemon._notice_loop(core))
    try:
        await real_sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert core.notice_bridge.poll_once.called is (not quiesced)


@pytest.mark.parametrize("quiesced", [True, False])
async def test_timeline_acceptance_and_dispatch_loop_hold_during_maintenance(
    monkeypatch: pytest.MonkeyPatch, quiesced: bool
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    real_sleep = asyncio.sleep

    async def short_sleep(_seconds: float):
        await real_sleep(0.01)

    monkeypatch.setattr(daemon.asyncio, "sleep", short_sleep)
    core = MagicMock()
    core.poll_timeline_outbound = AsyncMock()
    alerts = MagicMock()
    alerts.poll_once = AsyncMock()
    task = asyncio.create_task(daemon._timeline_outbound_loop(core, alerts))
    try:
        await real_sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert core.poll_timeline_outbound.called is (not quiesced)
    assert alerts.poll_once.called is (not quiesced)
