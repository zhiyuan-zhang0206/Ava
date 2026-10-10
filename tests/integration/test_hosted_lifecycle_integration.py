"""A hosted force cannot be undone by a prior restart. Integration: agent and ops are peers that both write the lifecycle row and fence each other, so no package below the top level may hold it (registered in scripts/structure/tests_location_allowed.py)."""

import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from agent.ownership.tests.test_lifecycle_intent import _command
from agent.tests.claim.test_inbound_ownership import _admit, _agent
from base.config import settings
from base.db import PG_KEEPALIVE_KWARGS, Database
from base.events.live.bus import EventBus
from ops.lifecycle.termination import _force_terminate_transaction


@pytest.mark.parametrize("applied", [False, True])
async def test_hosted_force_cannot_be_undone_by_prior_restart(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    applied: bool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    first = _command(db_conn, agent_id, "restart")
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner, work=None)
    if applied:
        assert (
            await apply_hosted_lifecycle(aops_pool, owner, bus=event_bus, resources=None)
            == "restart"
        )
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        _, _, _, force, _cutoff = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    later = _command(db_conn, agent_id, "restart")
    assert await apply_hosted_lifecycle(aops_pool, owner, bus=event_bus, resources=None) is None
    assert (
        await admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", uuid4(), expected_from="idling", db=database
        )
        is None
    )
    assert db_conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at,payload->'lifecycle_result'->>'reason' "
        "FROM inbound_messages WHERE id=%s",
        (first,),
    ).fetchone() == ("done", applied, None, "force_terminate")
    assert db_conn.execute(
        "SELECT id,status FROM inbound_messages WHERE id IN (%s,%s) ORDER BY id", (force, later)
    ).fetchall() == [(force, "pending" if applied else "claimed"), (later, "pending")]
    assert db_conn.execute(
        "SELECT status,lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated", None if applied else force)
