"""Delta-written threads read back through the agent's message reducer: reconstruction, resume, self-heal, snapshot tips, rebuilds, forks and startup reconcile."""

from collections.abc import Callable, Sequence
from typing import Annotated, Any, TypedDict, cast

import psycopg
import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from agent.messages.guard import guarded_delta_reducer
from agent.startup import reconcile_claimed_inbounds_at_startup
from base.agents.history.checkpoint import (
    load_checkpoint_message_count,
    load_checkpoint_messages_full,
    load_checkpoint_messages_segment,
)
from base.agents.history.checkpoint_copy import copy_checkpoint_chain
from base.agents.history.checkpoint_postgres_walks import (
    HistoryAsyncPostgresSaver as AsyncPostgresSaver,
)
from base.agents.history.delta_read_compat import (
    recovery_reconstruction_scope,
    wrap_saver_reads_with_delta_reconstruction,
)
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate


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


async def test_delta_read_span_has_phase_fields(
    aops_pool: AsyncConnectionPool, loguru_records: list[Any]
) -> None:
    saver = _saver(aops_pool)
    cfg = _config("drc-span")
    await _delta_app(saver).ainvoke({"messages": [], "n": 0, "target": 2}, cfg)  # pyright: ignore[reportUnknownMemberType]
    wrap_saver_reads_with_delta_reconstruction(saver)
    checkpoint = await saver.aget(cfg)
    assert checkpoint is not None
    spans = [
        record["extra"]
        for record in loguru_records
        if record["extra"].get("event") == "delta_read_compat"
        and record["extra"].get("checkpoint_id") == checkpoint["id"]
    ]
    assert spans
    span = spans[-1]
    assert all(
        span[field] >= 0 for field in ("tuple_read_ms", "history_read_ms", "fold_ms", "elapsed_ms")
    )
    assert span["elapsed_ms"] >= span["tuple_read_ms"]
    assert span["outcome"] == "success"
    assert "decode_ms" not in span and "reset_decode_ms" not in span


async def test_recovery_cache_invalidates_on_graph_state_update(
    aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    saver = _saver(aops_pool)
    graph = _delta_app(saver)
    config = _config("drc-state-write")
    await graph.ainvoke({"messages": [], "n": 0, "target": 2}, config)  # pyright: ignore[reportUnknownMemberType]
    previous = await AsyncPostgresSaver.aget_tuple(saver, config)
    assert previous is not None
    wrap_saver_reads_with_delta_reconstruction(saver)
    history = saver.aget_delta_channel_history
    walks = 0

    async def counted(*, config: RunnableConfig, channels: Sequence[str]) -> Any:
        nonlocal walks
        walks += 1
        return await history(config=config, channels=channels)

    monkeypatch.setattr(saver, "aget_delta_channel_history", counted)
    with recovery_reconstruction_scope(saver, "drc-state-write") as scope:
        assert scope is not None
        reader = scope.reader()
        await reader.aget_tuple(previous.config)
        await reader.aget_tuple(previous.config)
        assert walks == 1
        scoped_graph = graph.copy({"checkpointer": reader})
        await scoped_graph.aupdate_state(
            config, {"messages": [HumanMessage(id="new", content="new")]}
        )  # pyright: ignore[reportUnknownMemberType]
        await reader.aget_tuple(previous.config)
        assert walks == 2


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
    aops_pool: AsyncConnectionPool, db_conn: psycopg.Connection, database_gate: ProcessDbGate
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

    assert (
        load_checkpoint_message_count(Database.from_settings(gate=database_gate), agent_id)
        == len(truth)
        == 18
    )


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


async def test_gateway_readers_reconstruct_delta_threads(
    aops_pool: AsyncConnectionPool, db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    """`base/agents/history/checkpoint.py`'s sync readers (timeline + self-evolution +
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
    assert _ids(
        load_checkpoint_messages_full(Database.from_settings(gate=database_gate), agent_id)
    ) == [
        *truth_at_boundary,
        *truth[1:],
    ]
    assert (
        _ids(
            load_checkpoint_messages_segment(
                Database.from_settings(gate=database_gate), agent_id, boundary
            )
        )
        == truth_at_boundary[1:]
    )
    assert load_checkpoint_message_count(
        Database.from_settings(gate=database_gate), agent_id
    ) == len(truth)


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
        copy_checkpoint_chain(cur, source, tip, target)
    db_conn.commit()

    forked_cfg = _config(str(target))
    forked_truth = _ids((await delta.aget_state(forked_cfg)).values["messages"])
    assert forked_truth == truth
    wrap_saver_reads_with_delta_reconstruction(saver)
    vanilla = _vanilla_app(saver)
    assert _ids((await vanilla.aget_state(forked_cfg)).values["messages"]) == truth


async def test_fork_at_a_boundary_replicates_the_source_state(
    aops_pool: AsyncConnectionPool, db_conn: psycopg.Connection
) -> None:
    """Forking exactly at a compact boundary replicates the source's state there.

    A boundary is not a self-contained snapshot on a delta-written thread: its
    content folds from the write chain, so the copy must carry the segment
    window down to the previous boundary (or the root) — a window cut AT the
    boundary contains neither the segment's reset nor a snapshot, and the
    replica read back empty (task #3979). Both branches: a first boundary
    (window to root) and a boundary with a boundary below it ([B1..B2])."""
    source = create_agent(db_conn)
    t_b1 = create_agent(db_conn)
    t_b2 = create_agent(db_conn)
    db_conn.commit()
    saver = _saver(aops_pool)
    delta = _delta_app(saver)
    cfg = _config(str(source))

    # Segment 1: two turns (4 messages), then the compact-shaped write
    # sequence — stamp the newest checkpoint, REMOVE_ALL, restore summary+tail.
    await delta.ainvoke({"messages": [], "n": 0, "target": 2}, cfg, recursion_limit=60)  # pyright: ignore[reportUnknownMemberType]
    b1 = (await _checkpoint_ids(aops_pool, str(source)))[-1]
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE checkpoints SET metadata = metadata || jsonb_build_object('compact_boundary', true)"
            " WHERE thread_id = %s AND checkpoint_id = %s",
            (str(source), b1),
        )
    await delta.aupdate_state(cfg, {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]})
    await delta.aupdate_state(
        cfg,
        {
            "messages": [
                SystemMessage(id="sum0", content="summary 0"),
                HumanMessage(id="tail0", content="tail"),
            ]
        },
    )

    # Segment 2: two more turns, then a second compact (boundary B2).
    await delta.ainvoke({"n": 2, "target": 4}, cfg, recursion_limit=60)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    b2 = (await _checkpoint_ids(aops_pool, str(source)))[-1]
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE checkpoints SET metadata = metadata || jsonb_build_object('compact_boundary', true)"
            " WHERE thread_id = %s AND checkpoint_id = %s",
            (str(source), b2),
        )
    await delta.aupdate_state(cfg, {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]})
    await delta.aupdate_state(
        cfg,
        {
            "messages": [
                SystemMessage(id="sum1", content="summary 1"),
                HumanMessage(id="tail1", content="tail"),
            ]
        },
    )
    await delta.ainvoke({"n": 4, "target": 5}, cfg, recursion_limit=60)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]

    src_b1 = _ids((await delta.aget_state(_config(str(source), b1))).values["messages"])
    src_b2 = _ids((await delta.aget_state(_config(str(source), b2))).values["messages"])
    assert src_b1 == ["u0", "a0", "u1", "a1"]
    assert src_b2 == ["sum0", "tail0", "u2", "a2", "u3", "a3"]

    with db_conn.cursor() as cur:
        copy_checkpoint_chain(cur, source, b1, t_b1)
        copy_checkpoint_chain(cur, source, b2, t_b2)
    db_conn.commit()

    # Native delta read: fork@B1's window is the whole chain (no boundary
    # below); fork@B2's is [B1..B2].
    assert _ids((await delta.aget_state(_config(str(t_b1)))).values["messages"]) == src_b1
    assert _ids((await delta.aget_state(_config(str(t_b2)))).values["messages"]) == src_b2

    # Compat read of the same replicas.
    wrap_saver_reads_with_delta_reconstruction(saver)
    vanilla = _vanilla_app(saver)
    assert _ids((await vanilla.aget_state(_config(str(t_b1)))).values["messages"]) == src_b1
    assert _ids((await vanilla.aget_state(_config(str(t_b2)))).values["messages"]) == src_b2


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
    from base.agents.history.inbound_sideload import ReconcileReadInputs
    from base.config import settings

    await reconcile_claimed_inbounds_at_startup(
        aops_pool,
        saver,
        agent_id,
        incarnation=None,
        inputs=ReconcileReadInputs(
            stale_claimed_seconds=lambda: (
                settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
            ),
            clock_pad_seconds=lambda: settings.daemon.inbound_reconcile_clock_pad_seconds,
            boundary_scan_limit=lambda: settings.daemon.inbound_reconcile_boundary_scan_limit,
            window_row_cap=lambda: settings.daemon.inbound_reconcile_window_row_cap,
        ),
    )

    status = db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id = %s", (inbound,)
    ).fetchone()
    assert status == ("done",), "committed delta-thread inbound must not be re-delivered"
