"""The claim node refuses an unknown inbound kind, and a pending newer inbound vetoes a terminate."""

import psycopg
import pytest
from langchain_core.messages import SystemMessage
from langgraph.graph import END
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.state import AgentState
from agent.tests.claim.claim_support import (
    _await_inbound_visible,
    _config,
    _insert_inbound_kind,
    _make_runtime,
)
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from tests.fixtures.units import spawn_agent


async def test_claim_unknown_kind_raises(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
):
    """Unrecognized inbound kind = framework / DB schema desync — immediately raise,
    do not silently swallow bugs by 'defaulting to chat processing'.

    The DB CHECK constraint prevents production unknown kind, so it cannot be constructed
    via INSERT path; use monkeypatch to directly feed ClaimedInbound to claim_node to verify
    the dispatch's `case _:` fallback branch.
    """
    from agent.db import ClaimedInbound

    tid = spawn_agent(catalog=model_catalog, authority=config_authority)
    runtime = _make_runtime(ops_pool=aops_pool)

    async def fake_claim(
        _db: object,
        _tid: int,
        *,
        incarnation: RuntimeIncarnation | None,
        work: NativeWorkTarget | None,
        lifecycle_only: bool = False,
    ):
        assert incarnation is runtime.context.original_incarnation
        assert work is runtime.context.native_work
        assert not lifecycle_only
        return [ClaimedInbound(id=99, agent_id=tid, content="x", kind="bogus", source="system")]

    monkeypatch.setattr("agent.graph.claim.node.claim_inbound_batch", fake_claim)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(ValueError, match="Unknown inbound kind"):
        await claim_node(
            AgentState(),
            runtime,
            _config(
                tid,
            ),
        )


async def test_claim_terminate_vetoed_by_pending_inbound_after_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
):
    """The other half of the race: the batch (terminate alone) is claimed, but a
    message lands in the queue before the exit is committed. The claim node's
    final recheck must veto the death and re-enter claim so the fresh message is
    dispatched — the terminate row is already consumed ('done'), no marker, no
    END, and the chat stays pending for the re-entered claim to pick up. The
    message arrives after claim_inbound_batch returns, which is simulated by
    monkeypatching claim to return only the terminate row while a newer chat
    stays pending in the table."""
    from agent.db import ClaimedInbound

    tid = spawn_agent(catalog=model_catalog, authority=config_authority)
    terminate_id = _insert_inbound_kind(db_conn, tid, "", "terminate", source="self")
    chat_id = insert_inbound_message(
        db_conn, tid, "message after the claim", source="user", bus=event_bus, database=database
    )
    await _await_inbound_visible(aops_pool, chat_id)
    runtime = _make_runtime(ops_pool=aops_pool)

    async def fake_claim(
        _pool: AsyncConnectionPool,
        _agent_id: int,
        *,
        incarnation: RuntimeIncarnation | None,
        work: NativeWorkTarget | None,
        lifecycle_only: bool = False,
    ):
        assert incarnation is runtime.context.original_incarnation
        assert work is runtime.context.native_work
        assert not lifecycle_only
        # Faithful to claim_inbound_batch: the grab marks lifecycle rows 'done'
        # atomically, so the vetoed terminate is consumed and never retried.
        async with _pool.connection() as conn, conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            await cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "UPDATE inbound_messages SET status = 'done' WHERE id = %s", (terminate_id,)
            )
        return [
            ClaimedInbound(
                id=terminate_id, agent_id=tid, content="", kind="terminate", source="self"
            )
        ]

    monkeypatch.setattr("agent.graph.claim.node.claim_inbound_batch", fake_claim)  # pyright: ignore[reportUnknownArgumentType]

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        runtime,
        _config(
            tid,
        ),
    )

    # re-enter claim to dispatch the fresh message — not END, not a wake
    assert cmd.goto == "claim"
    assert cmd.goto != END
    # no terminate marker was committed
    assert cmd.update["messages"] == []  # type: ignore[index]
    # the newer chat is still pending for the re-entered claim to pick up
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = %s AND kind = 'chat'", (tid,)
        )
        chat_row = cur.fetchone()
        assert chat_row is not None
        assert chat_row[0] == "pending"
    # the vetoed terminate row is consumed, never retried
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = %s AND kind = 'terminate'", (tid,)
        )
        term_row = cur.fetchone()
        assert term_row is not None
        assert term_row[0] == "done"


async def test_claim_same_batch_newer_chat_vetoes_the_terminate(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
):
    """Veto half 1: a same-batch chat newer than the terminate keeps the agent
    alive."""
    from agent.db import ClaimedInbound

    tid = spawn_agent(catalog=model_catalog, authority=config_authority)
    terminate_id = _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    chat_id = insert_inbound_message(
        db_conn, tid, "message in the batch", source="user", bus=event_bus, database=database
    )
    await _await_inbound_visible(aops_pool, chat_id)
    runtime = _make_runtime(ops_pool=aops_pool)

    async def fake_claim(
        _pool: AsyncConnectionPool,
        _agent_id: int,
        *,
        incarnation: RuntimeIncarnation | None,
        work: NativeWorkTarget | None,
        lifecycle_only: bool = False,
    ):
        assert incarnation is runtime.context.original_incarnation
        assert work is runtime.context.native_work
        assert not lifecycle_only
        return [
            ClaimedInbound(
                id=terminate_id, agent_id=tid, content="", kind="terminate", source="user"
            ),
            ClaimedInbound(
                id=chat_id,
                agent_id=tid,
                content="message in the batch",
                kind="chat",
                source="user",
            ),
        ]

    monkeypatch.setattr("agent.graph.claim.node.claim_inbound_batch", fake_claim)  # pyright: ignore[reportUnknownArgumentType]

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        runtime,
        _config(
            tid,
        ),
    )

    assert cmd.goto != END
