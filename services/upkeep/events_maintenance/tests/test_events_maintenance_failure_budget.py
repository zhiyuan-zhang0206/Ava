"""The maintenance loops log an unexpected failed pass and carry on, but a run of
failures ends the process for the supervisor; a pass that completes starts the
count over and an unreachable database is skipped, not counted."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.daemon.health import LoopProgress
from base.daemon.round_loop import CONSECUTIVE_ROUND_FAILURE_LIMIT, RoundFailureLimitError
from services.upkeep.events_maintenance import daemon
from services.upkeep.events_maintenance.tests.slices import (
    events_maintenance_config,
    events_maintenance_db,
)
from tests.fixtures.unit.identity import machine_identity as machine_identity

_FAKE_POOL: Any = object()


def _drive(
    loop: str,
    tasks: asyncio.TaskGroup,
    process_config: ConfigBoot,
    machine_identity: MachineIdentity,
) -> Awaitable[None]:
    progress = LoopProgress(loop, timeout_s=5.0)
    config = events_maintenance_config(process_config)
    if loop == "dispatch":
        return daemon._dispatch_loop(
            _FAKE_POOL,
            progress,
            config,
            events_maintenance_db(process_config, local_machine=machine_identity),
            tasks=tasks,
        )
    return daemon.resolution_loop(_FAKE_POOL, progress, config, tasks=tasks)


def _script(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[BaseException | None]
) -> Callable[[], int]:
    """Make each pass raise its scripted outcome (None: complete); the loop is
    cancelled once the script runs out. Returns the number of passes run."""
    passes = 0

    async def fake_pass(*_args: object, **_kwargs: object) -> None:
        nonlocal passes
        if passes == len(outcomes):
            raise asyncio.CancelledError
        outcome = outcomes[passes]
        passes += 1
        if outcome is not None:
            raise outcome

    async def no_sleep(_progress: LoopProgress, _total_s: float) -> None:
        return None

    monkeypatch.setattr(daemon, "maintenance_with_liveness", fake_pass)
    monkeypatch.setattr(daemon, "_sleep_with_liveness", no_sleep)
    return lambda: passes


@pytest.mark.parametrize("loop", ["dispatch", "resolution"])
async def test_consecutive_failed_passes_end_the_loop(
    monkeypatch: pytest.MonkeyPatch,
    loop: str,
    process_config: ConfigBoot,
    machine_identity: MachineIdentity,
) -> None:
    limit = CONSECUTIVE_ROUND_FAILURE_LIMIT
    passes = _script(monkeypatch, [RuntimeError("boom")] * (limit + 3))

    with pytest.raises(ExceptionGroup) as raised:
        async with asyncio.TaskGroup() as tasks:
            await _drive(loop, tasks, process_config, machine_identity)

    assert raised.group_contains(RoundFailureLimitError)
    assert passes() == limit, "the loop must stop at the limit, not keep retrying"


@pytest.mark.parametrize("loop", ["dispatch", "resolution"])
async def test_a_completed_pass_and_a_db_outage_do_not_exhaust_the_budget(
    monkeypatch: pytest.MonkeyPatch,
    loop: str,
    process_config: ConfigBoot,
    machine_identity: MachineIdentity,
) -> None:
    limit = CONSECUTIVE_ROUND_FAILURE_LIMIT
    boom = RuntimeError("boom")
    # `limit - 1` failures, a completed pass, `limit - 1` more failures with
    # database outages between them: never `limit` unexpected failures in a row.
    outcomes: list[BaseException | None] = [boom] * (limit - 1)
    outcomes += [None]
    outcomes += [psycopg.OperationalError("db down")] * limit
    outcomes += [boom] * (limit - 1)
    passes = _script(monkeypatch, outcomes)

    with pytest.raises(asyncio.CancelledError):
        async with asyncio.TaskGroup() as tasks:
            await _drive(loop, tasks, process_config, machine_identity)

    assert passes() == len(outcomes)
