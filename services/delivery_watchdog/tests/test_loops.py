"""The watchdog's loop structure: the TaskGroup that owns the four loops, the
sequential round runner, the bounded fan-out, and the attempt clocks."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.daemon.loop_health import LivenessGroup, LoopProgress
from services.delivery_watchdog import attempts, daemon, rounds


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=4, open=True)
    try:
        yield p
    finally:
        p.close()


def _progress() -> LoopProgress:
    return LoopProgress("test", rounds.LOOP_LIVENESS_TIMEOUT_S)


def _agent(db: psycopg.Connection) -> int:
    from tests.fixtures.units import spawn_agent

    return spawn_agent(spawner="user")


def _patch_loops(
    monkeypatch: pytest.MonkeyPatch,
    *,
    crashing: str | None,
    cancelled: list[str],
    registered: list[str] | None = None,
) -> None:
    """Replace the four loop bodies with stand-ins that park forever, except
    `crashing`, which raises."""

    def stand_in(name: str):
        async def loop(*_args: object) -> None:
            if name == crashing:
                await asyncio.sleep(0.01)
                raise RuntimeError(f"{name} loop crashed")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return loop

    monkeypatch.setattr(daemon, "_scan_loop", stand_in("scan"))
    monkeypatch.setattr(daemon.resurrect_retry, "resurrect_loop", stand_in("resurrect"))
    monkeypatch.setattr(daemon.stall_recovery, "stall_recovery_loop", stand_in("harvest"))
    monkeypatch.setattr(daemon.turn_liveness, "hosted_turn_recovery_loop", stand_in("hosted_turn"))
    monkeypatch.setattr(daemon.turn_liveness, "hosted_turn_threshold_seconds", lambda: 2400.0)


@pytest.mark.parametrize("crashing", ["scan", "resurrect", "harvest", "hosted_turn"])
async def test_a_crashing_loop_cancels_its_siblings_and_ends_the_service(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch, crashing: str
) -> None:
    cancelled: list[str] = []
    _patch_loops(monkeypatch, crashing=crashing, cancelled=cancelled)

    with pytest.raises(ExceptionGroup) as raised:
        await daemon._run_loops(pool, LivenessGroup())

    assert [str(exc) for exc in raised.value.exceptions] == [f"{crashing} loop crashed"]
    assert sorted(cancelled) == sorted({"scan", "resurrect", "harvest", "hosted_turn"} - {crashing})


async def test_each_loop_reports_its_own_progress(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    liveness = LivenessGroup()
    _patch_loops(monkeypatch, crashing="scan", cancelled=[])

    with pytest.raises(ExceptionGroup):
        await daemon._run_loops(pool, liveness)

    assert set(liveness.snapshot()) == {"scan", "resurrect", "harvest", "hosted_turn"}


async def test_a_round_that_raises_ends_the_loop() -> None:
    async def one_round() -> None:
        raise ValueError("unexpected")

    with pytest.raises(ValueError):
        await rounds.run_rounds("t", _progress(), 0.01, one_round)


async def test_an_unreachable_database_skips_the_round_and_the_loop_goes_on() -> None:
    progress = _progress()
    outcomes = [psycopg.OperationalError("db down"), None, None]
    seen = 0

    async def one_round() -> None:
        nonlocal seen
        seen += 1
        outcome = outcomes[seen - 1]
        if outcome is not None:
            raise outcome

    task = asyncio.create_task(rounds.run_rounds("t", progress, 0.01, one_round))
    try:
        await asyncio.sleep(0.2)
        assert seen >= 3
        assert progress.snapshot()["last_error"] is not None
        assert progress.snapshot()["last_success_at"] is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rounds_are_strictly_sequential() -> None:
    running = 0
    peak = 0
    done = 0

    async def one_round() -> None:
        nonlocal running, peak, done
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.03)
        running -= 1
        done += 1

    task = asyncio.create_task(rounds.run_rounds("t", _progress(), 0.0, one_round))
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert done >= 3
    assert peak == 1


async def test_fan_out_bounds_concurrency_and_returns_when_all_are_done() -> None:
    running = 0
    peak = 0
    finished: list[int] = []

    def job(index: int):
        async def run() -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            finished.append(index)

        return run

    await rounds.fan_out([job(i) for i in range(6)], concurrency=2, progress=_progress())

    assert sorted(finished) == list(range(6))
    assert peak == 2


async def test_fan_out_failure_cancels_the_round() -> None:
    cancelled = asyncio.Event()

    async def failing() -> None:
        await asyncio.sleep(0.01)
        raise RuntimeError("job failed")

    async def parked() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(ExceptionGroup):
        await rounds.fan_out([failing, parked], concurrency=2, progress=_progress())

    assert cancelled.is_set()


def test_claim_hands_an_agent_out_once_per_cooldown(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    aid = _agent(db_conn)

    assert attempts.claim_attempts(pool, attempts.HARVEST, [aid], 60.0) == ([aid], 0)
    assert attempts.claim_attempts(pool, attempts.HARVEST, [aid], 60.0) == ([], 0)
    # Each loop kind keeps its own clock.
    assert attempts.claim_attempts(pool, attempts.RESURRECT, [aid], 60.0) == ([aid], 0)

    db_conn.execute(
        "UPDATE delivery_watchdog_attempts SET last_attempt_at = now() - interval '61 seconds' "
        "WHERE kind = 'harvest' AND agent_id = %s",
        (aid,),
    )
    db_conn.commit()
    assert attempts.claim_attempts(pool, attempts.HARVEST, [aid], 60.0) == ([aid], 0)


def test_claim_limit_defers_the_ready_agents_it_leaves(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    first, second, third = _agent(db_conn), _agent(db_conn), _agent(db_conn)
    assert attempts.claim_attempts(pool, attempts.RESURRECT, [first], 60.0) == ([first], 0)

    claimed, deferred = attempts.claim_attempts(
        pool, attempts.RESURRECT, [first, second, third], 60.0, limit=1
    )

    # `first` is cooling and is neither claimed nor counted as deferred.
    assert (claimed, deferred) == ([second], 1)


def test_claim_never_hands_out_an_unknown_agent(pool: ConnectionPool) -> None:
    assert attempts.claim_attempts(pool, attempts.HARVEST, [2_000_000_000], 60.0) == ([], 0)


def test_finish_restarts_the_cooldown_clock(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    aid = _agent(db_conn)
    attempts.claim_attempts(pool, attempts.HOSTED_TURN, [aid], 600.0)
    db_conn.execute(
        "UPDATE delivery_watchdog_attempts SET last_attempt_at = now() - interval '1 hour' "
        "WHERE kind = 'hosted_turn' AND agent_id = %s",
        (aid,),
    )
    db_conn.commit()

    attempts.finish_attempt(pool, attempts.HOSTED_TURN, aid)

    assert attempts.claim_attempts(pool, attempts.HOSTED_TURN, [aid], 600.0) == ([], 0)
