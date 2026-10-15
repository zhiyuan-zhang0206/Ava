from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.daemon.loop_health import LoopProgress
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from services.agent_runner.page_server import daemon
from services.agent_runner.page_server.tests.slices import page_server_config


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_runs_no_reconcile_pass(
    monkeypatch: pytest.MonkeyPatch, quiesced: bool, *, machine_identity: MachineIdentity
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    passes: list[object] = []

    def record_pass(*args: object) -> None:
        passes.append(args)

    monkeypatch.setattr(daemon, "reconcile_once", record_pass)
    real_sleep = asyncio.sleep

    async def short_sleep(_seconds: float) -> None:
        await real_sleep(0.01)

    monkeypatch.setattr(daemon.asyncio, "sleep", short_sleep)
    pool = MagicMock()
    task = asyncio.create_task(
        daemon.reconcile_loop(
            cast("ConnectionPool", pool),
            LoopProgress("t", 60.0),
            page_server_config(),
            cast("EventBus", MagicMock()),
            local_machine=machine_identity,
        )
    )
    try:
        await real_sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(passes) is (not quiesced)
