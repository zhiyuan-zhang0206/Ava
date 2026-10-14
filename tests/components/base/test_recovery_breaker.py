"""Recovery circuit breaker: durable streak, halt write, and gate literal."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base.agents.recovery.breaker import (
    HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS,
    PERMANENT_REJECT_REASON_BILLING,
    RECOVERY_BREAKER_CLEAR,
    SUPPRESS_REASON_PERMANENT_REJECT,
    halt_automatic_recovery,
    record_permanent_reject_turn,
)
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog


def test_clear_literal_matches_the_halt_threshold() -> None:
    """The SQL half of the breaker embeds the Python threshold as a literal;
    keep them in sync — a drift here silently changes every automatic gate."""
    assert f"permanent_reject_streak < {HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS}" == (
        RECOVERY_BREAKER_CLEAR
    )


async def test_record_increments_and_halt_suppresses_until_human(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )

    assert await record_permanent_reject_turn(aops_pool, aid, PERMANENT_REJECT_REASON_BILLING) == 1
    reason_row = db_conn.execute(
        "SELECT last_permanent_reject_reason FROM agents_meta WHERE id = %s", (aid,)
    ).fetchone()
    assert reason_row == (PERMANENT_REJECT_REASON_BILLING,)
    assert await record_permanent_reject_turn(aops_pool, aid, PERMANENT_REJECT_REASON_BILLING) == 2

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
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A bounded `resurrect_failed` window is weaker than the until-human halt:
    the trip replaces it (and an expired window even of its own reason)."""
    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
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
        await record_permanent_reject_turn(aops_pool, 999_999_999, PERMANENT_REJECT_REASON_BILLING)


def test_billing_reason_literal_matches_the_circuit_reason() -> None:
    """Anti-drift pin (task #3919): the durable value `runloop` records for an
    HTTP 402 (CIRCUIT_REASON_BILLING) is exactly the value the billing
    batch-recovery whitelist filters on."""
    from agent.state_channels import CIRCUIT_REASON_BILLING

    assert CIRCUIT_REASON_BILLING == PERMANENT_REJECT_REASON_BILLING
