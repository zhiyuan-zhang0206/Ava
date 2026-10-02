"""The stop window: every watchdog loop skips its pass while the unit is quiesced, so it
borrows no connection from a pool the stop is about to close."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from services.delivery_watchdog import daemon, resurrect_retry, stall_recovery, turn_liveness

_Loop = Callable[[ConnectionPool, LoopProgress], Coroutine[Any, Any, None]]

_LOOPS: dict[str, _Loop] = {
    "scan": daemon._scan_loop,
    "resurrect": lambda pool, progress: resurrect_retry.resurrect_loop(
        pool, progress, 0.01, 10, 60.0
    ),
    "harvest": lambda pool, progress: stall_recovery.stall_recovery_loop(
        pool, progress, 0.01, 60.0
    ),
    "hosted_turn": lambda pool, progress: turn_liveness.hosted_turn_recovery_loop(
        pool, cast("EventBus", MagicMock()), progress, 0.01, 60.0
    ),
}


@pytest.mark.parametrize("quiesced", [True, False])
@pytest.mark.parametrize("name", list(_LOOPS))
async def test_a_quiesced_unit_borrows_no_connection(
    monkeypatch: pytest.MonkeyPatch, name: str, quiesced: bool
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)

    async def short_sleep(_progress: LoopProgress, _total_s: float) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(round_loop, "sleep_with_progress", short_sleep)
    pool = MagicMock()
    task = asyncio.create_task(_LOOPS[name](cast("ConnectionPool", pool), LoopProgress("t", 60.0)))
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(pool.mock_calls) is (not quiesced)
