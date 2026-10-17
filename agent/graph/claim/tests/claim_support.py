"""The claim-node test harness: a runtime around a fake LLM, a direct inbound INSERT of any kind, the thread config and the pool-visibility barrier."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

import meiva
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.packages.composer_commands import CommandCatalog
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY, ExtensionRegistry

__all__ = ["insert_inbound_kind", "make_runtime", "thread_config"]


def make_fake_llm(summary: str = "synthetic compaction summary") -> Any:
    """Mock LLM — bind_tools(...).ainvoke returns AIMessage(content=summary),
    matching the call shape of generate_summary (same tool binding as the main llm node)."""
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(return_value=AIMessage(content=summary))
    return llm


def make_runtime(
    *,
    ops_pool: AsyncConnectionPool | None = None,
    llm: Any | None = None,
    event_publisher: Any | None = None,
    extensions: ExtensionRegistry = EMPTY,
    agent_id: int | None = None,
    database_gate: ProcessDbGate,
) -> Runtime[MeivaContext]:
    """Assemble a claim Runtime borrowing the test SDK root's clients and machine.

    `ops_pool=None` takes the container early-return path;

    InboundCommitted SSE fan-out goes through `ctx.event_publisher.emit`; default to a MagicMock
    so the node's `assert ctx.event_publisher` passes; tests verifying InboundCommitted pass their own
    mock to assert `pub.emit.call_args_list`.

    """
    retained = meiva.context
    ctx = MeivaContext(
        clients=retained.clients,
        machine=retained.machine,
        original_incarnation=retained.original_incarnation,
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        ops_pool=ops_pool,
        llm=llm if llm is not None else make_fake_llm(),
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        clock_factory=Clock.from_settings,
        extensions=extensions,
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        commands=CommandCatalog(skills=list),
    )
    return Runtime(context=ctx)


def insert_inbound_kind(
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


async def await_inbound_visible(pool: AsyncConnectionPool, inbound_id: int) -> None:
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


def thread_config(tid: int) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": str(
                tid,
            )
        }
    }
