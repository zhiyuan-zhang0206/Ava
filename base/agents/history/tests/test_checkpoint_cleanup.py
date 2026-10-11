"""Tests for base/agents/history/checkpoint_cleanup.py.

These drive the REAL LangGraph AsyncPostgresSaver against the session's test
Postgres (the same saver prod uses), so the compaction-boundary stamps are
exercised against genuine `checkpoints` rows.

The `aops_pool` fixture mirrors prod's `agent/loop.py` db_pool (autocommit +
check_connection); `_clean_state` truncates the checkpoint tables per test.
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Any, TypedDict, cast

import psycopg
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import _messages_delta_reducer
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint_cleanup import mark_compact_boundary
from base.agents.history.closing_request import ClosingRequest

# Untyped fixtures and helper calls throughout: the call-site rules stay at warning for this file.
# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning


def _saver(pool: AsyncConnectionPool) -> AsyncPostgresSaver:
    # Same cast as prod (agent/loop.py): the saver opens every cursor with its
    # own dict_row factory, so the pool's default tuple rows never reach it —
    # the DictRow pool type is type-only.
    return AsyncPostgresSaver(
        conn=cast(AsyncConnectionPool[psycopg.AsyncConnection[DictRow]], pool)
    )


def _delta_app(saver: AsyncPostgresSaver, *, snapshot_frequency: int = 1000):
    """A minimal graph whose `messages` channel is a DeltaChannel — the write
    model under evaluation (tasks #3180/#3181) — so the retention tests below
    run against genuinely delta-shaped checkpoint rows. `snapshot_frequency`
    picks the cadence of `_DeltaSnapshot` rows; a small value lands the newest
    checkpoint ON a snapshot step (the review #6143 x2b shape)."""

    class S(TypedDict):
        messages: Annotated[
            list[AnyMessage],
            DeltaChannel(
                cast(Callable[[Any, Sequence[Any]], Any], _messages_delta_reducer),
                snapshot_frequency=snapshot_frequency,
            ),
        ]
        n: int
        target: int

    def step(state: S) -> dict[str, Any]:
        n = state["n"]
        return {
            "messages": [
                HumanMessage(id=f"u{n}", content=f"user {n}"),
                AIMessage(id=f"a{n}", content=f"reply {n} original"),
            ],
            "n": n + 1,
        }

    graph = StateGraph(S)
    graph.add_node("step", step)
    graph.add_edge(START, "step")

    def route(state: S) -> str:
        return "step" if state["n"] < state["target"] else END

    graph.add_conditional_edges("step", route)
    return graph.compile(checkpointer=saver)


def _config(thread_id: str, checkpoint_id: str | None = None) -> RunnableConfig:
    cfg: RunnableConfig = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if checkpoint_id is not None:
        cfg["configurable"]["checkpoint_id"] = checkpoint_id
    return cfg


async def _put_turns(pool: AsyncConnectionPool, thread_id: str, n_turns: int) -> list[str]:
    """Write `n_turns` checkpoints for `thread_id`, one per turn, the way the
    graph does: the `messages` channel grows by one HumanMessage each turn and
    gets a fresh blob version.

    Returns the checkpoint ids in write order (oldest first).
    """
    saver = _saver(pool)
    ids: list[str] = []
    parent: str | None = None
    msg_version = None
    messages: list = []
    for turn in range(n_turns):
        messages = [*messages, HumanMessage(content=f"turn {turn}")]
        msg_version = saver.get_next_version(msg_version, None)
        ckpt = empty_checkpoint()
        # The graph writes v4; empty_checkpoint() alone still defaults to v1.
        ckpt["v"] = 4
        ckpt_id = ckpt["id"]
        ckpt["channel_values"] = {"messages": messages}
        ckpt["channel_versions"] = {"messages": msg_version}
        await saver.aput(_config(thread_id, parent), ckpt, {}, {"messages": msg_version})
        ids.append(ckpt_id)
        parent = ckpt_id
    return ids


async def test_mark_compact_boundary_stamps_newest(aops_pool: AsyncConnectionPool) -> None:
    """`mark_compact_boundary` stamps the thread's NEWEST checkpoint (idempotent)
    — the full-snapshot record of the just-frozen pre-compact segment."""
    ids = await _put_turns(aops_pool, "1", 4)
    assert await mark_compact_boundary(aops_pool, "1") == ids[-1]  # names the stamped checkpoint
    await mark_compact_boundary(aops_pool, "1")  # idempotent

    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT checkpoint_id, metadata FROM checkpoints WHERE thread_id = %s",
            ("1",),
        )
        rows = await cur.fetchall()
    stamped = [r[0] for r in rows if (r[1] or {}).get("compact_boundary")]
    assert stamped == [ids[-1]]  # exactly the newest, stamped once


async def test_mark_compact_boundary_records_the_closing_request(
    aops_pool: AsyncConnectionPool,
) -> None:
    """The compaction LLM call's input size, model and instruction size ride in the boundary's
    metadata; a stamp without one leaves no anchor."""
    ids = await _put_turns(aops_pool, "1", 3)
    closing = ClosingRequest(input_tokens=4321, extra_tokens=77, model="m1")
    assert await mark_compact_boundary(aops_pool, "1", closing=closing) == ids[-1]
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT metadata FROM checkpoints WHERE thread_id = %s AND checkpoint_id = %s",
            ("1", ids[-1]),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0]["compact_boundary"] is True
    assert ClosingRequest.from_metadata(row[0]["compact_anchor"]) == closing

    other = await _put_turns(aops_pool, "2", 2)
    await mark_compact_boundary(aops_pool, "2", closing=None)
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT metadata FROM checkpoints WHERE thread_id = %s AND checkpoint_id = %s",
            ("2", other[-1]),
        )
        row = await cur.fetchone()
    assert row is not None and "compact_anchor" not in row[0]


async def test_tail_edit_keeps_the_replaced_copy_in_writes(
    aops_pool: AsyncConnectionPool,
) -> None:
    """A same-id tail edit is an append at the storage layer: the replaced copy
    stays in the stored writes (never-delete ruling, 2026-09-12, task #3180)."""
    saver = _saver(aops_pool)
    app: Any = _delta_app(saver)
    cfg = _config("delta-edit")
    await app.ainvoke({"messages": [], "n": 0, "target": 2}, cfg, recursion_limit=40)
    await app.aupdate_state(cfg, {"messages": [AIMessage(id="a1", content="reply 1 edited")]})

    state = await app.aget_state(cfg)
    assert [m.content for m in state.values["messages"]][-1] == "reply 1 edited"

    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT blob FROM checkpoint_writes WHERE thread_id = %s", ("delta-edit",)
        )
        blobs = [bytes(r[0]) for r in await cur.fetchall()]
    assert any(b"reply 1 original" in blob for blob in blobs)  # replaced copy kept
    assert any(b"reply 1 edited" in blob for blob in blobs)  # replacement present
