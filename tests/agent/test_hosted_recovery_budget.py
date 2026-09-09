"""Reachable PostgreSQL must not make a slow, valid checkpoint repair starve."""

import asyncio
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from services.agent_host import db_recovery
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity
from tests.agent.test_hosted_db_recovery import _admit, _graph


async def test_checkpoint_repair_can_outlast_the_short_probe(
    aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    incarnation = await _admit(aops_pool)
    graph, saver = await _graph(aops_pool, incarnation.agent_id, lambda _: {})
    flushes = 0
    flush = db_recovery.flush_checkpoint

    async def slow_flush(*args: Any) -> None:
        nonlocal flushes
        flushes += 1
        # A valid checkpoint operation longer than the production 5s probe.
        await asyncio.sleep(5.1)
        await flush(*args)

    monkeypatch.setattr(db_recovery, "flush_checkpoint", slow_flush)
    with bind_turn_identity(incarnation.agent_id, incarnation=incarnation):
        await asyncio.wait_for(
            db_recovery.recover_database(
                pool=aops_pool, checkpointer=saver, graph=graph, incarnation=incarnation
            ),
            8,
        )
    assert flushes == 1


async def test_initial_owner_probe_does_not_consume_the_repair_budget(
    aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    incarnation = await _admit(aops_pool)
    graph, saver = await _graph(aops_pool, incarnation.agent_id, lambda _: {})
    refresh = db_recovery._refresh_owner
    probes = 0

    async def slow_initial_probe(pool: AsyncConnectionPool, owner: RuntimeIncarnation) -> None:
        nonlocal probes
        probes += 1
        if probes == 1:
            await asyncio.sleep(0.15)
        await refresh(pool, owner)

    monkeypatch.setattr(db_recovery, "_refresh_owner", slow_initial_probe)
    monkeypatch.setattr(db_recovery, "_RECOVERY_TIMEOUT_SECONDS", 0.1)
    with bind_turn_identity(incarnation.agent_id, incarnation=incarnation):
        await asyncio.wait_for(
            db_recovery.recover_database(
                pool=aops_pool, checkpointer=saver, graph=graph, incarnation=incarnation
            ),
            0.8,
        )
    assert probes == 3
