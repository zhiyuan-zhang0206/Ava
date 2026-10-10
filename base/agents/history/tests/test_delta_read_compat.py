"""Tests for base/agents/history/delta_read_compat.py — delta read-compat reconstruction.

Real delta-written threads against the session's test Postgres: the transition
layer must let plain (vanilla) readers see the same history the delta runtime
reconstructs, must leave vanilla-written threads untouched, and must self-heal
the store on the first vanilla write. Fork copying and the startup inbound
reconciler are covered end to end (review #6143 I2/I3a — task #3180/#3181).
"""

import asyncio
from collections.abc import Sequence
from typing import Annotated, Any, TypedDict, cast

import psycopg
import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint import _is_delta_snapshot_blob
from base.agents.history.checkpoint_postgres_walks import (
    HistoryAsyncPostgresSaver as AsyncPostgresSaver,
)
from base.agents.history.delta_read_compat import (
    _fold_messages,
    areconstruct_delta_messages,
    recovery_reconstruction_scope,
    wrap_saver_reads_with_delta_reconstruction,
)


def _saver(pool: AsyncConnectionPool) -> AsyncPostgresSaver:
    # Same cast as prod (agent/loop.py): the saver opens every cursor with its
    # own dict_row factory, so the pool's default tuple rows never reach it.
    return AsyncPostgresSaver(
        conn=cast(AsyncConnectionPool[psycopg.AsyncConnection[DictRow]], pool)
    )


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


def _synthetic_recovery_saver() -> tuple[AsyncPostgresSaver, list[tuple[str, str, str]], list[Any]]:
    """A saver whose exact tuple and history reads are independently observable."""
    saver = AsyncPostgresSaver(cast(Any, object()))
    walks: list[tuple[str, str, str]] = []
    pending: list[Any] = [("task", "other", "before")]

    async def raw_tuple(config: RunnableConfig) -> CheckpointTuple:
        configurable = dict(config["configurable"])  # pyright: ignore[reportTypedDictNotRequiredAccess]
        return CheckpointTuple(
            {"configurable": configurable},
            cast(
                Any,
                {
                    "id": configurable["checkpoint_id"],
                    "channel_values": {},
                    "channel_versions": {"messages": "v"},
                },
            ),
            cast(Any, {"counters_since_delta_snapshot": {"messages": 1}}),
            None,
            list(pending),
        )

    async def history(*, config: RunnableConfig, channels: Sequence[str]) -> dict[str, Any]:
        assert channels == ["messages"]
        values = config["configurable"]  # pyright: ignore[reportTypedDictNotRequiredAccess]
        key = (values["thread_id"], values["checkpoint_id"], values["checkpoint_ns"])
        walks.append(key)
        return {
            "messages": {"writes": [("task", "messages", [HumanMessage(id="m", content=str(key))])]}
        }

    async def write(_config: RunnableConfig, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def flush(_thread_id: str) -> None:
        return None

    saver.aget_tuple = raw_tuple  # type: ignore[method-assign]
    saver.aget_delta_channel_history = history  # type: ignore[method-assign]
    saver.aput = write  # type: ignore[method-assign]
    saver.aput_writes = write  # type: ignore[method-assign]
    saver._ava_nstep_flush = flush  # type: ignore[attr-defined]
    wrap_saver_reads_with_delta_reconstruction(saver)
    return saver, walks, pending


def _ids(messages: Sequence[Any]) -> list[str]:
    return [m.id for m in messages]


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


async def test_recovery_cache_exact_key_and_invalidation() -> None:
    saver, walks, pending = _synthetic_recovery_saver()
    first = _config("thread-a", "checkpoint-a")
    changed = _config("thread-a", "checkpoint-b")
    namespace: RunnableConfig = {
        "configurable": {
            "thread_id": "thread-a",
            "checkpoint_id": "checkpoint-a",
            "checkpoint_ns": "other",
        }
    }
    other_thread = _config("thread-b", "checkpoint-a")
    with recovery_reconstruction_scope(saver, "thread-a") as scope:
        assert scope is not None
        reader = scope.reader()
        initial = await reader.aget_tuple(first)
        pending[:] = [("task", "other", "after")]
        reused = await reader.aget_tuple(first)
        assert initial is not None and reused is not None
        assert len(walks) == 1
        assert reused.pending_writes == pending
        initial.checkpoint["channel_values"]["messages"].clear()
        copied = cast(CheckpointTuple, await reader.aget_tuple(first))
        assert len(copied.checkpoint["channel_values"]["messages"]) == 1
        copied.checkpoint["channel_values"]["messages"][0].content = "changed on hit"
        isolated = cast(CheckpointTuple, await reader.aget_tuple(first))
        assert isolated.checkpoint["channel_values"]["messages"][0].content != "changed on hit"

        await saver._ava_nstep_flush("thread-a")  # type: ignore[attr-defined]
        await reader.aget_tuple(first)
        assert len(walks) == 2
        await saver.aput_writes(first, [], "task")
        await reader.aget_tuple(first)
        assert len(walks) == 3
        await saver.aput(first, {}, {}, {})  # type: ignore[arg-type]
        await reader.aget_tuple(first)
        assert len(walks) == 4

        await reader.aget_tuple(changed)
        await reader.aget_tuple(namespace)
        await reader.aget_tuple(other_thread)
        assert walks[-3:] == [
            ("thread-a", "checkpoint-b", ""),
            ("thread-a", "checkpoint-a", "other"),
            ("thread-b", "checkpoint-a", ""),
        ]
    await saver.aget_tuple(first)
    assert len(walks) == 8


@pytest.mark.parametrize("operation", ["aput", "aput_writes", "flush"])
async def test_recovery_cache_write_commit_revokes_fill_and_flush_reuse(operation: str) -> None:
    saver, walks, _pending = _synthetic_recovery_saver()
    config = _config("thread-a", "checkpoint-a")
    entered = asyncio.Event()
    commit = asyncio.Event()
    stored = "old"

    async def history(*, config: RunnableConfig, channels: Sequence[str]) -> dict[str, Any]:
        walks.append(("thread-a", "checkpoint-a", ""))
        return {
            "messages": {"writes": [("task", "messages", [HumanMessage(id="m", content=stored)])]}
        }

    async def write(*_args: Any, **_kwargs: Any) -> None:
        nonlocal stored
        entered.set()
        await commit.wait()
        stored = "new"

    saver.aget_delta_channel_history = history  # type: ignore[method-assign]
    if operation == "aput":
        saver.aput = write  # type: ignore[method-assign]
    elif operation == "aput_writes":
        saver.aput_writes = write  # type: ignore[method-assign]
    else:
        saver._ava_nstep_flush = write  # type: ignore[attr-defined]
    with recovery_reconstruction_scope(saver, "thread-a") as scope:
        assert scope is not None
        reader = scope.reader()
        first = cast(CheckpointTuple, await reader.aget_tuple(config))
        flushed_generation = scope.generation
        if operation == "aput":
            writer = asyncio.create_task(saver.aput(config, {}, {}, {}))  # type: ignore[arg-type]
        elif operation == "aput_writes":
            writer = asyncio.create_task(saver.aput_writes(config, [], "task"))
        else:
            writer = asyncio.create_task(saver._ava_nstep_flush("thread-a"))  # type: ignore[attr-defined]
        await entered.wait()
        during = cast(CheckpointTuple, await reader.aget_tuple(config))
        generation_during_write = scope.generation
        commit.set()
        await writer
        after = cast(CheckpointTuple, await reader.aget_tuple(config))
        values = [
            item.checkpoint["channel_values"]["messages"][0].content
            for item in (first, during, after)
        ]
        assert values == ["old", "old", "new"]
        assert len(walks) == 3
        assert scope.generation > generation_during_write > flushed_generation


async def test_recovery_cache_concurrent_reads() -> None:
    saver, walks, _pending = _synthetic_recovery_saver()
    config = _config("thread-a", "checkpoint-a")
    with recovery_reconstruction_scope(saver, "thread-a") as scope:
        assert scope is not None
        reader = scope.reader()
        reads = await asyncio.gather(*(reader.aget_tuple(config) for _ in range(3)))
        assert all(read is not None for read in reads)
        assert len(walks) == 1


async def test_recovery_cache_cancelled_reconstruction(loguru_records: list[Any]) -> None:
    saver, _walks, _pending = _synthetic_recovery_saver()
    config = _config("thread-a", "checkpoint-a")
    original_history = saver.aget_delta_channel_history
    entered = asyncio.Event()
    attempts = 0

    async def blocked_history(*, config: RunnableConfig, channels: Sequence[str]) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await asyncio.Event().wait()
        return await original_history(config=config, channels=channels)

    saver.aget_delta_channel_history = blocked_history  # type: ignore[method-assign]
    with recovery_reconstruction_scope(saver, "thread-a") as scope:
        assert scope is not None
        reader = scope.reader()
        task = asyncio.create_task(reader.aget_tuple(config))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cancelled = [
            record["extra"]
            for record in loguru_records
            if record["extra"].get("outcome") == "cancelled"
        ]
        assert len(cancelled) == 1
        assert cancelled[0]["failed_phase"] == "history_read"
        assert cancelled[0]["cache_hit"] is None
        assert cancelled[0]["message_count"] is None
        assert await reader.aget_tuple(config) is not None
        assert attempts == 2
    assert await saver.aget_tuple(config) is not None
    assert attempts == 3


async def test_recovery_cache_isolated_across_concurrent_agents() -> None:
    saver, walks, _pending = _synthetic_recovery_saver()

    async def read_twice(thread_id: str) -> None:
        config = _config(thread_id, "same-checkpoint-id")
        with recovery_reconstruction_scope(saver, thread_id) as scope:
            assert scope is not None
            reader = scope.reader()
            await reader.aget_tuple(config)
            await reader.aget_tuple(config)

    await asyncio.gather(read_twice("thread-a"), read_twice("thread-b"))
    assert walks.count(("thread-a", "same-checkpoint-id", "")) == 1
    assert walks.count(("thread-b", "same-checkpoint-id", "")) == 1


def test_delta_snapshot_markers_cover_ext_and_fixext() -> None:
    """Marker coverage: small snapshot payloads pack as fixext (d4-d8), not
    just ext8/16/32 — an emptied messages snapshot serializes with a fixext
    head (QA probe: `_DeltaSnapshot([])` -> first byte 0xd4). The count
    predicate must accept both families and still reject a plain array head."""
    _type, payload = JsonPlusSerializer().dumps_typed(_DeltaSnapshot([]))
    assert payload[0] in (0xC7, 0xC8, 0xC9, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8)
    assert _is_delta_snapshot_blob("msgpack", payload[:5]) is True
    for marker in (b"\xc7", b"\xc8", b"\xc9", b"\xd4", b"\xd5", b"\xd6", b"\xd7", b"\xd8"):
        assert _is_delta_snapshot_blob("msgpack", marker) is True
    assert _is_delta_snapshot_blob("msgpack", b"\x92") is False


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


async def test_scope_requires_explicit_binding_and_parent() -> None:
    saver, walks, _pending = _synthetic_recovery_saver()
    config = _config("thread-a", "checkpoint-a")
    with recovery_reconstruction_scope(saver, "thread-a") as scope:
        assert scope is not None
        reader = scope.reader()
        await reader.aget_tuple(config)
        await saver.aget_tuple(config)
        await reader.aget_tuple(config)
        assert len(walks) == 2
        with recovery_reconstruction_scope(saver, "thread-a", parent=scope) as nested:
            assert nested is scope and nested is not None
            await nested.reader().aget_tuple(config)
        assert len(walks) == 2
        with (
            pytest.raises(RuntimeError, match="concurrent checkpoint recovery"),
            recovery_reconstruction_scope(saver, "thread-a"),
        ):
            pass
        with (
            pytest.raises(ValueError, match="parent does not match"),
            recovery_reconstruction_scope(saver, "thread-b", parent=scope),
        ):
            pass
    assert scope.messages is None and scope.active is False
    with pytest.raises(RuntimeError, match="scope has ended"):
        scope.reader()


async def test_ordinary_empty_checkpoint_is_not_delta() -> None:
    saver, walks, _pending = _synthetic_recovery_saver()
    config = _config("thread-a", "checkpoint-a")
    checkpoint = await saver.aget_tuple(config)
    assert checkpoint is not None
    checkpoint.checkpoint["channel_values"].clear()
    checkpoint = checkpoint._replace(metadata={})
    assert await areconstruct_delta_messages(saver, checkpoint) is False
    assert len(walks) == 1


@pytest.mark.parametrize("history", [{}, {"messages": {"writes": []}}])
async def test_missing_delta_history_propagates(
    monkeypatch: pytest.MonkeyPatch, history: dict[str, Any]
) -> None:
    saver, _walks, _pending = _synthetic_recovery_saver()

    async def missing(**_kwargs: Any) -> dict[str, Any]:
        return history

    monkeypatch.setattr(saver, "aget_delta_channel_history", missing)
    with pytest.raises(RuntimeError, match="messages history is missing"):
        await saver.aget_tuple(_config("thread-a", "checkpoint-a"))


async def test_scope_sync_bridge_shares_async_reconstruction() -> None:
    saver, walks, _pending = _synthetic_recovery_saver()
    config = _config("thread-a", "checkpoint-a")
    with recovery_reconstruction_scope(saver, "thread-a") as scope:
        assert scope is not None
        reader = scope.reader()
        sync = await asyncio.to_thread(reader.get_tuple, config)
        asynchronous = await reader.aget_tuple(config)
        assert sync is not None and asynchronous is not None
        assert sync.checkpoint["channel_values"] == asynchronous.checkpoint["channel_values"]
        assert len(walks) == 1
