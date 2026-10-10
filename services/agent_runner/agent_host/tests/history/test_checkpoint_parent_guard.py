"""Real PostgreSQL proofs for the hosted saver's exact parent write boundary."""

import asyncio
from types import SimpleNamespace
from typing import Annotated, Any, Literal, TypedDict, cast

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.pregel._loop import AsyncPregelLoop
from psycopg import AsyncConnection, errors
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import flush_checkpoint
from agent.messages.guard import guarded_delta_reducer
from base.config import settings
from services.agent_runner.agent_host.daemon import _build_checkpointer
from services.agent_runner.agent_host.pooled_checkpoint import (
    MissingCheckpointParentError,
    PooledPostgresSaver,
)


def _config(thread: str = "901", namespace: str = "", parent: str | None = None) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": thread, "checkpoint_ns": namespace}
    if parent is not None:
        configurable["checkpoint_id"] = parent
    return {"configurable": configurable}


def _checkpoint(version: str = "1") -> Checkpoint:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"value": ["committed"]}
    checkpoint["channel_versions"] = {"value": version}
    return checkpoint


def _saver(pool: AsyncConnectionPool[Any]) -> PooledPostgresSaver:
    return PooledPostgresSaver(cast(AsyncConnectionPool[AsyncConnection[DictRow]], pool))


class _DeltaState(TypedDict):
    messages: Annotated[
        list[AnyMessage], DeltaChannel(guarded_delta_reducer, snapshot_frequency=1000)
    ]


def _delta_append(_state: _DeltaState) -> dict[str, list[AIMessage]]:
    return {"messages": [AIMessage(id="reply", content="committed reply")]}


async def _assert_unwritten(conn: AsyncConnection, thread: str, checkpoint: Checkpoint) -> None:
    row = await (
        await conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id=%s AND checkpoint_id=%s",
            (thread, checkpoint["id"]),
        )
    ).fetchone()
    assert row == (0,)
    row = await (
        await conn.execute(
            "SELECT count(*) FROM checkpoint_blobs WHERE thread_id=%s AND version=%s",
            (thread, checkpoint["channel_versions"]["value"]),
        )
    ).fetchone()
    assert row == (0,)


async def test_first_checkpoint_and_exact_parent_append(aops_pool: AsyncConnectionPool) -> None:
    saver = _saver(aops_pool)
    parent = await saver.aput(
        _config(), _checkpoint(), {"source": "input", "step": -1}, {"value": "1"}
    )
    child = _checkpoint("2")
    saved = await saver.aput(parent, child, {"source": "loop", "step": 0}, {"value": "2"})
    stored = await saver.aget_tuple(saved)
    assert stored is not None
    assert stored.parent_config == parent
    assert stored.checkpoint["channel_values"]["value"] == ["committed"]


async def test_historical_full_snapshot_can_be_the_exact_parent_of_a_new_branch(
    aops_pool: AsyncConnectionPool,
) -> None:
    saver = _saver(aops_pool)
    historical = await saver.aput(
        _config(), _checkpoint(), {"source": "input", "step": -1}, {"value": "1"}
    )
    abandoned = await saver.aput(
        historical, _checkpoint("2"), {"source": "loop", "step": 0}, {"value": "2"}
    )
    branch = await saver.aput(
        historical, _checkpoint("3"), {"source": "update", "step": 1}, {"value": "3"}
    )
    stored = await saver.aget_tuple(branch)
    assert stored is not None
    assert stored.parent_config == historical
    assert stored.checkpoint["channel_values"]["value"] == ["committed"]
    assert await saver.aget_tuple(abandoned) is not None


@pytest.mark.parametrize("other", ["absent", "thread", "namespace"])
async def test_missing_exact_parent_cannot_commit_child_or_blob(
    aops_pool: AsyncConnectionPool,
    adb_conn: AsyncConnection,
    other: Literal["absent", "thread", "namespace"],
) -> None:
    saver = _saver(aops_pool)
    parent = _checkpoint()
    if other != "absent":
        await saver.aput(
            _config("902" if other == "thread" else "901", "other" if other == "namespace" else ""),
            parent,
            {"source": "input", "step": -1},
            {"value": "1"},
        )
    child = _checkpoint("2")
    with pytest.raises(MissingCheckpointParentError, match="checkpoint parent is missing"):
        await saver.aput(
            _config(parent=parent["id"]), child, {"source": "loop", "step": 0}, {"value": "2"}
        )
    await _assert_unwritten(adb_conn, "901", child)


async def test_child_insert_failure_rolls_back_already_written_blob(
    aops_pool: AsyncConnectionPool, adb_conn: AsyncConnection
) -> None:
    saver = _saver(aops_pool)
    parent = await saver.aput(
        _config(), _checkpoint(), {"source": "input", "step": -1}, {"value": "1"}
    )
    child = _checkpoint("2")
    cast(Any, child)["id"] = None  # The blob insert precedes this real NOT NULL violation.
    with pytest.raises(errors.NotNullViolation):
        await saver.aput(parent, child, {"source": "loop", "step": 0}, {"value": "2"})
    await _assert_unwritten(adb_conn, "901", child)
    stored = await saver.aget_tuple(_config())
    assert stored is not None
    assert stored.config == parent


async def test_production_nstep_reparents_skipped_checkpoint_and_flush(
    aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_interval", 4)
    saver = await _build_checkpointer(aops_pool)
    first = await saver.aput(
        _config(), _checkpoint(), {"source": "input", "step": -1}, {"value": "1"}
    )
    skipped = _checkpoint("2")
    assert await saver.aput(first, skipped, {"source": "loop", "step": 1}, {"value": "2"}) == first
    phantom = _config(parent=skipped["id"])
    retained = await saver.aput(
        phantom, _checkpoint("3"), {"source": "loop", "step": 4}, {"value": "3"}
    )
    stored = await saver.aget_tuple(retained)
    assert stored is not None
    assert stored.parent_config == first
    assert await saver.aget_tuple(phantom) is None
    tail = _checkpoint("4")
    await saver.aput(retained, tail, {"source": "loop", "step": 5}, {"value": "4"})
    await flush_checkpoint(saver, 901)
    stored = await saver.aget_tuple(_config())
    assert stored is not None
    assert stored.checkpoint["id"] == tail["id"]
    assert stored.parent_config == retained
    assert stored.checkpoint["channel_values"]["value"] == ["committed"]


async def test_actual_delta_graph_with_production_nstep_replays_committed_messages(
    aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_interval", 4)
    saver = await _build_checkpointer(aops_pool)
    builder = cast(Any, StateGraph(_DeltaState))  # Pinned graph overloads contain Unknown types.
    builder.add_node("append", _delta_append)
    builder.add_edge(START, "append")
    builder.add_edge("append", END)
    graph = builder.compile(checkpointer=saver)
    await graph.ainvoke(
        {"messages": [HumanMessage(id="user", content="committed input")]}, _config()
    )
    await flush_checkpoint(saver, 901)
    cold = await saver.aget_tuple(_config())
    assert cold is not None
    assert [message.id for message in cold.checkpoint["channel_values"]["messages"]] == [
        "user",
        "reply",
    ]
    # Delta retires the interval: input, dispatch and completion all persisted.
    checkpoints = [checkpoint async for checkpoint in saver.alist(_config())]
    assert [checkpoint.metadata.get("step") for checkpoint in reversed(checkpoints)] == [-1, 0, 1]


async def _wait_for_database_lock(conn: AsyncConnection, pid: int | None = None) -> None:
    async with asyncio.timeout(3):
        while True:
            row = await (
                await conn.execute(
                    "SELECT count(*) FROM pg_locks WHERE NOT granted AND (%s::int IS NULL OR pid=%s)",
                    (pid, pid),
                )
            ).fetchone()
            if row is not None and row[0]:
                return
            await asyncio.sleep(0.01)


async def test_parent_deletion_waits_until_the_guarded_child_transaction_commits(
    aops_pool: AsyncConnectionPool, adb_conn: AsyncConnection
) -> None:
    saver = _saver(aops_pool)
    parent = await saver.aput(
        _config(), _checkpoint(), {"source": "input", "step": -1}, {"value": "1"}
    )
    child = _checkpoint("2")
    await saver.aput(parent, child, {"source": "loop", "step": 0}, {"value": "2"})
    # Block the retry's child UPSERT after it has locked its exact parent.
    await adb_conn.execute(
        "SELECT 1 FROM checkpoints WHERE thread_id='901' AND checkpoint_id=%s FOR UPDATE",
        (child["id"],),
    )
    writing = asyncio.create_task(saver.aput(parent, child, {"source": "loop", "step": 0}, {}))
    deleting: asyncio.Task[None] | None = None
    try:
        await _wait_for_database_lock(adb_conn)
        async with aops_pool.connection() as conn:

            async def delete_parent() -> None:
                await conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id='901' AND checkpoint_id=%s",
                    (parent.get("configurable", {})["checkpoint_id"],),
                )

            deleting = asyncio.create_task(delete_parent())
            await _wait_for_database_lock(adb_conn, conn.info.backend_pid)
            assert not deleting.done()
            assert not writing.done()
            await adb_conn.rollback()
            await asyncio.wait_for(writing, timeout=5)
            await asyncio.wait_for(deleting, timeout=5)
    finally:
        await adb_conn.rollback()
        await asyncio.wait_for(writing, timeout=5)
        if deleting is not None:
            await asyncio.wait_for(deleting, timeout=5)
    # The guarantee ends at commit, rather than claiming a permanent foreign key.
    stored = await saver.aget_tuple(_config())
    assert stored is not None
    assert stored.checkpoint["id"] == child["id"]


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_pinned_previous_save_failure_cannot_commit_dangling_successor(
    aops_pool: AsyncConnectionPool, adb_conn: AsyncConnection, failure: Literal["error", "cancel"]
) -> None:
    saver = _saver(aops_pool)
    previous = _checkpoint("1")
    if failure == "error":
        versions = cast(Any, {"value": None})  # Real PostgreSQL blob NOT NULL failure.
        prev = asyncio.create_task(
            saver.aput(_config(), previous, {"source": "input", "step": -1}, versions)
        )
        await asyncio.gather(prev, return_exceptions=True)
        original = prev.exception()
        assert isinstance(original, errors.NotNullViolation)
    else:
        # This exclusive pool lease makes the previous real save wait for admission.
        async with AsyncConnectionPool[AsyncConnection[DictRow]](
            aops_pool.conninfo,
            min_size=1,
            max_size=1,
            kwargs={"autocommit": True},
            open=False,
        ) as pool:
            await pool.wait()
            saver = _saver(pool)
            async with pool.connection():
                prev = asyncio.create_task(
                    saver.aput(_config(), previous, {"source": "input", "step": -1}, {"value": "1"})
                )
                async with asyncio.timeout(2):
                    while pool.get_stats().get("requests_waiting", 0) == 0:
                        await asyncio.sleep(0)
                prev.cancel()
                await asyncio.gather(prev, return_exceptions=True)
            assert prev.cancelled()
            await _failed_successor(saver, prev, previous, adb_conn, None)
        return
    await _failed_successor(saver, prev, previous, adb_conn, original)


async def _failed_successor(
    saver: PooledPostgresSaver,
    prev: asyncio.Task[RunnableConfig],
    previous: Checkpoint,
    conn: AsyncConnection,
    original: BaseException | None,
) -> None:
    child = _checkpoint("2")
    owner = SimpleNamespace(_delta_write_futs=[], checkpointer=saver)
    with pytest.raises(
        MissingCheckpointParentError, match="checkpoint parent is missing"
    ) as caught:
        # Run the pinned library method itself, including its finally: aput.
        await cast(Any, AsyncPregelLoop)._checkpointer_put_after_previous(
            cast(Any, owner),
            prev,
            _config(parent=previous["id"]),
            child,
            {"source": "loop", "step": 0},
            {"value": "2"},
        )
    if original is not None:
        assert caught.value.__context__ is original
    else:
        assert isinstance(caught.value.__context__, asyncio.CancelledError)
    await _assert_unwritten(conn, "901", child)
    assert await saver.aget_tuple(_config()) is None
