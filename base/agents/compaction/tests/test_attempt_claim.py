"""Concurrent original attempt claims never authorize two provider invocations."""

import asyncio

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.agents.compaction.commands import accept
from base.agents.compaction.execution import claim_attempt, pending
from base.agents.compaction.tests.helpers import source
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog


async def test_concurrent_claim_returns_exact_one_first_provider_authority(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> None:
    incarnation, target, *_ = await source(
        db_conn, aops_pool, config_authority=config_authority, model_catalog=model_catalog
    )
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        accept(pool, str(target.observation_id), target.source.agent_id, target)
    command = await pending(aops_pool, target.source.agent_id)
    assert command is not None
    claims = await asyncio.gather(
        *[claim_attempt(aops_pool, command, incarnation, provider_key="gpt") for _ in range(4)]
    )
    assert sum(fresh for _, fresh in claims) == 1
    assert all(claim == claims[0][0] for claim, _ in claims)
    assert claims[0][0].attempt_id is not None and claims[0][0].execution is not None
    assert claims[0][0].execution.work_id != target.source.work_id
    assert claims[0][0].attempt_provider == "gpt"
