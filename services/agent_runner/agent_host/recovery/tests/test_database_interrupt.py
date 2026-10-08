"""Database recovery observes control without claiming or replaying it."""

import asyncio
from typing import Any

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent import state as states
from base.agents.observation.db_wait import DatabaseWaits
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.native_process.turn_identity import bind_turn_identity
from services.agent_runner.agent_host import db_recovery
from services.agent_runner.agent_host.recovery.tests.test_hosted_db_recovery import _admit, _graph


@pytest.mark.parametrize("persistent_failure", [False, True])
async def test_recovery_retries_promptly_but_does_not_execute_or_ack_control(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    persistent_failure: bool,
    database: Database,
    event_bus: EventBus,
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id

    async def never(_state: states.AgentState) -> dict[str, Any]:
        raise AssertionError("observing cancel must not replay the checkpoint's work")

    graph, saver = await _graph(aops_pool, agent, never)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    before = await saver.aget(config)
    command = insert_inbound_message(
        db_conn, agent, "", "user", kind="cancel", bus=event_bus, database=database
    )
    db_conn.commit()
    flush = db_recovery.flush_checkpoint
    attempts = 0
    retried = asyncio.Event()
    repair_tasks: set[int] = set()

    async def unavailable_once(checkpointer: object, agent_id: int) -> None:
        nonlocal attempts
        attempts += 1
        repair_tasks.add(id(asyncio.current_task()))
        if attempts == 2:
            retried.set()
        if attempts == 1 or persistent_failure:
            raise PoolTimeout("checkpoint temporarily unavailable")
        await flush(checkpointer, agent_id)

    monkeypatch.setattr(db_recovery, "flush_checkpoint", unavailable_once)
    monkeypatch.setattr(db_recovery, "_INITIAL_BACKOFF_SECONDS", 0.15 if persistent_failure else 30)
    with bind_turn_identity(agent, incarnation=incarnation):
        original = asyncio.create_task(
            db_recovery.recover_database(
                pool=aops_pool,
                checkpointer=saver,
                graph=graph,
                incarnation=incarnation,
                database_waits=DatabaseWaits(),
                peek_lock=asyncio.Lock(),
            )
        )
    try:
        if persistent_failure:
            await asyncio.wait_for(retried.wait(), 1)
            await asyncio.sleep(0.04)
            assert not original.done()
        else:
            await asyncio.wait_for(original, 1)
    finally:
        if not original.done():
            original.cancel()
            with pytest.raises(asyncio.CancelledError):
                await original
    assert attempts == 2
    assert repair_tasks == {id(original)}
    assert await saver.aget(config) == before
    assert db_conn.execute(
        "SELECT status,claimed_at,applied_at,observed_at FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone() == ("pending", None, None, None)
