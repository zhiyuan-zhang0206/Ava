"""Stale-incarnation ownership fences preserve claimed rows during the consume pass."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.inbound import RuntimeOwnershipLostError
from agent.startup import consume_claimed_inbounds
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host import settlement as settlement_mod
from services.agent_runner.agent_host.recovery.tests.test_reconcile_after_abort import (
    claimed_row,
    row_statuses,
)
from services.agent_runner.agent_host.tests.lifecycle.recovery_admission import admit
from tests.fixtures.unit.identity import machine_identity as machine_identity


def _replace_owner(conn: psycopg.Connection[Any], agent: int) -> None:
    conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s, runtime_owner=%s WHERE id=%s",
        (uuid4(), uuid4(), agent),
    )
    conn.commit()


async def test_replaced_incarnation_writes_nothing(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A replacement runtime owns the row now: the stale incarnation's pass is
    refused by the lease fence before any write."""
    incarnation = await admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    claimed = claimed_row(db_conn, agent, "claimed")
    _replace_owner(db_conn, agent)

    with pytest.raises(RuntimeOwnershipLostError):
        await consume_claimed_inbounds(aops_pool, agent, incarnation=incarnation)

    assert row_statuses(db_conn, [claimed]) == {claimed: "claimed"}


async def test_settlement_pass_swallows_a_replaced_incarnation(
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
    """At the settlement boundary the same refusal is a logged no-op — the
    abort's settlement must not fail because its pass was fenced out."""
    incarnation = await admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    claimed = claimed_row(db_conn, agent, "claimed")
    _replace_owner(db_conn, agent)

    # must not raise: the settlement is not failed by a fenced-out pass
    await settlement_mod.reconcile_inbounds_after_abort(aops_pool, incarnation, resources=None)

    assert row_statuses(db_conn, [claimed]) == {claimed: "claimed"}
    skips = [r for r in loguru_records if r["extra"].get("event") == "host_abort_reconcile_skipped"]
    assert [r["extra"]["reason"] for r in skips] == ["ownership_lost"]
