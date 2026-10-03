"""Database failures at final flush and lifecycle commit cannot replay work."""

from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import apply_hosted_lifecycle
from agent.ownership.inbound import RuntimeOwnershipLostError
from agent.ownership.tests.test_lifecycle_intent import _command
from agent.tests.test_inbound_ownership import _admit, _agent
from base.agents.context import AvaContext
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import bind_turn_identity
from services.agent_host import host as host_module
from services.agent_host.tests.test_hosted_compact_failure import _prepare_graph


@pytest.mark.parametrize("failure_site", ["flush", "before_lifecycle", "after_lifecycle"])
async def test_database_failure_after_graph_return_preserves_completed_work(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    agent = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent)
    replies: list[str] = []
    graph, saver, config, _history = await _prepare_graph(aops_pool, agent, 100, replies)
    command = None if failure_site == "flush" else _command(db_conn, agent, "restart")
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
    original_flush = host_module.flush_checkpoint

    async def fail_flush_once(checkpointer: object, target: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            await broken.execute("SELECT 1")
        await original_flush(checkpointer, target)

    async def fail_lifecycle_once(
        pool: AsyncConnectionPool, token: RuntimeIncarnation, **kwargs: Any
    ) -> str | None:
        nonlocal failed
        if not failed:
            failed = True
            if failure_site == "after_lifecycle":
                await apply_hosted_lifecycle(pool, token, **kwargs)
            await broken.execute("SELECT 1")
        return await apply_hosted_lifecycle(pool, token, **kwargs)

    if failure_site == "flush":
        monkeypatch.setattr(host_module, "flush_checkpoint", fail_flush_once)
    else:
        monkeypatch.setattr(host_module, "apply_hosted_lifecycle", fail_lifecycle_once)
    with bind_turn_identity(agent, incarnation=incarnation):
        if failure_site == "after_lifecycle":
            with pytest.raises(RuntimeOwnershipLostError, match="lost authority"):
                await host._invoke_until_done(agent, ctx)
        else:
            assert not (await host._invoke_until_done(agent, ctx)).exited
    assert failed
    cold = await saver.aget(config)
    assert cold is not None
    assert replies == (["continued"] if failure_site == "flush" else [])
    if command is None:
        assert cold["channel_values"]["halted"] is True
        assert (
            sum(message.text == "continued" for message in cold["channel_values"]["messages"]) == 1
        )
    else:
        # The successful or uncertain commit remains one durable restart. A
        # released old owner cannot acknowledge a new generation or run a model.
        assert db_conn.execute(
            "SELECT status,applied_at IS NOT NULL,observed_at FROM inbound_messages WHERE id=%s",
            (command,),
        ).fetchone() == ("claimed", True, None)
        assert db_conn.execute(
            "SELECT status,lease_expires_at FROM agents_meta WHERE id=%s", (agent,)
        ).fetchone() == ("idling", None)
