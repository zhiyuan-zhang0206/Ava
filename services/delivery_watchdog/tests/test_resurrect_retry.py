"""The terminated-owner resurrect retry loop: persisted cooldown, per-round cap,
single flight, RPC deadline, and the durable failure/suppression ladder."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import ops.lifecycle as ol
from base.agents import AgentStatus
from base.config import settings
from base.daemon.loop_health import LoopProgress
from base.db import Database, insert_inbound_message
from services.delivery_watchdog import attempts, resurrect_guard, resurrect_retry, rounds

_THRESHOLD_S = 86400.0


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=4, open=True)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def progress() -> LoopProgress:
    return LoopProgress("resurrect", rounds.loop_liveness_timeout_s())


def _terminated_owner_with_chat(db: psycopg.Connection) -> tuple[int, int]:
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'terminated', termination_source = 'exit' "
            "WHERE id = %s",
            (aid,),
        )
    db.commit()
    return aid, insert_inbound_message(db, aid, "hello?", source="user")


def _ignore_emit(*_args: object, **_kwargs: object) -> None:
    return None


def _expire_cooldown(db: psycopg.Connection, aid: int) -> None:
    db.execute(
        "UPDATE delivery_watchdog_attempts SET last_attempt_at = now() - interval '1 hour' "
        "WHERE kind = 'resurrect' AND agent_id = %s",
        (aid,),
    )
    db.commit()


def _state(db: psycopg.Connection, aid: int) -> tuple[int, int]:
    row = db.execute(
        "SELECT consecutive_failures, suppress_count FROM delivery_watchdog_attempts "
        "WHERE kind = 'resurrect' AND agent_id = %s",
        (aid,),
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def _stub_resurrect(monkeypatch: pytest.MonkeyPatch, outcome: AgentStatus) -> list[int]:
    calls: list[int] = []

    async def fake(
        _db: object, aid: int, *, trigger_inbound_id: int, trigger_inbound_kind: str
    ) -> AgentStatus:
        assert trigger_inbound_kind == "chat"
        calls.append(aid)
        return outcome

    monkeypatch.setattr(ol, "resurrect_if_terminated", fake)
    return calls


async def test_repeated_terminated_results_suppress_and_emit_once(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid, _ = _terminated_owner_with_chat(db_conn)
    calls = _stub_resurrect(monkeypatch, AgentStatus.TERMINATED)
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr(resurrect_guard.telemetry, "emit", _record_emit)
    monkeypatch.setattr(settings.daemon, "delivery_watchdog_resurrect_fail_before_suppress", 5)
    monkeypatch.setattr(settings.daemon, "delivery_watchdog_suppress_base_seconds", 1800.0)
    monkeypatch.setattr(settings.daemon, "delivery_watchdog_suppress_max_seconds", 86400.0)

    for _ in range(5):
        await resurrect_retry.resurrect_round(
            pool, Database.from_settings(), progress, 5, _THRESHOLD_S
        )
        _expire_cooldown(db_conn, aid)

    row = db_conn.execute(
        "SELECT wake_suppress_reason, "
        "EXTRACT(EPOCH FROM (wake_suppressed_until - clock_timestamp())) "
        "FROM agents_meta WHERE id = %s",
        (aid,),
    ).fetchone()
    assert row is not None
    assert row[0] == "resurrect_failed"
    assert 1795.0 < float(row[1]) <= 1800.0
    assert calls == [aid] * 5
    assert len(emitted) == 1
    assert emitted[0][0] == ("telemetry", "delivery_wake_suppressed")
    assert emitted[0][1]["attributes"] == {
        "consecutive_failures": 5,
        "suppress_count": 1,
        "suppress_seconds": 1800.0,
        "reason": "resurrect_failed",
    }
    assert _state(db_conn, aid) == (0, 1)

    # The durable selector, not the cooldown, keeps later rounds away.
    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 5, _THRESHOLD_S)
    assert calls == [aid] * 5


async def test_success_resets_failure_and_suppression_escalation_counts(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid, trigger = _terminated_owner_with_chat(db_conn)
    attempts.claim_attempts(pool, attempts.RESURRECT, [aid], 0.0)
    db_conn.execute(
        "UPDATE delivery_watchdog_attempts SET consecutive_failures = 4, suppress_count = 3 "
        "WHERE kind = 'resurrect' AND agent_id = %s",
        (aid,),
    )
    db_conn.commit()
    _stub_resurrect(monkeypatch, AgentStatus.IDLING)

    await resurrect_retry.resurrect_one(pool, Database.from_settings(), aid, trigger)

    assert _state(db_conn, aid) == (0, 0)


async def test_expired_suppression_escalates_again_with_bounded_backoff(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid, trigger = _terminated_owner_with_chat(db_conn)
    _stub_resurrect(monkeypatch, AgentStatus.TERMINATED)
    monkeypatch.setattr(resurrect_guard.telemetry, "emit", _ignore_emit)
    monkeypatch.setattr(settings.daemon, "delivery_watchdog_resurrect_fail_before_suppress", 1)
    monkeypatch.setattr(settings.daemon, "delivery_watchdog_suppress_base_seconds", 10.0)
    monkeypatch.setattr(settings.daemon, "delivery_watchdog_suppress_max_seconds", 15.0)

    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 5, _THRESHOLD_S)
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until = now() - interval '1 second' WHERE id = %s",
        (aid,),
    )
    db_conn.commit()
    _expire_cooldown(db_conn, aid)
    assert resurrect_retry.select_terminated_owners_with_pending(pool, _THRESHOLD_S) == [
        (aid, trigger)
    ]

    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 5, _THRESHOLD_S)

    row = db_conn.execute(
        "SELECT EXTRACT(EPOCH FROM (wake_suppressed_until - clock_timestamp())) "
        "FROM agents_meta WHERE id = %s",
        (aid,),
    ).fetchone()
    assert row is not None
    assert 14.0 < float(row[0]) <= 15.0
    assert _state(db_conn, aid) == (0, 2)


async def test_failed_resurrect_enters_a_cooldown_that_lives_in_the_database(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attempt clock is a row, not process memory: the second round (a
    restarted watchdog sees the same thing) does not retry inside the cooldown,
    and the failure it recorded is still there."""
    aid, _ = _terminated_owner_with_chat(db_conn)
    calls: list[int] = []

    async def fail(_db: object, aid_: int, **_kwargs: object) -> AgentStatus:
        calls.append(aid_)
        raise RuntimeError("runner unavailable")

    monkeypatch.setattr(ol, "resurrect_if_terminated", fail)

    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 5, _THRESHOLD_S)
    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 5, _THRESHOLD_S)

    assert calls == [aid]
    assert _state(db_conn, aid) == (1, 0)
    _expire_cooldown(db_conn, aid)
    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 5, _THRESHOLD_S)
    assert calls == [aid, aid]
    assert _state(db_conn, aid) == (2, 0)


async def test_cooldown_counts_from_the_attempts_end(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid, _ = _terminated_owner_with_chat(db_conn)

    async def slow_failure(_db: object, aid_: int, **_kwargs: object) -> AgentStatus:
        # Age the claim row while the RPC is in flight, as a slow RPC would.
        await asyncio.to_thread(_expire_cooldown, db_conn, aid_)
        raise RuntimeError("unreachable after retries")

    monkeypatch.setattr(ol, "resurrect_if_terminated", slow_failure)

    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 5, _THRESHOLD_S)

    claimed, _ = attempts.claim_attempts(pool, attempts.RESURRECT, [aid], 60.0)
    assert claimed == []


async def test_hung_rpc_is_cut_at_the_deadline_and_counts_as_a_failure(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid, _ = _terminated_owner_with_chat(db_conn)

    async def hang(_db: object, aid_: int, **_kwargs: object) -> AgentStatus:
        await asyncio.Event().wait()
        return AgentStatus.TERMINATED

    monkeypatch.setattr(ol, "resurrect_if_terminated", hang)
    monkeypatch.setattr(rounds, "rpc_deadline_s", lambda: 0.05)

    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 5, _THRESHOLD_S)

    assert _state(db_conn, aid) == (1, 0)


async def test_cancelled_round_enters_cooldown_without_counting_a_failure(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid, _ = _terminated_owner_with_chat(db_conn)
    started = asyncio.Event()

    async def block(_db: object, aid_: int, **_kwargs: object) -> AgentStatus:
        started.set()
        await asyncio.Event().wait()
        return AgentStatus.TERMINATED

    monkeypatch.setattr(ol, "resurrect_if_terminated", block)
    round_task = asyncio.create_task(
        resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 5, _THRESHOLD_S)
    )
    await started.wait()
    round_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await round_task

    assert _state(db_conn, aid) == (0, 0)
    claimed, _ = attempts.claim_attempts(pool, attempts.RESURRECT, [aid], 60.0)
    assert claimed == []


async def test_the_cap_bounds_each_round_and_the_backlog_drains_over_rounds(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _ = _terminated_owner_with_chat(db_conn)
    second, _ = _terminated_owner_with_chat(db_conn)
    calls = _stub_resurrect(monkeypatch, AgentStatus.IDLING)

    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 1, _THRESHOLD_S)
    assert calls == [first]

    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 1, _THRESHOLD_S)
    assert calls == [first, second]

    await resurrect_retry.resurrect_round(pool, Database.from_settings(), progress, 1, _THRESHOLD_S)
    assert calls == [first, second]


async def test_a_slow_resurrect_is_never_attempted_twice_in_flight(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single flight is the loop's own sequencing: while the RPC spans many
    intervals the loop is inside the round, so nothing else can start one."""
    aid, _ = _terminated_owner_with_chat(db_conn)
    release = asyncio.Event()
    calls: list[int] = []

    async def slow(_db: object, aid_: int, **_kwargs: object) -> AgentStatus:
        calls.append(aid_)
        await release.wait()
        return AgentStatus.TERMINATED

    monkeypatch.setattr(ol, "resurrect_if_terminated", slow)
    monkeypatch.setattr(resurrect_guard.telemetry, "emit", _ignore_emit)
    loop_task = asyncio.create_task(
        resurrect_retry.resurrect_loop(
            pool, Database.from_settings(), progress, 0.01, 5, _THRESHOLD_S
        )
    )
    try:
        await asyncio.sleep(0.3)
        assert calls == [aid]
        release.set()
        await asyncio.sleep(0.3)
        assert calls == [aid]  # finished, and now inside the persisted cooldown
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_concurrency_within_a_round_is_bounded(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _ in range(4):
        _terminated_owner_with_chat(db_conn)
    running = 0
    peak = 0

    async def tracked(_db: object, aid_: int, **_kwargs: object) -> AgentStatus:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return AgentStatus.IDLING

    monkeypatch.setattr(ol, "resurrect_if_terminated", tracked)

    await resurrect_retry.resurrect_round(
        pool, Database.from_settings(), progress, 10, _THRESHOLD_S
    )

    assert peak == resurrect_retry._RESURRECT_MAX_CONCURRENCY
