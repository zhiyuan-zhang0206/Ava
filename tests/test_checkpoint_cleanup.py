"""Tests for shared/checkpoint_cleanup.py.

These drive the REAL LangGraph AsyncPostgresSaver against the session's test
Postgres (the same saver prod uses), so the trim SQL is exercised against
genuine `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` rows and real
serialized message blobs — not hand-rolled fixtures. The decisive assertions
are reconstruction-level: after a trim, the latest checkpoint must still
deserialize to the full message history, proving no blob a survivor references
was deleted.

The `aops_pool` fixture mirrors prod's `agent/loop.py` db_pool (autocommit +
check_connection); `_clean_state` truncates the checkpoint tables per test.
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Any, TypedDict, cast

import psycopg
import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import _messages_delta_reducer
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from shared.checkpoint_cleanup import (
    count_checkpoints,
    mark_compact_boundary,
    trim_checkpoints,
)


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


async def _put_turns(
    pool: AsyncConnectionPool,
    thread_id: str,
    n_turns: int,
    *,
    with_scratch: bool = False,
) -> list[str]:
    """Write `n_turns` checkpoints for `thread_id`, one per turn, the way the
    graph does: the `messages` channel grows by one HumanMessage each turn and
    gets a fresh blob version, so older blob versions become orphaned once their
    checkpoints are trimmed.

    `with_scratch` adds a second non-primitive channel written ONCE on turn 0
    and never again — its blob version stays fixed while every later checkpoint
    keeps referencing it. This is the case a naive "delete blobs of deleted
    checkpoints" trim would corrupt; the correct (channel, version) retention
    must keep it alive as long as any survivor references it.

    Returns the checkpoint ids in write order (oldest first).
    """
    saver = _saver(pool)
    ids: list[str] = []
    parent: str | None = None
    msg_version = None
    scratch_version = None
    messages: list = []
    for turn in range(n_turns):
        messages = [*messages, HumanMessage(content=f"turn {turn}")]
        msg_version = saver.get_next_version(msg_version, None)
        ckpt = empty_checkpoint()
        ckpt_id = ckpt["id"]
        channel_values: dict = {"messages": messages}
        channel_versions: dict = {"messages": msg_version}
        new_versions: dict = {"messages": msg_version}
        if with_scratch:
            if turn == 0:
                scratch_version = saver.get_next_version(None, None)
                channel_values["scratch"] = ["fixed-on-turn-0"]
                new_versions["scratch"] = scratch_version
            # Every checkpoint keeps referencing the turn-0 scratch blob.
            channel_versions["scratch"] = scratch_version
        ckpt["channel_values"] = channel_values
        ckpt["channel_versions"] = channel_versions
        await saver.aput(_config(thread_id, parent), ckpt, {}, new_versions)
        ids.append(ckpt_id)
        parent = ckpt_id
    return ids


async def _blob_versions(pool: AsyncConnectionPool, thread_id: str, channel: str) -> set[str]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT version FROM checkpoint_blobs WHERE thread_id = %s AND channel = %s",
            (thread_id, channel),
        )
        return {r[0] for r in await cur.fetchall()}


async def _surviving_ids(pool: AsyncConnectionPool, thread_id: str) -> set[str]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s",
            (thread_id,),
        )
        return {r[0] for r in await cur.fetchall()}


async def _newest_messages_version(pool: AsyncConnectionPool, thread_id: str) -> str:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT checkpoint -> 'channel_versions' ->> 'messages' FROM checkpoints"
            " WHERE thread_id = %s ORDER BY checkpoint_id DESC LIMIT 1",
            (thread_id,),
        )
        row = await cur.fetchone()
    assert row is not None and row[0] is not None
    return row[0]


async def test_count_checkpoints(aops_pool: AsyncConnectionPool) -> None:
    assert await count_checkpoints(aops_pool, "1") == 0
    await _put_turns(aops_pool, "1", 4)
    assert await count_checkpoints(aops_pool, "1") == 4
    # Scoped to the thread — another thread's checkpoints do not count.
    await _put_turns(aops_pool, "2", 2)
    assert await count_checkpoints(aops_pool, "1") == 4
    assert await count_checkpoints(aops_pool, "2") == 2


async def test_trim_keeps_latest_deletes_old(aops_pool: AsyncConnectionPool) -> None:
    ids = await _put_turns(aops_pool, "1", 10)
    counts = await trim_checkpoints(aops_pool, "1", keep=3)

    assert counts.checkpoints == 7
    assert await count_checkpoints(aops_pool, "1") == 3
    # The survivors are exactly the latest 3 written.
    assert await _surviving_ids(aops_pool, "1") == set(ids[-3:])


async def test_trim_only_touches_target_thread(aops_pool: AsyncConnectionPool) -> None:
    await _put_turns(aops_pool, "1", 6)
    await _put_turns(aops_pool, "2", 6)
    await trim_checkpoints(aops_pool, "1", keep=2)
    assert await count_checkpoints(aops_pool, "1") == 2
    assert await count_checkpoints(aops_pool, "2") == 6  # untouched


async def test_trim_noop_when_under_keep(aops_pool: AsyncConnectionPool) -> None:
    await _put_turns(aops_pool, "1", 2)
    counts = await trim_checkpoints(aops_pool, "1", keep=5)
    assert counts == (0, 0, 0)
    assert await count_checkpoints(aops_pool, "1") == 2


async def test_trim_deletes_orphaned_message_blobs(aops_pool: AsyncConnectionPool) -> None:
    await _put_turns(aops_pool, "1", 8)
    # Each turn writes one fresh `messages` blob version -> 8 versions.
    assert len(await _blob_versions(aops_pool, "1", "messages")) == 8
    counts = await trim_checkpoints(aops_pool, "1", keep=3)
    # 5 checkpoints dropped -> their (now unreferenced) message blobs go too.
    assert counts.checkpoints == 5
    assert counts.blobs == 5
    assert len(await _blob_versions(aops_pool, "1", "messages")) == 3


async def test_trim_keeps_blob_referenced_by_survivor(aops_pool: AsyncConnectionPool) -> None:
    """The decisive correctness test: an unchanged channel's single blob,
    written on turn 0, must survive a trim because every later (surviving)
    checkpoint still references it — even though the checkpoint that *wrote* it
    is deleted."""
    await _put_turns(aops_pool, "1", 8, with_scratch=True)
    assert await _blob_versions(aops_pool, "1", "scratch")  # one scratch blob
    counts = await trim_checkpoints(aops_pool, "1", keep=3)

    # The turn-0 checkpoint (which wrote scratch) is deleted...
    assert counts.checkpoints == 5
    # ...but the scratch blob is retained (survivors still reference it), so
    # only the orphaned message blobs are deleted, not the scratch blob.
    assert counts.blobs == 5
    assert await _blob_versions(aops_pool, "1", "scratch"), "scratch blob wrongly deleted"

    # Reconstruction-level proof: the latest checkpoint still deserializes to
    # the full message history AND the unchanged scratch channel.
    saver = _saver(aops_pool)
    latest = await saver.aget(_config("1"))
    assert latest is not None
    assert [m.content for m in latest["channel_values"]["messages"]] == [
        f"turn {i}" for i in range(8)
    ]
    assert latest["channel_values"]["scratch"] == ["fixed-on-turn-0"]


async def test_trim_cascades_writes(aops_pool: AsyncConnectionPool) -> None:
    ids = await _put_turns(aops_pool, "1", 6)
    saver = _saver(aops_pool)
    # Pending writes attached to an old checkpoint (will be trimmed) and to the
    # latest (will survive).
    await saver.aput_writes(_config("1", ids[0]), [("messages", "old-write")], task_id="t-old")
    await saver.aput_writes(_config("1", ids[-1]), [("messages", "new-write")], task_id="t-new")

    async def _write_ckpt_ids() -> set[str]:
        async with aops_pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT checkpoint_id FROM checkpoint_writes WHERE thread_id = %s",
                ("1",),
            )
            return {r[0] for r in await cur.fetchall()}

    assert await _write_ckpt_ids() == {ids[0], ids[-1]}
    counts = await trim_checkpoints(aops_pool, "1", keep=2)
    assert counts.writes >= 1  # the old checkpoint's writes were cascaded
    assert await _write_ckpt_ids() == {ids[-1]}  # only the survivor's writes remain


async def test_trim_keep_must_be_positive(aops_pool: AsyncConnectionPool) -> None:
    await _put_turns(aops_pool, "1", 3)
    with pytest.raises(AssertionError):
        await trim_checkpoints(aops_pool, "1", keep=0)


async def test_mark_compact_boundary_stamps_newest(aops_pool: AsyncConnectionPool) -> None:
    """`mark_compact_boundary` stamps the thread's NEWEST checkpoint (idempotent)
    — the full-snapshot record of the just-frozen pre-compact segment."""
    ids = await _put_turns(aops_pool, "1", 4)
    await mark_compact_boundary(aops_pool, "1")
    await mark_compact_boundary(aops_pool, "1")  # idempotent

    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT checkpoint_id, metadata FROM checkpoints WHERE thread_id = %s",
            ("1",),
        )
        rows = await cur.fetchall()
    stamped = [r[0] for r in rows if (r[1] or {}).get("compact_boundary")]
    assert stamped == [ids[-1]]  # exactly the newest, stamped once


async def test_trim_keeps_compaction_boundary(aops_pool: AsyncConnectionPool) -> None:
    """A boundary outside the newest keep window is exempt from trimming,
    and its blob references survive with it."""
    ids = await _put_turns(aops_pool, "1", 8)
    # Stamp an old checkpoint as the boundary of an earlier compaction segment.
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE checkpoints SET metadata = metadata || jsonb_build_object('compact_boundary', true)"
            " WHERE thread_id = %s AND checkpoint_id = %s",
            ("1", ids[1]),
        )

    counts = await trim_checkpoints(aops_pool, "1", keep=3)

    # The boundary at ids[1] is exempt: only the other four out-of-window
    # rows are doomed, and its (channel, version) blob refs are kept too.
    assert counts.checkpoints == 4
    assert await _surviving_ids(aops_pool, "1") == {ids[1], *ids[-3:]}
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM checkpoint_blobs WHERE thread_id = %s",
            ("1",),
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == 4


async def test_trim_noop_when_newest_messages_blob_is_missing(
    aops_pool: AsyncConnectionPool,
) -> None:
    ids = await _put_turns(aops_pool, "1", 4)
    newest_version = await _newest_messages_version(aops_pool, "1")
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM checkpoint_blobs"
            " WHERE thread_id = %s AND channel = 'messages' AND version = %s",
            ("1", newest_version),
        )

    counts = await trim_checkpoints(aops_pool, "1", keep=3)

    assert counts == (0, 0, 0)
    assert await _surviving_ids(aops_pool, "1") == set(ids)
    assert len(await _blob_versions(aops_pool, "1", "messages")) == 3


async def test_trim_noop_while_newer_messages_blob_is_in_flight(
    aops_pool: AsyncConnectionPool,
) -> None:
    ids = await _put_turns(aops_pool, "1", 4)
    newest_version = await _newest_messages_version(aops_pool, "1")
    in_flight_version = _saver(aops_pool).get_next_version(newest_version, None)
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO checkpoint_blobs"
            " (thread_id, checkpoint_ns, channel, version, type, blob)"
            " VALUES (%s, '', 'messages', %s, 'json', %s)",
            ("1", in_flight_version, b'"in-flight"'),
        )

    counts = await trim_checkpoints(aops_pool, "1", keep=3)

    assert counts == (0, 0, 0)
    assert await _surviving_ids(aops_pool, "1") == set(ids)
    versions = await _blob_versions(aops_pool, "1", "messages")
    assert len(versions) == 5
    assert in_flight_version in versions


# -- never-delete retention (tasks #3180/#3181) -------------------------------


async def test_trim_parks_a_delta_written_thread(aops_pool: AsyncConnectionPool) -> None:
    """Real delta rows: the explicit `delta_thread` exemption parks the thread
    (its replay counters mark it delta; the newest messages version additionally
    has no blobs to begin with) — zero deletions and the history still
    reconstructs. This is the intended steady state under the never-delete
    ruling (2026-09-12, tasks #3180/#3181)."""
    saver = _saver(aops_pool)
    app: Any = _delta_app(saver)
    cfg = _config("delta-park")
    await app.ainvoke({"messages": [], "n": 0, "target": 3}, cfg, recursion_limit=40)

    before = await count_checkpoints(aops_pool, "delta-park")
    assert before >= 4

    counts = await trim_checkpoints(aops_pool, "delta-park", keep=1)

    assert counts == (0, 0, 0)
    assert await count_checkpoints(aops_pool, "delta-park") == before
    state = await app.aget_state(cfg)
    assert [m.id for m in state.values["messages"]] == ["u0", "a0", "u1", "a1", "u2", "a2"]


async def test_trim_parks_a_delta_thread_at_snapshot_point(
    aops_pool: AsyncConnectionPool,
) -> None:
    """The explicit exemption, not blob absence, parks the trim at a delta
    snapshot step (review #6143 I1/x2b): with `snapshot_frequency=2` the newest
    checkpoint IS a snapshot point — no replay counters in its metadata and its
    messages blob is committed (EXT-7 `_DeltaSnapshot`) — so the plain guard
    alone would proceed and sever the ancestor chain. Zero deletions, and every
    retained read still reconstructs."""
    saver = _saver(aops_pool)
    app: Any = _delta_app(saver, snapshot_frequency=2)
    cfg = _config("delta-spark")
    await app.ainvoke({"messages": [], "n": 0, "target": 9}, cfg, recursion_limit=60)

    # Preconditions that make this the review-#6143-x2b shape.
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT metadata ? 'counters_since_delta_snapshot',"
            " (SELECT count(*) FROM checkpoint_blobs b"
            "   WHERE b.thread_id = c.thread_id AND b.channel = 'messages'"
            "     AND b.version = c.checkpoint -> 'channel_versions' ->> 'messages')"
            " FROM checkpoints c WHERE c.thread_id = %s"
            " ORDER BY c.checkpoint_id DESC LIMIT 1",
            ("delta-spark",),
        )
        row = await cur.fetchone()
    assert row is not None
    has_counters, blob_rows = row
    assert has_counters is False, "target must land the newest checkpoint on a snapshot step"
    assert blob_rows == 1, "the snapshot's messages blob must be committed"

    before = await count_checkpoints(aops_pool, "delta-spark")
    counts = await trim_checkpoints(aops_pool, "delta-spark", keep=1)

    assert counts == (0, 0, 0)
    assert await count_checkpoints(aops_pool, "delta-spark") == before

    full_ids = [x for i in range(9) for x in (f"u{i}", f"a{i}")]
    state = await app.aget_state(cfg)
    assert [m.id for m in state.values["messages"]] == full_ids

    # A mid-chain point still reads back its exact prefix (the surviving
    # snapshot seeds survive with it).
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s"
            " ORDER BY checkpoint_id OFFSET 4 LIMIT 1",
            ("delta-spark",),
        )
        mid_row = await cur.fetchone()
    assert mid_row is not None
    mid_state = await app.aget_state(_config("delta-spark", mid_row[0]))
    mid_ids = [m.id for m in mid_state.values["messages"]]
    assert mid_ids == full_ids[: len(mid_ids)] and len(mid_ids) > 0


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
