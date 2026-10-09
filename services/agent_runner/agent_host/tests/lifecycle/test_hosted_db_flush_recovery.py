"""Database failures at final flush and lifecycle commit cannot replay work."""

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import apply_hosted_lifecycle
from agent.ownership.tests.test_lifecycle_intent import _command
from agent.tests.claim.test_inbound_ownership import _admit, _agent
from base.agents.context import AvaContext
from base.config import settings
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host import host as host_module
from services.agent_runner.agent_host.tests.history.test_hosted_compact_failure import (
    _prepare_graph,
)


@pytest.mark.parametrize(
    "failure_site", ["flush", "after_flush", "before_lifecycle", "after_lifecycle"]
)
@pytest.mark.parametrize("command_kind", ["restart", "terminate"])
async def test_database_failure_after_graph_return_preserves_completed_work(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
    command_kind: str,
) -> None:
    agent = _agent(db_conn)
    incarnation = await _admit(
        aops_pool,
        agent,
    )
    replies: list[str] = []
    graph, saver, config, _history = await _prepare_graph(aops_pool, agent, 100, replies)
    command = (
        None if failure_site in ("flush", "after_flush") else _command(db_conn, agent, command_kind)
    )
    host = host_module.AgentHost(
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    ctx = AvaContext(
        ops_pool=aops_pool,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
    )
    # A real closed PostgreSQL connection supplies the I/O failure. Injection
    # selects only the boundary; checkpoint, graph and lifecycle transactions run.
    broken = await psycopg.AsyncConnection.connect(settings.data_plane.db_url)
    await broken.close()
    failed = False
    queued: int | None = None
    original_flush = host_module.flush_checkpoint
    invocation = AsyncMock(wraps=host_module.run_invocation_with_stall_guard)
    monkeypatch.setattr(host_module, "run_invocation_with_stall_guard", invocation)

    async def fail_flush_once(checkpointer: object, target: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            if failure_site == "after_flush":
                await original_flush(checkpointer, target)
            await broken.execute("SELECT 1")
        await original_flush(checkpointer, target)

    async def fail_lifecycle_once(
        pool: AsyncConnectionPool, token: RuntimeIncarnation, **kwargs: Any
    ) -> str | None:
        nonlocal failed, queued
        if not failed:
            failed = True
            queued = insert_inbound_message(
                db_conn,
                agent,
                "Next work",
                source="user",
                database=ctx.require_db(),
                bus=ctx.require_bus(),
            )
            if failure_site == "after_lifecycle":
                await apply_hosted_lifecycle(pool, token, **kwargs)
            await broken.execute("SELECT 1")
        return await apply_hosted_lifecycle(pool, token, **kwargs)

    if failure_site in ("flush", "after_flush"):
        monkeypatch.setattr(host_module, "flush_checkpoint", fail_flush_once)
    else:
        monkeypatch.setattr(host_module, "apply_hosted_lifecycle", fail_lifecycle_once)
    outcome = await host._invoke_until_done(
        agent,
        replace(ctx, original_incarnation=incarnation, hosted_resources=None, native_work=None),
    )
    assert outcome.exited == (command is not None and command_kind == "terminate")
    assert failed
    cold = await saver.aget(config)
    assert cold is not None
    assert replies == (["continued"] if command is None else [])
    assert invocation.await_count == (2 if command is None else 1)
    if command is None:
        assert cold["channel_values"]["halted"] is True
        assert (
            sum(message.text == "continued" for message in cold["channel_values"]["messages"]) == 1
        )
    else:
        # The original lifecycle receipt proves the outcome. A
        # released old owner cannot acknowledge a new generation or run a model.
        assert db_conn.execute(
            "SELECT status,applied_at IS NOT NULL,observed_at IS NOT NULL "
            "FROM inbound_messages WHERE id=%s",
            (command,),
        ).fetchone() == (
            "done" if command_kind == "terminate" else "claimed",
            True,
            command_kind == "terminate",
        )
        assert db_conn.execute(
            "SELECT status,lease_expires_at FROM agents_meta WHERE id=%s", (agent,)
        ).fetchone() == ("terminated" if command_kind == "terminate" else "idling", None)
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (queued,)
        ).fetchone() == ("pending",)
