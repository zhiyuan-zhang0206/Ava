"""Stale-incarnation ownership fences preserve claimed rows during reconcile."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.inbound import RuntimeOwnershipLostError
from agent.startup import reconcile_claimed_inbounds_at_startup
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host import settlement as settlement_mod
from services.agent_runner.agent_host.recovery.tests.test_hosted_db_recovery import admit_recovery
from services.agent_runner.agent_host.recovery.tests.test_reconcile_after_abort import (
    claimed_row,
    row_statuses,
    seed_checkpoint,
)
from services.agent_runner.agent_host.tests.host_policy import configured_policy


async def test_replaced_incarnation_writes_nothing(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """A replacement runtime owns the row now: the stale incarnation's pass is
    refused by the lease fence before any write."""
    incarnation = await admit_recovery(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
    )
    agent = incarnation.agent_id
    committed = claimed_row(db_conn, agent, "committed")
    orphan = claimed_row(db_conn, agent, "orphan")
    saver = await seed_checkpoint(aops_pool, agent, committed)
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s, runtime_owner=%s WHERE id=%s",
        (uuid4(), uuid4(), agent),
    )
    db_conn.commit()

    with pytest.raises(RuntimeOwnershipLostError):
        await reconcile_claimed_inbounds_at_startup(
            aops_pool,
            saver,
            agent,
            incarnation=incarnation,
            inputs=configured_policy().reconcile_inputs,
        )

    assert row_statuses(db_conn, [committed, orphan]) == {
        committed: "claimed",
        orphan: "claimed",
    }


async def test_settlement_pass_swallows_a_replaced_incarnation(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """At the settlement boundary the same refusal is a logged no-op — the
    abort's settlement must not fail because its reconcile was fenced out."""
    incarnation = await admit_recovery(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
    )
    agent = incarnation.agent_id
    committed = claimed_row(db_conn, agent, "committed")
    saver = await seed_checkpoint(aops_pool, agent, committed)
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s, runtime_owner=%s WHERE id=%s",
        (uuid4(), uuid4(), agent),
    )
    db_conn.commit()

    # must not raise: the settlement is not failed by a fenced-out reconcile
    await settlement_mod.reconcile_inbounds_after_abort(
        aops_pool, saver, incarnation, resources=None, inputs=configured_policy().reconcile_inputs
    )

    assert row_statuses(db_conn, [committed]) == {committed: "claimed"}
    skips = [r for r in loguru_records if r["extra"].get("event") == "host_abort_reconcile_skipped"]
    assert [r["extra"]["reason"] for r in skips] == ["ownership_lost"]
