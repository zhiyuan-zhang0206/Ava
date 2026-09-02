"""Hosted application waits for graph return, then uses the durable command."""

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.db import ClaimedInbound, claim_inbound_batch
from agent.graph._claim_dispatch import _BatchState, _handle_restart
from agent.hosted_ownership import admit_hosted_runtime, settle_hosted_runtime
from services.agent_host.host import AgentHost
from shared.context import AvaContext
from shared.turn_identity import bind_turn_identity
from tests.agent.test_inbound_ownership import _admit, _agent
from tests.agent.test_lifecycle_intent import _command


async def test_hosted_restart_marker_does_not_claim_completion() -> None:
    state = _BatchState()
    await _handle_restart(
        AvaContext(hosted=True),
        1,
        ClaimedInbound(id=1, agent_id=1, content="", kind="restart", source="self", payload={}),
        state,
    )
    assert state.restart_requested
    assert isinstance(state.new_msgs[0], SystemMessage)
    content = state.new_msgs[0].model_dump()["content"]
    assert isinstance(content, str)
    assert "Restart was accepted" in content
    assert "have been restarted" not in content


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_hosted_applies_only_after_continuation_returns(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, kind: str
) -> None:
    agent_id = _agent(db_conn)
    old = await _admit(aops_pool, agent_id)
    inbound = _command(db_conn, agent_id, kind)
    entered, release = asyncio.Event(), asyncio.Event()

    async def graph_return(*args: object, **kwargs: object) -> dict[str, bool]:
        entered.set()
        await release.wait()
        return {
            "exit_requested": kind == "terminate",
            "restart_requested": kind == "restart",
            "turn_idle": False,
        }

    graph = Mock()
    graph.ainvoke = AsyncMock(side_effect=graph_return)
    host = AgentHost(pool=aops_pool, checkpointer=Mock(), graph=graph, machine="claim-test")
    host._runtimes[agent_id] = Mock()
    with bind_turn_identity(agent_id, incarnation=old):
        assert [row.id for row in await claim_inbound_batch(aops_pool, agent_id)] == [inbound]
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
        ).fetchone() == ("claimed",)
        task = asyncio.create_task(
            host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool, hosted=True))
        )
        await asyncio.wait_for(entered.wait(), 2)
        try:
            assert db_conn.execute(
                "SELECT runtime_generation,runtime_owner,status FROM agents_meta WHERE id=%s",
                (agent_id,),
            ).fetchone() == (old.generation, old.owner, "running")
            assert db_conn.execute(
                "SELECT applied_at,observed_at FROM inbound_messages WHERE id=%s", (inbound,)
            ).fetchone() == (None, None)
            assert agent_id in host._runtimes
        finally:
            release.set()
            await asyncio.wait_for(task, 3)
    assert agent_id not in host._runtimes
    record = db_conn.execute(
        "SELECT status,applied_at,observed_at FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone()
    assert record is not None and record[1] is not None
    if kind == "terminate":
        assert record[0] == "done" and record[2] is not None
    else:
        assert record[0] == "claimed" and record[2] is None
        assert not await settle_hosted_runtime(aops_pool, old)
        new = await admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", uuid4(), expected_from="idling"
        )
        assert new is not None and new.generation != old.generation
        assert db_conn.execute(
            "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (inbound,)
        ).fetchone() == ("done", True)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


async def test_stale_unapplied_pointer_closes_without_retargeting(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    old = await _admit(aops_pool, agent_id)
    first = _command(db_conn, agent_id, "restart")
    with bind_turn_identity(agent_id, incarnation=old):
        await claim_inbound_batch(aops_pool, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (agent_id,)
    )
    db_conn.commit()
    new = await admit_hosted_runtime(
        aops_pool, agent_id, "claim-test", uuid4(), expected_from="running"
    )
    assert new is not None
    second = _command(db_conn, agent_id, "restart")
    with bind_turn_identity(agent_id, incarnation=new):
        assert [row.id for row in await claim_inbound_batch(aops_pool, agent_id)] == [second]
    assert db_conn.execute(
        "SELECT status,applied_at,target_generation FROM inbound_messages WHERE id=%s", (first,)
    ).fetchone() == ("done", None, old.generation)


async def test_existing_pg_backstop_finds_accepted_command_without_pending_rows(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _command(db_conn, agent_id, "restart")
    with bind_turn_identity(agent_id, incarnation=owner):
        assert [row.id for row in await claim_inbound_batch(aops_pool, agent_id)] == [inbound]
    assert await settle_hosted_runtime(aops_pool, owner)
    host = AgentHost(pool=aops_pool, checkpointer=Mock(), graph=Mock(), machine="claim-test")
    wakes = await host.pending_inbound_wakes(stale_after_s=60)
    assert agent_id in [wake.agent_id for wake in wakes]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND status='pending'", (agent_id,)
    ).fetchone() == (0,)
