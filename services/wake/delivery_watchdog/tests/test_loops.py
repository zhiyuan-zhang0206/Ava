"""The watchdog's loop structure: the TaskGroup that owns the four loops, the
sequential round runner, the bounded fan-out, and the attempt clocks."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.db import Database
from base.events.live.bus import EventBus
from ops.cluster_rpc import worst_case_dispatch_seconds
from services.wake.delivery_watchdog import attempts, daemon, rounds


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=4, open=True)
    try:
        yield p
    finally:
        p.close()


def _progress() -> LoopProgress:
    return LoopProgress("test", rounds.loop_liveness_timeout_s())


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
        await daemon._run_loops(
            pool, Database.from_settings(), EventBus.from_settings(), LivenessGroup()
        )

    assert [str(exc) for exc in raised.value.exceptions] == [f"{crashing} loop crashed"]
    assert sorted(cancelled) == sorted({"scan", "resurrect", "harvest", "hosted_turn"} - {crashing})


async def test_each_loop_reports_its_own_progress(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    liveness = LivenessGroup()
    _patch_loops(monkeypatch, crashing="scan", cancelled=[])

    with pytest.raises(ExceptionGroup):
        await daemon._run_loops(pool, Database.from_settings(), EventBus.from_settings(), liveness)

    assert set(liveness.snapshot()) == {"scan", "resurrect", "harvest", "hosted_turn"}


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


def test_the_job_deadline_is_derived_from_the_cluster_rpc_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.gateway, "cluster_rpc_timeout_seconds", 30.0)
    monkeypatch.setattr(settings.gateway, "cluster_rpc_max_retries", 3)

    # Four attempts of 30s, plus the 0.5 + 1 + 2 second backoffs at their +50% jitter ceiling.
    assert worst_case_dispatch_seconds() == pytest.approx(4 * 30.0 + 3.5 * 1.5)
    assert rounds.rpc_deadline_s() == pytest.approx(2 * worst_case_dispatch_seconds())
    assert rounds.loop_liveness_timeout_s() > rounds.rpc_deadline_s()

    monkeypatch.setattr(settings.gateway, "cluster_rpc_max_retries", 0)
    assert worst_case_dispatch_seconds() == pytest.approx(30.0)
