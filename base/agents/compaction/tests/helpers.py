"""Actual native owner, ended work and cold persisted source for compact storage tests."""

from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.impersonation import flush_checkpoint
from base.agents.compaction.commands import observe
from base.agents.compaction.models import CompactTarget
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host.invocation.compact.source import produce_source
from services.agent_runner.agent_host.tests.history.test_hosted_compact_failure import (
    _prepare_graph,
)
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work


async def source(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> tuple[Any, ...]:
    incarnation, work = await managed_work(conn, pool, database_gate=database_gate)
    graph, saver, config, history = await _prepare_graph(pool, work.agent_id, 1, [])
    await graph.aupdate_state(
        config,
        {
            "native_work": work,
            "turn_idle": True,
            "turn_active": False,
            "halted": True,
        },
        as_node="claim",
    )
    await flush_checkpoint(saver, work.agent_id)
    conn.execute(
        "UPDATE native_graph_work SET phase='settled',ended_at=now() WHERE id=%s", (work.work_id,)
    )
    conn.execute(
        "UPDATE agents_meta SET status='idling',config_overlay=%s WHERE id=%s",
        ('{"llm_model":"gpt-6.1-sol"}', work.agent_id),
    )
    conn.commit()
    assert not settings.lm.llm_override
    await produce_source(
        pool,
        saver,
        work.agent_id,
        incarnation.owner,
        HostedTurnResources(),
        catalog=model_catalog,
        llm_override=config_authority.runtime.lm.llm_override,
        default_reader=lambda _domain, field: config_authority.service_field_value(field),
    )
    with ConnectionPool[psycopg.Connection](conn.info.dsn) as sync_pool:
        target: CompactTarget = observe(sync_pool, work.agent_id)
    return incarnation, target, graph, saver, config, history
