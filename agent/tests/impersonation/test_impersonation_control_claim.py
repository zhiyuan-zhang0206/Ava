"""Durable control claims across external and replaced native incarnations."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent import impersonation
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def test_control_claim_leaves_cancel_for_external_or_resumed_native(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    from agent.db import claim_inbound_batch
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    for kind in ("chat", "compact_request", "cancel"):
        db_conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'wait',%s,'user')",
            (agent_id, kind),
        )
    db_conn.commit()
    batch = await claim_inbound_batch(
        aops_pool, agent_id, lifecycle_only=True, incarnation=None, work=None
    )
    assert batch == []
    assert db_conn.execute(
        "SELECT kind FROM inbound_messages WHERE agent_id=%s AND status='pending' ORDER BY kind",
        (agent_id,),
    ).fetchall() == [("cancel",), ("chat",), ("compact_request",)]
    db_conn.commit()
    assert not await impersonation.lifecycle_ready(aops_pool, agent_id)
    # If the external holder never acknowledges cancellation, the ordinary
    # native claim after release/expiry still receives the durable request.
    resumed = await claim_inbound_batch(aops_pool, agent_id, incarnation=None, work=None)
    assert {item.kind for item in resumed} == {"cancel", "chat", "compact_request"}
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='cancel'",
        (agent_id,),
    ).fetchone() == ("done",)


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_control_claim_records_superseded_accepted_intent(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    kind: str,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:

    from agent.db import claim_inbound_batch
    from agent.ownership.hosted import admit_hosted_runtime
    from base.cluster.machine import machine_name
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    owner = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_name(),
        uuid4(),
        expected_from="idling",
        db=database,
    )
    assert owner is not None
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'',%s,'user')",
        (agent_id, kind),
    )
    db_conn.commit()
    accepted = await claim_inbound_batch(
        aops_pool, agent_id, lifecycle_only=True, incarnation=owner, work=None
    )
    assert len(accepted) == 1 and accepted[0].durable_lifecycle
    replacement = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (replacement.generation, replacement.owner, agent_id),
    )
    db_conn.commit()
    assert (
        await claim_inbound_batch(
            aops_pool, agent_id, lifecycle_only=True, incarnation=replacement, work=None
        )
        == []
    )
    assert db_conn.execute(
        "SELECT status,applied_at,target_generation,target_owner,payload->'lifecycle_result' "
        "FROM inbound_messages WHERE agent_id=%s AND kind=%s",
        (agent_id, kind),
    ).fetchone() == (
        "done",
        None,
        owner.generation,
        owner.owner,
        {"outcome": "superseded", "reason": "target_replaced"},
    )
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_control_claim_preserves_unaccepted_intent(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    kind: str,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    from agent.db import claim_inbound_batch
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'',%s,'user')",
        (agent_id, kind),
    )
    db_conn.commit()
    with pytest.raises(RuntimeError, match="lifecycle claim requires an admitted"):
        await claim_inbound_batch(
            aops_pool, agent_id, lifecycle_only=True, incarnation=None, work=None
        )
    assert db_conn.execute(
        "SELECT status,claimed_at,payload->'lifecycle_result' "
        "FROM inbound_messages WHERE agent_id=%s",
        (agent_id,),
    ).fetchone() == ("pending", None, None)
