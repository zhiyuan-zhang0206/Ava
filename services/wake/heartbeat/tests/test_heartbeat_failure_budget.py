"""The heartbeat poll and liveness loops log an unexpected failed round and carry
on, but a run of failures ends the process for the supervisor; a round that
completes starts the count over and an unreachable database is skipped."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.daemon.loop_health import LoopProgress
from base.daemon.round_loop import CONSECUTIVE_ROUND_FAILURE_LIMIT, RoundFailureLimitError
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from services.wake.heartbeat import daemon as heartbeat_daemon


def _drive(loop: str, gate: ProcessDbGate) -> Awaitable[None]:
    pool = cast(ConnectionPool, object())
    database = Database.from_settings(gate=gate)
    bus = EventBus.from_settings()
    progress = LoopProgress(loop, 60.0)
    if loop == "poll":
        return heartbeat_daemon.dispatch_loop(pool, database, bus, progress)
    return heartbeat_daemon.liveness_loop(database, pool, bus, progress)


def _script(
    monkeypatch: pytest.MonkeyPatch, loop: str, outcomes: list[BaseException | None]
) -> Callable[[], int]:
    """Each round raises its scripted outcome (None: completes); the loop is
    cancelled when the script runs out. Returns the number of rounds run."""
    rounds = 0

    def next_outcome() -> None:
        nonlocal rounds
        if rounds == len(outcomes):
            raise asyncio.CancelledError
        outcome = outcomes[rounds]
        rounds += 1
        if outcome is not None:
            raise outcome

    async def no_sleep(_liveness: object, _total_s: float) -> None:
        return None

    async def liveness_pass(*_args: object) -> None:
        next_outcome()

    def empty_poll(*_args: object, **_kwargs: object) -> list[tuple[int, float]]:
        next_outcome()
        return []

    def nothing(*_args: object, **_kwargs: object) -> None:
        return None

    def no_deadlines(*_args: object, **_kwargs: object) -> dict[int, float]:
        return {}

    monkeypatch.setattr(heartbeat_daemon, "sleep_with_liveness", no_sleep)
    if loop == "poll":
        monkeypatch.setattr(heartbeat_daemon, "_sweep_backoff_resets", nothing)
        monkeypatch.setattr(heartbeat_daemon, "_reconcile_checkin_outcomes", nothing)
        monkeypatch.setattr(heartbeat_daemon, "_select_idle_agents_needing_heartbeat", empty_poll)
        monkeypatch.setattr(heartbeat_daemon, "backoff_deadlines", no_deadlines)
    else:
        monkeypatch.setattr(heartbeat_daemon, "run_liveness_pass", liveness_pass)
    return lambda: rounds


@pytest.mark.parametrize("loop", ["poll", "liveness"])
async def test_consecutive_failed_rounds_end_the_loop(
    monkeypatch: pytest.MonkeyPatch, loop: str, database_gate: ProcessDbGate
) -> None:
    limit = CONSECUTIVE_ROUND_FAILURE_LIMIT
    rounds = _script(monkeypatch, loop, [RuntimeError("boom")] * (limit + 3))

    with pytest.raises(RoundFailureLimitError):
        await _drive(loop, database_gate)

    assert rounds() == limit, "the loop must stop at the limit, not keep retrying"


@pytest.mark.parametrize("loop", ["poll", "liveness"])
async def test_a_completed_round_and_a_db_outage_do_not_exhaust_the_budget(
    monkeypatch: pytest.MonkeyPatch, loop: str, database_gate: ProcessDbGate
) -> None:
    limit = CONSECUTIVE_ROUND_FAILURE_LIMIT
    boom = RuntimeError("boom")
    outcomes: list[BaseException | None] = [boom] * (limit - 1)
    outcomes += [None]
    outcomes += [psycopg.OperationalError("db down")] * limit
    outcomes += [boom] * (limit - 1)
    rounds = _script(monkeypatch, loop, outcomes)

    with pytest.raises(asyncio.CancelledError):
        await _drive(loop, database_gate)

    assert rounds() == len(outcomes)
