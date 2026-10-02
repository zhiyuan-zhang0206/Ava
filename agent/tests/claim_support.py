"""The claim-node test harness: a runtime around a fake LLM, a direct inbound INSERT of any kind, the thread config and the pool-visibility barrier."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from base.agents.context import AvaContext
from base.agents.context.slices import AgentSlices


def _fake_llm(summary: str = "synthetic compaction summary") -> Any:
    """Mock LLM — bind_tools(...).ainvoke returns AIMessage(content=summary),
    matching the call shape of generate_summary (same tool binding as the main llm node)."""
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(return_value=AIMessage(content=summary))
    return llm


def _make_runtime(
    *,
    ops_pool: AsyncConnectionPool | None = None,
    llm: Any | None = None,
    event_publisher: Any | None = None,
) -> Runtime[AvaContext]:
    """test helper: assemble AvaContext into Runtime.

    `ops_pool=None` takes the container early-return path;

    InboundCommitted SSE fan-out goes through `ctx.event_publisher.emit`; default to a MagicMock
    so the node's `assert ctx.event_publisher` passes; tests verifying InboundCommitted pass their own
    mock to assert `pub.emit.call_args_list`.

    """
    ctx = AvaContext(
        ops_pool=ops_pool,
        llm=llm if llm is not None else _fake_llm(),
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),
        agent=AgentSlices.resolve(),
    )
    return Runtime(context=ctx)


def _insert_inbound_kind(
    db: psycopg.Connection, tid: int, content: str, kind: str, source: str = "system"
) -> int:
    """Directly INSERT an inbound of any kind (bypasses the chat-only helper in base/db/__init__.py)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tid, content, kind, source),
        )
        new_id = cur.fetchone()[0]  # type: ignore[index]
    db.commit()
    return new_id


async def _await_inbound_visible(pool: AsyncConnectionPool, inbound_id: int) -> None:
    """Block until a `db_conn`-committed inbound row is visible on `pool`.

    Setup writes go through the sync `db_conn`; `claim_node` claims the batch
    through `aops_pool` (a different connection). Under `-n auto` there is a
    cross-connection window where a just-committed row is not yet visible on the
    pool, so a single `claim_node` call can read a partial batch and skip the
    lifecycle flip — surfacing later as a baffling `assert 'idling' ==
    'restarting'`. Prod never hits this: claim is Redis-pub/sub-driven and re-claims
    on the next wake, so the still-pending row is picked up. This barrier mirrors
    that guarantee for the test's one-shot call. Waiting on the LAST-committed
    setup row suffices: `db_conn` commits sequentially, so its visibility implies
    every earlier setup write (status, prior inbounds) is visible too.
    """
    for _ in range(100):
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM inbound_messages WHERE id = %s", (inbound_id,))
            if await cur.fetchone() is not None:
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"inbound {inbound_id} not visible on the claim pool after 2s")


def _config(tid: int) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": str(
                tid,
            )
        }
    }
