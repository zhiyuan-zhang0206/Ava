"""The stop window: every watchdog loop skips its pass while the unit is quiesced, so it
borrows no connection from a pool the stop is about to close."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from services.wake.delivery_watchdog import daemon, resurrect_retry, stall_recovery, turn_liveness
from tests.fixtures.unit.identity import machine_identity as machine_identity

_Loop = Callable[
    [ConnectionPool, LoopProgress, ProcessDbGate, MachineIdentity], Coroutine[Any, Any, None]
]

_LOOPS: dict[str, _Loop] = {
    "scan": lambda pool, progress, database_gate, _machine_identity: daemon._scan_loop(
        pool, Database.from_settings(gate=database_gate), EventBus.from_settings(), progress
    ),
    "resurrect": lambda pool, progress, database_gate, machine_identity: (
        resurrect_retry.resurrect_loop(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            progress,
            0.01,
            10,
            60.0,
            machine=machine_identity,
        )
    ),
    "harvest": lambda pool, progress, database_gate, machine_identity: (
        stall_recovery.stall_recovery_loop(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            progress,
            0.01,
            60.0,
            machine=machine_identity,
        )
    ),
    "hosted_turn": lambda pool, progress, database_gate, machine_identity: (
        turn_liveness.hosted_turn_recovery_loop(
            pool,
            Database.from_settings(gate=database_gate),
            cast("EventBus", MagicMock()),
            progress,
            0.01,
            60.0,
            machine=machine_identity,
        )
    ),
}


@pytest.mark.parametrize("quiesced", [True, False])
@pytest.mark.parametrize("name", list(_LOOPS))
async def test_a_quiesced_unit_borrows_no_connection(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    quiesced: bool,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)

    async def short_sleep(_progress: LoopProgress, _total_s: float) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(round_loop, "sleep_with_progress", short_sleep)
    pool = MagicMock()
    task = asyncio.create_task(
        _LOOPS[name](
            cast("ConnectionPool", pool), LoopProgress("t", 60.0), database_gate, machine_identity
        )
    )
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(pool.mock_calls) is (not quiesced)
