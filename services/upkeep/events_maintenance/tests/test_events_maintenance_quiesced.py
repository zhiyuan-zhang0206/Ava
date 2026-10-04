"""The stop window: both maintenance loops skip their passes while the unit is quiesced, so
neither borrows a connection from a pool the stop is about to close."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from base.daemon.loop_health import LoopProgress
from base.deploy.maintenance import admission
from services.upkeep.events_maintenance import daemon
from services.upkeep.events_maintenance.config import EventsMaintenanceConfig
from services.upkeep.events_maintenance.daemon import events_maintenance_db
from services.upkeep.events_maintenance.tests.slices import events_maintenance_config

_Loop = Callable[[Any, LoopProgress, EventsMaintenanceConfig], Coroutine[Any, Any, None]]


def _rollup(pool: Any, progress: LoopProgress, config: EventsMaintenanceConfig) -> Any:
    return daemon._dispatch_loop(
        pool,
        progress,
        config,
        events_maintenance_db(),
    )


_LOOPS: dict[str, tuple[_Loop, str]] = {
    "rollup": (_rollup, "_run_maintenance"),
    "resolution": (daemon._resolution_loop, "_run_resolution"),
}


@pytest.mark.parametrize("quiesced", [True, False])
@pytest.mark.parametrize("name", list(_LOOPS))
async def test_a_quiesced_unit_runs_no_pass(
    monkeypatch: pytest.MonkeyPatch, name: str, quiesced: bool
) -> None:
    loop, pass_name = _LOOPS[name]
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    passes: list[object] = []

    def record_pass(*args: object) -> None:
        passes.append(args)

    monkeypatch.setattr(daemon, pass_name, record_pass)

    async def short_sleep(_progress: LoopProgress, _total_s: float) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(daemon, "_sleep_with_liveness", short_sleep)
    task = asyncio.create_task(
        loop(object(), LoopProgress("t", timeout_s=5.0), events_maintenance_config())
    )
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(passes) is (not quiesced)
