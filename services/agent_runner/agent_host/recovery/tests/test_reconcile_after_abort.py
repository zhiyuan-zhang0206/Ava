"""Real-row abort settlement: every claimed chat is consumed, none is redelivered.

The same `consume_claimed_inbounds` pass disposes rows left claimed after a
hosted abort, at turn end and on cold admission. Coverage includes ownership
fences and idempotency. The pass never reads checkpoint history: an interrupted
claim has an unknown result whether or not its message reached the checkpoint,
so it is consumed and an abort cannot cause execution to replay.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.startup import consume_claimed_inbounds
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from services.agent_runner.agent_host import host as host_module
from services.agent_runner.agent_host import settlement as settlement_mod
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy
from services.agent_runner.agent_host.tests.lifecycle.recovery_admission import admit as _admit


def claimed_row(
    conn: psycopg.Connection[Any], agent: int, content: str, *, age: timedelta = timedelta()
) -> int:
    """One `'claimed'` chat row, exactly what an aborted turn leaves behind."""
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, claimed_at) "
        "VALUES (%s, %s, 'chat', 'user', 'claimed', %s) RETURNING id",
        (agent, content, datetime.now(UTC) - age),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def row_statuses(conn: psycopg.Connection[Any], ids: list[int]) -> dict[int, str]:
    rows = conn.execute(
        "SELECT id, status FROM inbound_messages WHERE id = ANY(%s)", (ids,)
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


async def test_settled_abort_consumes_every_claimed_row(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """Abort settlement consumes young and stale claims alike, with no history read."""
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    young = claimed_row(db_conn, agent, "young")
    stale = claimed_row(db_conn, agent, "stale", age=timedelta(days=2))

    await settlement_mod.reconcile_inbounds_after_abort(aops_pool, incarnation, resources=None)

    assert row_statuses(db_conn, [young, stale]) == {young: "done", stale: "done"}
    records = [r for r in loguru_records if r["extra"].get("event") == "inbound_reconcile"]
    assert [r["extra"]["consumed"] for r in records] == [2]


async def test_boot_consume_after_the_abort_pass_changes_nothing(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A kill after the abort leaves the rows already final: the boot pass
    (the same helper) is idempotent and silent on the second run."""
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    claimed = claimed_row(db_conn, agent, "claimed")

    await settlement_mod.reconcile_inbounds_after_abort(aops_pool, incarnation, resources=None)
    assert row_statuses(db_conn, [claimed]) == {claimed: "done"}
    logged = [r for r in loguru_records if r["extra"].get("event") == "inbound_reconcile"]
    await consume_claimed_inbounds(aops_pool, agent, incarnation=incarnation)

    assert row_statuses(db_conn, [claimed]) == {claimed: "done"}
    assert [r for r in loguru_records if r["extra"].get("event") == "inbound_reconcile"] == logged


async def test_no_claimed_rows_is_silent(
    redis_url: str,
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    await consume_claimed_inbounds(aops_pool, incarnation.agent_id, incarnation=incarnation)

    assert not [r for r in loguru_records if r["extra"].get("event") == "inbound_reconcile"]


async def test_next_claim_does_not_re_deliver_any_interrupted_row(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """No interrupted claim is retried after abort."""
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    first = claimed_row(db_conn, agent, "first")
    second = claimed_row(db_conn, agent, "second")

    await settlement_mod.reconcile_inbounds_after_abort(aops_pool, incarnation, resources=None)
    claimed = await claim_inbound_batch(aops_pool, agent, incarnation=incarnation, work=None)

    assert claimed == []
    assert row_statuses(db_conn, [first, second]) == {first: "done", second: "done"}


async def test_cold_build_consumes_claims_without_reading_checkpoint_history(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """The cold runtime build consumes claims whatever the checkpoint holds.

    The checkpointer handed to the host is unusable: any read of it raises, so
    consumption cannot depend on history evidence of what was committed.
    """
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    claimed = claimed_row(db_conn, agent, "claimed")
    unusable_saver = Mock()
    monkeypatch.setattr(host_module, "build_runtime", AsyncMock())
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=unusable_saver,
        graph=Mock(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog(skills=list),
    )
    slices = AgentSlices.resolve(default_reader=configured_policy().default_reader)

    await host._build_runtime(agent, "fingerprint", slices, incarnation=incarnation)

    assert row_statuses(db_conn, [claimed]) == {claimed: "done"}
    assert unusable_saver.method_calls == []
