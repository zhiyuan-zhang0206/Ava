"""Recovery circuit breaker: durable streak, halt write, and gate literal."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from shared.recovery_breaker import (
    HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS,
    RECOVERY_BREAKER_CLEAR,
    SUPPRESS_REASON_PERMANENT_REJECT,
    halt_automatic_recovery,
    record_permanent_reject_turn,
)
from tests.conftest import spawn_agent


def test_clear_literal_matches_the_halt_threshold() -> None:
    """The SQL half of the breaker embeds the Python threshold as a literal;
    keep them in sync — a drift here silently changes every automatic gate."""
    assert f"permanent_reject_streak < {HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS}" == (
        RECOVERY_BREAKER_CLEAR
    )


async def test_record_increments_and_halt_suppresses_until_human(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    aid = spawn_agent(spawner="user")

    assert await record_permanent_reject_turn(aops_pool, aid) == 1
    assert await record_permanent_reject_turn(aops_pool, aid) == 2

    assert await halt_automatic_recovery(aops_pool, aid) is True
    row = db_conn.execute(
        "SELECT wake_suppress_reason, "
        "EXTRACT(EPOCH FROM (wake_suppressed_until - clock_timestamp())) "
        "FROM agents_meta WHERE id = %s",
        (aid,),
    ).fetchone()
    assert row is not None
    reason, window_s = row
    assert reason == SUPPRESS_REASON_PERMANENT_REJECT
    assert window_s > 300 * 24 * 3600.0  # until-human, far past any timer

    # Idempotent: a second trip leaves the active window untouched.
    assert await halt_automatic_recovery(aops_pool, aid) is False


async def test_halt_replaces_another_reasons_window(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """A bounded `resurrect_failed` window is weaker than the until-human halt:
    the trip replaces it (and an expired window even of its own reason)."""
    aid = spawn_agent(spawner="user")
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until = now() + interval '1 hour', "
        "wake_suppress_reason = 'resurrect_failed' WHERE id = %s",
        (aid,),
    )
    db_conn.commit()

    assert await halt_automatic_recovery(aops_pool, aid) is True
    row = db_conn.execute(
        "SELECT wake_suppress_reason, wake_suppressed_until > now() + interval '300 days' "
        "FROM agents_meta WHERE id = %s",
        (aid,),
    ).fetchone()
    assert row == (SUPPRESS_REASON_PERMANENT_REJECT, True)


async def test_record_requires_the_agent_row(aops_pool: AsyncConnectionPool) -> None:
    with pytest.raises(RuntimeError, match="disappeared"):
        await record_permanent_reject_turn(aops_pool, 999_999_999)
