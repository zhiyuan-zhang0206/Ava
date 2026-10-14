"""The stop window: both maintenance loops skip their passes while the unit is quiesced, so
neither borrows a connection from a pool the stop is about to close."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any

import pytest

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.daemon.loop_health import LoopProgress
from base.deploy.maintenance import admission
from services.upkeep.events_maintenance import daemon
from services.upkeep.events_maintenance.config import EventsMaintenanceConfig
from services.upkeep.events_maintenance.tests.slices import (
    events_maintenance_config,
    events_maintenance_db,
)
from tests.fixtures.unit.identity import machine_identity as machine_identity

_Loop = Callable[..., Coroutine[Any, Any, None]]


def _rollup(
    pool: Any,
    progress: LoopProgress,
    config: EventsMaintenanceConfig,
    *,
    tasks: asyncio.TaskGroup,
    process_config: ConfigBoot,
    local_machine: MachineIdentity,
) -> Any:
    return daemon.dispatch_loop(
        pool,
        progress,
        config,
        events_maintenance_db(process_config, local_machine=local_machine),
        tasks=tasks,
    )


_LOOPS: dict[str, tuple[_Loop, str]] = {
    "rollup": (_rollup, "run_maintenance"),
    "resolution": (daemon.resolution_loop, "_run_resolution"),
}


@pytest.mark.parametrize("quiesced", [True, False])
@pytest.mark.parametrize("name", list(_LOOPS))
async def test_a_quiesced_unit_runs_no_pass(
    process_config: ConfigBoot,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    quiesced: bool,
    machine_identity: MachineIdentity,
) -> None:
    loop, pass_name = _LOOPS[name]
    if name == "rollup":
        loop = partial(loop, process_config=process_config, local_machine=machine_identity)
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    passes: list[object] = []

    def record_pass(*args: object) -> None:
        passes.append(args)

    monkeypatch.setattr(daemon, pass_name, record_pass)

    entered = asyncio.Event()
    suspended = asyncio.Event()

    async def held_pass(
        pool: Any, _progress: LoopProgress, run: Any, *, tasks: asyncio.TaskGroup
    ) -> None:
        run(pool)
        entered.set()
        await suspended.wait()

    async def short_sleep(_progress: LoopProgress, _total_s: float) -> None:
        entered.set()
        await suspended.wait()

    # Cancel inside an admitted pass or the first quiesced sleep, rather than
    # racing a fixed delay against worker-thread completion.
    monkeypatch.setattr(daemon, "maintenance_with_liveness", held_pass)
    monkeypatch.setattr(daemon, "sleep_with_liveness", short_sleep)
    async with asyncio.TaskGroup() as tasks:
        task = tasks.create_task(
            loop(
                object(),
                LoopProgress("t", timeout_s=5.0),
                events_maintenance_config(process_config),
                tasks=tasks,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=2.0)
        finally:
            task.cancel()
    assert task.cancelled(), "the loop must propagate cancellation"
    assert bool(passes) is (not quiesced)
