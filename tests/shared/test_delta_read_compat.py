"""Tests for shared/delta_read_compat.py — delta read-compat reconstruction.

Real delta-written threads against the session's test Postgres: the transition
layer must let plain (vanilla) readers see the same history the delta runtime
reconstructs, must leave vanilla-written threads untouched, and must self-heal
the store on the first vanilla write. Fork copying and the startup inbound
reconciler are covered end to end (review #6143 I2/I3a — task #3180/#3181).
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Any, TypedDict, cast

import psycopg
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from agent.messages_guard import guarded_delta_reducer
from agent.startup import _reconcile_claimed_inbounds_at_startup
from ops.agent_spawn import _copy_checkpoint_chain
from shared.checkpoint import (
    load_checkpoint_message_count,
    load_checkpoint_messages_full,
    load_checkpoint_messages_segment,
)
from shared.db import create_agent
from shared.delta_read_compat import (
    _fold_messages,
    areconstruct_delta_messages,
    wrap_saver_reads_with_delta_reconstruction,
)


def _saver(pool: AsyncConnectionPool) -> AsyncPostgresSaver:
    # Same cast as prod (agent/loop.py): the saver opens every cursor with its
    # own dict_row factory, so the pool's default tuple rows never reach it.
    return AsyncPostgresSaver(
        conn=cast(AsyncConnectionPool[psycopg.AsyncConnection[DictRow]], pool)
    )


def _delta_app(saver: AsyncPostgresSaver, *, snapshot_frequency: int = 1000):
    class S(TypedDict):
        messages: Annotated[
            list[AnyMessage],
            DeltaChannel(
                cast(Callable[[Any, Sequence[Any]], Any], guarded_delta_reducer),
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
                AIMessage(id=f"a{n}", content=f"reply {n}"),
            ],
            "n": n + 1,
        }

    def route(state: S) -> str:
        return "step" if state["n"] < state["target"] else END

    graph = StateGraph(S)
    graph.add_node("step", step)  # pyright: ignore[reportUnknownMemberType]
    graph.add_edge(START, "step")
    graph.add_conditional_edges("step", route)
    return graph.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]


def _vanilla_app(saver: AsyncPostgresSaver):
    class S(TypedDict):
        messages: Annotated[list[AnyMessage], add_messages]
        n: int
        target: int

    def step(state: S) -> dict[str, Any]:
        n = state["n"]
        return {
            "messages": [
                HumanMessage(id=f"u{n}", content=f"user {n}"),
                AIMessage(id=f"a{n}", content=f"reply {n}"),
            ],
            "n": n + 1,
        }

    def route(state: S) -> str:
        return "step" if state["n"] < state["target"] else END

    graph = StateGraph(S)
    graph.add_node("step", step)  # pyright: ignore[reportUnknownMemberType]
    graph.add_edge(START, "step")
    graph.add_conditional_edges("step", route)
    return graph.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]


def _config(thread_id: str, checkpoint_id: str | None = None) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _ids(messages: Sequence[Any]) -> list[str]:
    return [m.id for m in messages]


async def _checkpoint_ids(pool: AsyncConnectionPool, thread_id: str) -> list[str]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id",
            (thread_id,),
        )
        return [r[0] for r in await cur.fetchall()]


async def test_vanilla_thread_is_untouched(aops_pool: AsyncConnectionPool) -> None:
    saver = _saver(aops_pool)
    app = _vanilla_app(saver)
    cfg = _config("drc-vanilla")
    await app.ainvoke({"messages": [], "n": 0, "target": 3}, cfg, recursion_limit=40)  # pyright: ignore[reportUnknownMemberType]

    # A plain read of the (unpatched) tuple needs no reconstruction...
    raw = await saver.aget_tuple(cfg)
    assert raw is not None
    assert await areconstruct_delta_messages(saver, raw) is False
    # ...and the wrapped read returns the same history.
    wrap_saver_reads_with_delta_reconstruction(saver)
    state = await app.aget_state(cfg)
    assert _ids(state.values["messages"]) == ["u0", "a0", "u1", "a1", "u2", "a2"]


async def test_delta_thread_reconstructs_resumes_and_self_heals(
    aops_pool: AsyncConnectionPool,
) -> None:
    saver = _saver(aops_pool)
    delta = _delta_app(saver)
    cfg = _config("drc-f1000")
    await delta.ainvoke({"messages": [], "n": 0, "target": 6}, cfg, recursion_limit=60)  # pyright: ignore[reportUnknownMemberType]
    truth = _ids((await delta.aget_state(cfg)).values["messages"])
    assert len(truth) == 12

    wrap_saver_reads_with_delta_reconstruction(saver)
    vanilla = _vanilla_app(saver)
    got = _ids((await vanilla.aget_state(cfg)).values["messages"])
    assert got == truth
    # The startup reconciler reads through `aget` — same patched entry point.
    via_aget = await saver.aget(cfg)
    assert via_aget is not None
    assert len(via_aget["channel_values"]["messages"]) == len(truth)

    # A vanilla write resumes from the reconstructed history, and the new
    # checkpoint materializes a full messages blob (the store self-heals).
    await vanilla.ainvoke({"n": 6, "target": 7}, cfg, recursion_limit=60)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    after_vanilla = _ids((await vanilla.aget_state(cfg)).values["messages"])
    after_delta = _ids((await delta.aget_state(cfg)).values["messages"])
    assert after_vanilla == after_delta == [*truth, "u6", "a6"]
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM checkpoints c JOIN checkpoint_blobs b"
            " ON b.thread_id = c.thread_id AND b.channel = 'messages'"
            " AND b.version = c.checkpoint -> 'channel_versions' ->> 'messages'"
            " WHERE c.thread_id = %s",
            ("drc-f1000",),
        )
        row = await cur.fetchone()
    assert row is not None and row[0] >= 1, "vanilla resume must materialize a messages blob"


async def test_snapshot_tip_unwraps_and_mid_chain_walks(
    aops_pool: AsyncConnectionPool,
) -> None:
    saver = _saver(aops_pool)
    delta = _delta_app(saver, snapshot_frequency=2)
    cfg = _config("drc-snap")
    await delta.ainvoke({"messages": [], "n": 0, "target": 9}, cfg, recursion_limit=60)  # pyright: ignore[reportUnknownMemberType]
    truth = _ids((await delta.aget_state(cfg)).values["messages"])

    # Precondition: the newest checkpoint is a snapshot step, so its stored
    # value is a `_DeltaSnapshot` — the unwrap branch, not the walk.
    raw = await saver.aget_tuple(cfg)
    assert raw is not None
    stored = raw.checkpoint["channel_values"].get("messages")
    assert isinstance(stored, _DeltaSnapshot)

    wrap_saver_reads_with_delta_reconstruction(saver)
    vanilla = _vanilla_app(saver)
    got = _ids((await vanilla.aget_state(cfg)).values["messages"])
    assert got == truth
    unwrapped = await saver.aget_tuple(cfg)
    assert unwrapped is not None
    assert isinstance(unwrapped.checkpoint["channel_values"]["messages"], list)

    # A mid-chain checkpoint (between snapshots) reconstructs from its nearest
    # snapshot seed plus the write tail.
    ids = await _checkpoint_ids(aops_pool, "drc-snap")
    mid = ids[5]
    truth_mid = _ids((await delta.aget_state(_config("drc-snap", mid))).values["messages"])
    got_mid = _ids((await vanilla.aget_state(_config("drc-snap", mid))).values["messages"])
    assert got_mid == truth_mid
    assert 0 < len(truth_mid) < len(truth)


async def test_count_reconstructs_snapshot_tip(
    aops_pool: AsyncConnectionPool, db_conn: psycopg.Connection
) -> None:
    """A snapshot-step delta tip stores a `_DeltaSnapshot` extension, not a
    plain msgpack array — the count reader must reconstruct instead of raising
    (drill D2, execution card §4)."""
    agent_id = create_agent(db_conn)
    db_conn.commit()
    saver = _saver(aops_pool)
    delta = _delta_app(saver, snapshot_frequency=2)
    cfg = _config(str(agent_id))
    await delta.ainvoke({"messages": [], "n": 0, "target": 9}, cfg, recursion_limit=60)  # pyright: ignore[reportUnknownMemberType]
    truth = _ids((await delta.aget_state(cfg)).values["messages"])

    # Precondition: the newest checkpoint is a snapshot step (extension blob).
    raw = await saver.aget_tuple(cfg)
    assert raw is not None
    stored = raw.checkpoint["channel_values"].get("messages")
    assert isinstance(stored, _DeltaSnapshot)

    assert load_checkpoint_message_count(agent_id) == len(truth) == 18


async def test_remove_all_rebuild_folds(aops_pool: AsyncConnectionPool) -> None:
    saver = _saver(aops_pool)
    delta = _delta_app(saver)
    cfg = _config("drc-rebuild")
    await delta.ainvoke({"messages": [], "n": 0, "target": 6}, cfg, recursion_limit=60)  # pyright: ignore[reportUnknownMemberType]
    await delta.aupdate_state(
        cfg,
        {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                HumanMessage(id="rb0", content="rebuilt"),
            ]
        },
    )
    await delta.ainvoke({"n": 6, "target": 8}, cfg, recursion_limit=60)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    truth = _ids((await delta.aget_state(cfg)).values["messages"])
    assert truth == ["rb0", "u6", "a6", "u7", "a7"]

    wrap_saver_reads_with_delta_reconstruction(saver)
    vanilla = _vanilla_app(saver)
    got = _ids((await vanilla.aget_state(cfg)).values["messages"])
    assert got == truth


def test_fold_fast_path_equals_per_write_add_messages() -> None:
    """The append fast path is value-identical to folding every stored write
    through `add_messages` — including the slow shapes (replace, REMOVE_ALL)."""
    base: list[AnyMessage] = [
        HumanMessage(id="s0", content="start"),
        AIMessage(id="s1", content="ans"),
    ]
    cases: list[list[Any]] = [
        [[HumanMessage(id="n1", content="new")], [AIMessage(id="n2", content="a")]],
        [[HumanMessage(id="n1", content="new"), AIMessage(id="s1", content="edited")]],
        [[RemoveMessage(id=REMOVE_ALL_MESSAGES), HumanMessage(id="rb", content="rebuilt")]],
        [
            [HumanMessage(id="n1", content="new")],
            [RemoveMessage(id=REMOVE_ALL_MESSAGES), HumanMessage(id="rb", content="rebuilt")],
            [AIMessage(id="n2", content="after")],
        ],
    ]
    for writes in cases:
        expected: Any = base
        for write in writes:
            expected = add_messages(expected, write)
        fast = _fold_messages(base, writes)
        assert _ids(fast) == _ids(expected)
        assert [m.content for m in fast] == [m.content for m in expected]


async def test_gateway_readers_reconstruct_delta_threads(
    aops_pool: AsyncConnectionPool, db_conn: psycopg.Connection
) -> None:
    """`shared/checkpoint.py`'s sync readers (timeline + self-evolution +
    restore drill) see reconstructed content: full stitch, boundary segment,
    and the delta fallback for the header count."""
    agent_id = create_agent(db_conn)
    db_conn.commit()
    thread = str(agent_id)
    saver = _saver(aops_pool)
    delta = _delta_app(saver)
    cfg = _config(thread)
    await delta.ainvoke(  # pyright: ignore[reportUnknownMemberType]
        {"messages": [SystemMessage(id="sys", content="system")], "n": 0, "target": 6},
        cfg,
        recursion_limit=60,
    )
    truth = _ids((await delta.aget_state(cfg)).values["messages"])
    assert truth[0] == "sys" and len(truth) == 13

    # Stamp a mid checkpoint as a compact boundary (the segment-reader shape).
    ids = await _checkpoint_ids(aops_pool, thread)
    boundary = ids[5]
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE checkpoints SET metadata = metadata || jsonb_build_object('compact_boundary', true)"
            " WHERE thread_id = %s AND checkpoint_id = %s",
            (thread, boundary),
        )
    truth_at_boundary = _ids((await delta.aget_state(_config(thread, boundary))).values["messages"])

    # The boundary is synthetic (no compaction happened), so both segments
    # carry the full prefix; the stitch appends the latest segment with its
    # leading system prompt dropped. Both reads must come back repaired.
    assert _ids(load_checkpoint_messages_full(agent_id)) == [*truth_at_boundary, *truth[1:]]
    assert _ids(load_checkpoint_messages_segment(agent_id, boundary)) == truth_at_boundary[1:]
    assert load_checkpoint_message_count(agent_id) == len(truth)


async def test_fork_copies_the_delta_write_chain(
    aops_pool: AsyncConnectionPool, db_conn: psycopg.Connection
) -> None:
    """A forked thread is a full replica of a delta-written source: the write
    chain comes along, so both readers reconstruct the same history."""
    source = create_agent(db_conn)
    target = create_agent(db_conn)
    db_conn.commit()
    saver = _saver(aops_pool)
    delta = _delta_app(saver)
    cfg = _config(str(source))
    await delta.ainvoke({"messages": [], "n": 0, "target": 6}, cfg, recursion_limit=60)  # pyright: ignore[reportUnknownMemberType]
    truth = _ids((await delta.aget_state(cfg)).values["messages"])
    tip = (await _checkpoint_ids(aops_pool, str(source)))[-1]

    with db_conn.cursor() as cur:
        _copy_checkpoint_chain(cur, source, tip, target)
    db_conn.commit()

    forked_cfg = _config(str(target))
    forked_truth = _ids((await delta.aget_state(forked_cfg)).values["messages"])
    assert forked_truth == truth
    wrap_saver_reads_with_delta_reconstruction(saver)
    vanilla = _vanilla_app(saver)
    assert _ids((await vanilla.aget_state(forked_cfg)).values["messages"]) == truth


async def test_startup_reconcile_reads_reconstructed_delta_state(
    aops_pool: AsyncConnectionPool, db_conn: psycopg.Connection
) -> None:
    """Review #6143 x8: a claimed inbound whose HumanMessage already committed
    into a delta-written thread must be flipped to `done`, not reset to
    `pending` (which would re-deliver it)."""
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id,status,machine) VALUES (%s,'idling','drc-test') "
        "ON CONFLICT (id) DO UPDATE SET status='idling',machine='drc-test'",
        (agent_id,),
    )
    row = db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, 'durable', 'chat', 'user') RETURNING id",
        (agent_id,),
    ).fetchone()
    assert row is not None
    inbound = row[0]
    db_conn.execute(
        "UPDATE inbound_messages SET status = 'claimed', claimed_at = now() WHERE id = %s",
        (inbound,),
    )
    db_conn.commit()

    saver = _saver(aops_pool)
    delta = _delta_app(saver)
    cfg = _config(str(agent_id))
    await delta.ainvoke(  # pyright: ignore[reportUnknownMemberType]
        {
            "messages": [
                HumanMessage(
                    id="inb0",
                    content="committed request",
                    additional_kwargs={"ava_inbound_id": inbound},
                )
            ],
            "n": 0,
            "target": 2,
        },
        cfg,
        recursion_limit=40,
    )

    wrap_saver_reads_with_delta_reconstruction(saver)
    await _reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent_id)

    status = db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id = %s", (inbound,)
    ).fetchone()
    assert status == ("done",), "committed delta-thread inbound must not be re-delivered"
