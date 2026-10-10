"""A completed idle invocation settles before a newly queued chat is claimed."""

from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import flush_checkpoint
from agent.tests.claim.test_inbound_ownership import _admit, _agent
from base.agents.context import AvaContext
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host import host as host_module
from services.agent_runner.agent_host import invocation as invocation_owner
from services.agent_runner.agent_host.invocation import PendingWorkResult
from services.agent_runner.agent_host.invocation.checkpoints import TurnCheckpoints
from services.agent_runner.agent_host.tests.history.test_hosted_compact_failure import (
    _prepare_graph,
)
from services.agent_runner.agent_host.tests.host_policy import configured_policy


@pytest.mark.parametrize("site", ["before_flush", "after_flush", "before_idle", "after_idle"])
async def test_completed_idle_result_does_not_claim_next_chat_during_recovery(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    model_catalog: ModelCatalog,
) -> None:
    agent = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent)
    replies: list[str] = []
    graph, saver, config, _history = await _prepare_graph(aops_pool, agent, 100, replies)
    await graph.aupdate_state(config, {"halted": True}, as_node="claim")
    await flush_checkpoint(saver, agent)
    host = host_module.AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    ctx = AvaContext(
        ops_pool=aops_pool,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        catalog=model_catalog,
    )
    original_invoke = host_module.run_invocation_with_stall_guard
    original_flush = invocation_owner.flush_checkpoint
    original_settle = invocation_owner.settle_checkpoint
    invocations = 0
    queued: int | None = None

    async def counted_invoke(*args: Any, **kwargs: Any) -> Any:
        nonlocal invocations
        invocations += 1
        return await original_invoke(*args, **kwargs)

    async def fail_once() -> None:
        nonlocal queued
        queued = insert_inbound_message(
            db_conn,
            agent,
            "This is the next work",
            source="user",
            database=ctx.require_db(),
            bus=ctx.require_bus(),
        )
        broken = await psycopg.AsyncConnection.connect(db_conn.info.dsn)
        await broken.close()
        await broken.execute("SELECT 1")

    async def flushing(checkpointer: object, target: int) -> None:
        if queued is None and site == "before_flush":
            await fail_once()
        await original_flush(checkpointer, target)
        if queued is None and site == "after_flush":
            await fail_once()

    async def settling(*args: Any, **kwargs: Any) -> bool:
        # Initial preparation disables activation; only the returned idle
        # invocation enters its post-flush settlement with activation enabled.
        idle_boundary = kwargs.get("activate_accepted", True)
        if idle_boundary and queued is None and site == "before_idle":
            await fail_once()
        result = await original_settle(*args, **kwargs)
        if idle_boundary and queued is None and site == "after_idle":
            await fail_once()
        return result

    monkeypatch.setattr(host_module, "run_invocation_with_stall_guard", counted_invoke)
    monkeypatch.setattr(invocation_owner, "flush_checkpoint", flushing)
    monkeypatch.setattr(invocation_owner, "settle_checkpoint", settling)
    outcome = await host._invoke_until_done(
        agent,
        replace(ctx, original_incarnation=incarnation, hosted_resources=None, native_work=None),
    )
    assert not outcome.crashed
    assert queued is not None
    assert invocations == 1
    assert replies == []
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (queued,)
    ).fetchone() == ("pending",)
    cold = await saver.aget(config)
    assert cold is not None and cold["channel_values"]["halted"] is True


async def test_missing_lifecycle_pointer_still_invalidates_cached_runtime(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, model_catalog: ModelCatalog
) -> None:
    agent = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent)
    graph, saver, _config, _history = await _prepare_graph(aops_pool, agent, 100, [])
    host = host_module.AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    ctx = AvaContext(
        ops_pool=aops_pool,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        catalog=model_catalog,
    )
    host._runtimes[agent] = MagicMock()
    # A forced or superseded lifecycle return can carry its graph flag while
    # the original pointer is already absent. It still invalidates the cache,
    # but does not prove that termination was applied.
    pending = PendingWorkResult(
        {"exit_requested": True, "restart_requested": False},
        checkpoint_flushed=True,
        trace_attached=True,
    )
    outcome = await invocation_owner.finish_completed_invocation(
        aops_pool,
        TurnCheckpoints(saver, graph),
        agent,
        replace(ctx, original_incarnation=incarnation, hosted_resources=None, native_work=None),
        pending,
        host.drop_agent,
        host_module.kill_terminating_agent_shells,
        db=ctx.require_db(),
        bus=ctx.require_bus(),
        relays=host.relays,
    )
    assert outcome is not None and not outcome.exited
    assert agent not in host._runtimes
    assert pending.lifecycle_command_id is None
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "running",
    )
