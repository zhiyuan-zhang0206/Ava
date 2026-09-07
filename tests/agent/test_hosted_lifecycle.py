"""Hosted application waits for graph return, then uses the durable command."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import ClaimedInbound, claim_inbound_batch
from agent.graph._claim_dispatch import _BatchState, _handle_restart
from agent.hosted_ownership import (
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    settle_hosted_runtime,
)
from services.agent_host.host import AgentHost
from shared.config import settings
from shared.context import AvaContext
from shared.db import PG_KEEPALIVE_KWARGS
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity
from tests.agent.test_inbound_ownership import _admit, _agent
from tests.agent.test_lifecycle_intent import _command


async def test_hosted_restart_marker_does_not_claim_completion() -> None:
    state = _BatchState()
    await _handle_restart(
        AvaContext(),
        1,
        ClaimedInbound(id=1, agent_id=1, content="", kind="restart", source="self", payload={}),
        state,
    )
    assert state.restart_requested
    assert isinstance(state.new_msgs[0], HumanMessage)
    assert (
        state.new_msgs[0].model_dump()["additional_kwargs"]["ava_note_tag"] == "lifecycle_restart"
    )
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
            host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))
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


@pytest.mark.parametrize("crash", ["after_cache_drop", "before_observe", "after_commit"])
async def test_hosted_terminate_crash_has_no_applied_unobserved_gap(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    crash: str,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _command(db_conn, agent_id, "terminate")
    graph = Mock()
    graph.ainvoke = AsyncMock(
        return_value={"exit_requested": True, "restart_requested": False, "turn_idle": False}
    )
    host = AgentHost(pool=aops_pool, checkpointer=Mock(), graph=graph, machine="claim-test")
    host._runtimes[agent_id] = Mock()
    original_execute = psycopg.AsyncConnection.execute
    original_drop = host.drop_agent

    async def fail_observe(
        conn: psycopg.AsyncConnection, query: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if "UPDATE inbound_messages SET observed_at=" in str(query):
            raise RuntimeError("injected observation crash")
        return await original_execute(conn, query, *args, **kwargs)

    def fail_drop(target: int) -> None:
        original_drop(target)
        raise RuntimeError("injected cache crash")

    async def fail_after_commit(pool: AsyncConnectionPool, token: RuntimeIncarnation) -> str | None:
        await apply_hosted_lifecycle(pool, token)
        raise RuntimeError("injected post-commit crash")

    with bind_turn_identity(agent_id, incarnation=owner):
        await claim_inbound_batch(aops_pool, agent_id)
        with monkeypatch.context() as patch:
            if crash == "after_cache_drop":
                patch.setattr(host, "drop_agent", fail_drop)
            elif crash == "before_observe":
                patch.setattr(psycopg.AsyncConnection, "execute", fail_observe)
            else:
                patch.setattr("services.agent_host.host.apply_hosted_lifecycle", fail_after_commit)
            with pytest.raises(RuntimeError, match="injected"):
                await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))
        state = db_conn.execute(
            "SELECT status,applied_at IS NOT NULL,observed_at IS NOT NULL "
            "FROM inbound_messages WHERE id=%s",
            (inbound,),
        ).fetchone()
        assert state == (
            ("done", True, True) if crash == "after_commit" else ("claimed", False, False)
        )
        db_conn.commit()
        if crash != "after_commit":
            # Same admitted continuation can retry; cache absence is not a new owner.
            assert await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))
    assert db_conn.execute(
        "SELECT lifecycle_command_id,status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None, "terminated")
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("done", True)


@pytest.mark.parametrize("applied", [False, True])
async def test_hosted_force_cannot_be_undone_by_prior_restart(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, applied: bool
) -> None:
    from ops.ops_exit import _force_terminate_transaction

    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    first = _command(db_conn, agent_id, "restart")
    with bind_turn_identity(agent_id, incarnation=owner):
        await claim_inbound_batch(aops_pool, agent_id)
        if applied:
            assert await apply_hosted_lifecycle(aops_pool, owner) == "restart"
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        _, _, _, force = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    later = _command(db_conn, agent_id, "restart")
    assert await apply_hosted_lifecycle(aops_pool, owner) is None
    assert (
        await admit_hosted_runtime(
            aops_pool, agent_id, "claim-test", uuid4(), expected_from="idling"
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
