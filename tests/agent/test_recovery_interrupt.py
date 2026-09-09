"""Recovery sees control intent without claiming it or abandoning durable state."""

import asyncio
import time
from contextlib import AsyncExitStack
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent import state as states
from ops.agent_spawn import create_agent_row
from services.agent_host import db_recovery, recovery_interrupt
from services.agent_host.recovery_interrupt import RecoveryInterrupt
from shared.config import settings
from shared.db import insert_inbound_message
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity
from tests.agent.test_hosted_db_recovery import _admit, _graph


@pytest.mark.parametrize("kind", ["cancel", "terminate"])
async def test_pending_external_interrupt_shortens_backoff_without_claiming(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, kind: str
) -> None:
    agent, _ = create_agent_row(spawner="user", machine=machine_name())
    incarnation = RuntimeIncarnation(agent, uuid4(), uuid4())
    command = insert_inbound_message(db_conn, agent, "", "user", kind=kind)
    db_conn.commit()
    interrupt = RecoveryInterrupt(aops_pool, incarnation)

    await asyncio.wait_for(interrupt.wait_backoff(30), timeout=1)

    assert db_conn.execute(
        "SELECT status,claimed_at,applied_at,observed_at FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone() == ("pending", None, None, None)

    # The same durable intent cannot accelerate every failing retry.
    started = time.monotonic()
    await interrupt.wait_backoff(0.04)
    assert time.monotonic() - started >= 0.035


async def test_interrupt_arriving_during_backoff_is_observed(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _ = create_agent_row(spawner="user", machine=machine_name())
    interrupt = RecoveryInterrupt(aops_pool, RuntimeIncarnation(agent, uuid4(), uuid4()))
    checked = asyncio.Event()
    original = recovery_interrupt.has_pending_interrupt

    async def observe(pool: AsyncConnectionPool, agent_id: int) -> bool:
        result = await original(pool, agent_id)
        checked.set()
        return result

    monkeypatch.setattr(recovery_interrupt, "has_pending_interrupt", observe)
    monkeypatch.setattr(recovery_interrupt, "_POLL_INTERVAL_SECONDS", 0.01)
    waiter = asyncio.create_task(interrupt.wait_backoff(30))
    try:
        await asyncio.wait_for(checked.wait(), 1)
        command = insert_inbound_message(db_conn, agent, "", "user", kind="cancel")
        db_conn.commit()
        await asyncio.wait_for(waiter, 1)
    finally:
        if not waiter.done():
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("pending",)


async def test_self_control_does_not_shorten_backoff(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent, _ = create_agent_row(spawner="user", machine=machine_name())
    insert_inbound_message(db_conn, agent, "", "self", kind="terminate")
    db_conn.commit()
    interrupt = RecoveryInterrupt(aops_pool, RuntimeIncarnation(agent, uuid4(), uuid4()))
    started = time.monotonic()
    await interrupt.wait_backoff(0.04)
    assert time.monotonic() - started >= 0.035


async def test_unavailable_control_pool_does_not_extend_backoff_or_leak_borrowers() -> None:
    async with AsyncConnectionPool[psycopg.AsyncConnection](
        settings.data_plane.db_url,
        min_size=1,
        max_size=1,
        kwargs={"autocommit": True},
    ) as pool:
        interrupt = RecoveryInterrupt(pool, RuntimeIncarnation(1, uuid4(), uuid4()))
        async with pool.connection():
            started = time.monotonic()
            await asyncio.wait_for(interrupt.wait_backoff(0.04), 0.5)
            assert 0.035 <= time.monotonic() - started < 0.5
        async with pool.connection(timeout=0.1) as conn:
            assert await (await conn.execute("SELECT 1")).fetchone() == (1,)


async def test_external_cancellation_unwinds_the_inline_control_query() -> None:
    async with AsyncConnectionPool[psycopg.AsyncConnection](
        settings.data_plane.db_url,
        min_size=1,
        max_size=1,
        kwargs={"autocommit": True},
    ) as pool:
        interrupt = RecoveryInterrupt(pool, RuntimeIncarnation(1, uuid4(), uuid4()))
        async with pool.connection():
            waiter = asyncio.create_task(interrupt.wait_backoff(30))
            await asyncio.sleep(0.01)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiter, 0.5)
        async with pool.connection(timeout=0.1) as conn:
            assert await (await conn.execute("SELECT 1")).fetchone() == (1,)


async def test_optional_observers_share_one_pool_slot_without_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    queried_agents: list[int] = []

    async def held_read(pool: AsyncConnectionPool, agent_id: int) -> bool:
        queried_agents.append(agent_id)
        async with pool.connection():
            entered.set()
            await release.wait()
        return True

    monkeypatch.setattr(recovery_interrupt, "has_pending_interrupt", held_read)
    async with AsyncConnectionPool[psycopg.AsyncConnection](
        settings.data_plane.db_url,
        min_size=4,
        max_size=4,
        kwargs={"autocommit": True},
    ) as pool:
        await pool.wait()
        first = RecoveryInterrupt(pool, RuntimeIncarnation(1, uuid4(), uuid4()))
        second = RecoveryInterrupt(pool, RuntimeIncarnation(2, uuid4(), uuid4()))
        waiting = asyncio.create_task(first.wait_backoff(30))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(second.wait_backoff(0.04), 0.5)
            assert queried_agents == [1]
            assert not waiting.done()
            # Optional peeks leave three of the reserved pool's four connections
            # available for ownership renewal, lifecycle and durable scans.
            async with AsyncExitStack() as controls:
                for _ in range(3):
                    conn = await controls.enter_async_context(pool.connection(timeout=0.1))
                    assert await (await conn.execute("SELECT 1")).fetchone() == (1,)
        finally:
            release.set()
            await asyncio.wait_for(waiting, 1)


@pytest.mark.parametrize("persistent_failure", [False, True])
async def test_recovery_retries_promptly_but_does_not_execute_or_ack_control(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    persistent_failure: bool,
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id

    async def never(_state: states.AgentState) -> dict[str, Any]:
        raise AssertionError("observing cancel must not replay the checkpoint's work")

    graph, saver = await _graph(aops_pool, agent, never)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    before = await saver.aget(config)
    command = insert_inbound_message(db_conn, agent, "", "user", kind="cancel")
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
                pool=aops_pool, checkpointer=saver, graph=graph, incarnation=incarnation
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
