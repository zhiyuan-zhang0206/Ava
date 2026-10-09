"""Recovery sees control intent without claiming it or abandoning durable state."""

import asyncio
import time
from contextlib import AsyncExitStack
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.cluster.machine import machine_name
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.agents.spawn import create_agent_row
from services.agent_runner.agent_host.recovery import interrupt as recovery_interrupt
from services.agent_runner.agent_host.recovery.interrupt import RecoveryInterrupt


@pytest.mark.parametrize("kind", ["cancel", "terminate"])
async def test_pending_external_interrupt_shortens_backoff_without_claiming(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    kind: str,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent, _, _prompt_id, _attempt_id = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_name(),
        catalog=model_catalog,
        authority=config_authority,
    )
    incarnation = RuntimeIncarnation(agent, uuid4(), uuid4())
    command = insert_inbound_message(
        db_conn, agent, "", "user", kind=kind, bus=event_bus, database=database
    )
    db_conn.commit()
    interrupt = RecoveryInterrupt(aops_pool, incarnation, asyncio.Lock(), work=None)

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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent, _, _prompt_id, _attempt_id = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_name(),
        catalog=model_catalog,
        authority=config_authority,
    )
    interrupt = RecoveryInterrupt(
        aops_pool, RuntimeIncarnation(agent, uuid4(), uuid4()), asyncio.Lock(), work=None
    )
    checked = asyncio.Event()
    original = recovery_interrupt.has_pending_interrupt

    async def observe(
        pool: AsyncConnectionPool,
        agent_id: int,
        *,
        incarnation: RuntimeIncarnation | None,
        work: NativeWorkTarget | None,
    ) -> bool:
        result = await original(pool, agent_id, incarnation=incarnation, work=work)
        checked.set()
        return result

    monkeypatch.setattr(recovery_interrupt, "has_pending_interrupt", observe)
    monkeypatch.setattr(recovery_interrupt, "_POLL_INTERVAL_SECONDS", 0.01)
    waiter = asyncio.create_task(interrupt.wait_backoff(30))
    try:
        await asyncio.wait_for(checked.wait(), 1)
        command = insert_inbound_message(
            db_conn, agent, "", "user", kind="cancel", bus=event_bus, database=database
        )
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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent, _, _prompt_id, _attempt_id = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_name(),
        catalog=model_catalog,
        authority=config_authority,
    )
    insert_inbound_message(
        db_conn, agent, "", "self", kind="terminate", bus=event_bus, database=database
    )
    db_conn.commit()
    interrupt = RecoveryInterrupt(
        aops_pool, RuntimeIncarnation(agent, uuid4(), uuid4()), asyncio.Lock(), work=None
    )
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
        interrupt = RecoveryInterrupt(
            pool, RuntimeIncarnation(1, uuid4(), uuid4()), asyncio.Lock(), work=None
        )
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
        interrupt = RecoveryInterrupt(
            pool, RuntimeIncarnation(1, uuid4(), uuid4()), asyncio.Lock(), work=None
        )
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

    async def held_read(
        pool: AsyncConnectionPool,
        agent_id: int,
        *,
        incarnation: RuntimeIncarnation | None,
        work: NativeWorkTarget | None,
    ) -> bool:
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
        peek_lock = asyncio.Lock()
        first = RecoveryInterrupt(
            pool, RuntimeIncarnation(1, uuid4(), uuid4()), peek_lock, work=None
        )
        second = RecoveryInterrupt(
            pool, RuntimeIncarnation(2, uuid4(), uuid4()), peek_lock, work=None
        )
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
