"""N-step checkpoint write throttling keeps checkpoint rows and writes in lockstep.

Delta-bearing threads are the exception: once a checkpoint reveals a
`DeltaChannel`, the throttle retires so every super-step and every write batch
persists exactly as upstream produced them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, cast
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.constants import PUSH
from langgraph.graph import END, START, StateGraph
from psycopg_pool import AsyncConnectionPool
from typing_extensions import TypedDict

from agent.messages_guard import guarded_delta_reducer
from agent.startup import _wrap_saver_writes_with_nstep_interval
from shared.config.agent_runtime import AgentRuntimeSettings


class _StubSaver:
    """Duck-typed saver that records the write calls made by the wrapper."""

    _ava_nstep_flush: Callable[[str], Awaitable[None]]

    def __init__(self) -> None:
        self.aput_calls: list[
            tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]
        ] = []
        self.aput_writes_calls: list[
            tuple[dict[str, object], list[tuple[str, object]], str, str | None]
        ] = []

    async def aput(
        self,
        config: dict[str, object],
        checkpoint: dict[str, object],
        metadata: dict[str, object],
        new_versions: dict[str, object],
    ) -> dict[str, object]:
        self.aput_calls.append((config, checkpoint, metadata, new_versions))
        step = metadata["step"]
        assert isinstance(step, int)
        return {"stored_step": step}

    async def aput_writes(
        self,
        config: dict[str, object],
        writes: list[tuple[str, object]],
        task_id: str,
        task_path: str | None = None,
    ) -> None:
        self.aput_writes_calls.append((config, writes, task_id, task_path))


class _GraphState(TypedDict):
    """State used to exercise the real LangGraph checkpoint loop."""

    count: int


class _DeltaState(TypedDict):
    """Delta-channel state used by the wrapper-retirement regression."""

    messages: Annotated[list[Any], DeltaChannel(guarded_delta_reducer, snapshot_frequency=1000)]
    n: int
    target: int


def _wrap(saver: _StubSaver, interval: int | Callable[[], int]) -> None:
    _wrap_saver_writes_with_nstep_interval(cast(AsyncPostgresSaver, saver), interval)


def _stored_thread_ids(saver: _StubSaver) -> list[str]:
    return [cast(dict[str, str], call[0]["configurable"])["thread_id"] for call in saver.aput_calls]


async def _aput(
    saver: _StubSaver, step: int, source: str = "update", thread_id: str = "default"
) -> dict[str, object]:
    return await saver.aput(
        {"configurable": {"thread_id": thread_id}, "input_step": step},
        {"checkpoint_id": str(step), "channel_versions": {"messages": f"v{step}"}},
        {"source": source, "step": step},
        {"channel": step},
    )


async def _aput_delta(
    saver: _StubSaver,
    step: int,
    *,
    source: str = "loop",
    thread_id: str = "default",
    counters: dict[str, tuple[int, int]] | None = None,
    snapshot: bool = False,
) -> dict[str, object]:
    """Aput carrying delta evidence: channel counters, a `_DeltaSnapshot`, or neither."""
    checkpoint: dict[str, object] = {
        "checkpoint_id": str(step),
        "channel_versions": {"messages": f"v{step}"},
    }
    if snapshot:
        checkpoint["channel_values"] = {"messages": _DeltaSnapshot([])}
    metadata: dict[str, object] = {"source": source, "step": step}
    if counters is not None:
        metadata["counters_since_delta_snapshot"] = counters
    return await saver.aput(
        {"configurable": {"thread_id": thread_id}, "input_step": step},
        checkpoint,
        metadata,
        {"channel": step},
    )


async def test_interval_writes_only_aligned_update_checkpoints() -> None:
    saver = _StubSaver()
    _wrap(saver, interval=4)

    for step in range(9):
        await _aput(saver, step)

    assert [call[2]["step"] for call in saver.aput_calls] == [0, 4, 8]


async def test_interval_writes_only_aligned_loop_supersteps() -> None:
    saver = _StubSaver()
    _wrap(saver, interval=4)

    for step in range(9):
        await _aput(saver, step, source="loop")

    assert [call[2]["step"] for call in saver.aput_calls] == [0, 4, 8]


async def test_interval_keeps_channel_and_push_writes_in_checkpoint_lockstep() -> None:
    saver = _StubSaver()
    _wrap(saver, interval=4)

    await _aput(saver, 0, source="loop")
    await saver.aput_writes(
        {"configurable": {"thread_id": "default"}, "config": 1},
        [("channel-skipped", "value")],
        "task-skipped",
    )
    await _aput(saver, 1, source="loop")
    await _aput(saver, 2, source="loop")
    await _aput(saver, 3, source="loop")
    await saver.aput_writes(
        {"configurable": {"thread_id": "default"}, "config": 4},
        [("channel-written", "value")],
        "task-written",
    )
    await _aput(saver, 4)
    await saver.aput_writes(
        {"configurable": {"thread_id": "default"}, "config": 4},
        [(PUSH, "value")],
        "task-push",
    )

    assert [call[1] for call in saver.aput_writes_calls] == [
        [("channel-written", "value")],
        [(PUSH, "value")],
    ]


async def test_writes_without_a_seen_checkpoint_fail_open() -> None:
    saver = _StubSaver()
    _wrap(saver, interval=4)

    await saver.aput_writes(
        {"configurable": {"thread_id": "default"}, "config": 1},
        [("channel", "value")],
        "task",
    )

    assert [call[1] for call in saver.aput_writes_calls] == [[("channel", "value")]]


async def test_interval_keeps_real_graph_parents_and_write_targets_persisted() -> None:
    """Pin the LangGraph loop contract: its returned ``aput`` config is ignored.

    A saver-only test cannot reveal a skipped checkpoint becoming the next
    persisted checkpoint's parent. Drive a real graph and assert its stored
    chain has no missing parents or write targets.
    """
    from langgraph.graph import END, START, StateGraph

    def increment(state: _GraphState) -> dict[str, int]:
        return {"count": state["count"] + 1}

    def route(state: _GraphState) -> str:
        return "increment" if state["count"] < 6 else END

    saver = InMemorySaver()
    _wrap(cast(_StubSaver, saver), interval=4)
    graph = StateGraph(_GraphState)
    graph.add_node("increment", increment)  # pyright: ignore[reportUnknownMemberType]
    graph.add_edge(START, "increment")
    graph.add_conditional_edges("increment", route)
    compiled = graph.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]

    await compiled.ainvoke(  # pyright: ignore[reportUnknownMemberType]
        {"count": 0}, {"configurable": {"thread_id": "nstep-chain"}}
    )
    await cast(_StubSaver, saver)._ava_nstep_flush("nstep-chain")

    checkpoints = saver.storage["nstep-chain"][""]
    checkpoint_ids = set(checkpoints)
    parent_ids = {
        parent_id for _checkpoint, _metadata, parent_id in checkpoints.values() if parent_id
    }
    write_ids = {
        checkpoint_id
        for thread_id, _namespace, checkpoint_id in saver.writes
        if thread_id == "nstep-chain"
    }

    assert parent_ids <= checkpoint_ids
    assert write_ids <= checkpoint_ids


async def test_interval_one_is_pure_passthrough() -> None:
    saver = _StubSaver()
    _wrap(saver, interval=1)

    first_result = await _aput(saver, 1)
    await saver.aput_writes(
        {"configurable": {"thread_id": "default"}, "config": 1},
        [("channel", "first")],
        "task-1",
    )
    second_result = await _aput(saver, 2)
    await saver.aput_writes(
        {"configurable": {"thread_id": "default"}, "config": 2},
        [(PUSH, "second")],
        "task-2",
    )

    assert first_result == {"stored_step": 1}
    assert second_result == {"stored_step": 2}
    assert [call[2]["step"] for call in saver.aput_calls] == [1, 2]
    assert [call[1] for call in saver.aput_writes_calls] == [
        [("channel", "first")],
        [(PUSH, "second")],
    ]
    assert not hasattr(saver, "_ava_nstep_flush")


async def test_input_and_fork_checkpoints_are_never_throttled() -> None:
    saver = _StubSaver()
    _wrap(saver, interval=4)

    await _aput(saver, 1, source="input")
    await _aput(saver, 3, source="fork")

    assert [call[2]["source"] for call in saver.aput_calls] == ["input", "fork"]


async def test_input_after_a_skipped_superstep_uses_the_last_persisted_parent() -> None:
    saver = _StubSaver()
    _wrap(saver, interval=4)

    await _aput(saver, 0, source="loop")
    await _aput(saver, 1, source="loop")
    await saver.aput(
        {"configurable": {"thread_id": "default"}, "skipped_parent": 1},
        {"checkpoint_id": "input", "channel_versions": {}},
        {"source": "input", "step": 2},
        {"channel": 2},
    )

    assert saver.aput_calls[-1][0] == {"stored_step": 0}


async def test_final_flush_persists_only_the_last_skipped_checkpoint_once() -> None:
    saver = _StubSaver()
    _wrap(saver, interval=4)

    await _aput(saver, 1)
    await _aput(saver, 2)
    await _aput(saver, 3)
    await saver._ava_nstep_flush("default")
    await saver._ava_nstep_flush("default")

    assert [call[2]["step"] for call in saver.aput_calls] == [3]


async def test_written_checkpoint_clears_skipped_tail_and_skipped_aput_returns_input_config() -> (
    None
):
    saver = _StubSaver()
    _wrap(saver, interval=4)

    input_config: dict[str, object] = {
        "configurable": {"thread_id": "default"},
        "input_step": 1,
    }
    skipped_result = await saver.aput(
        input_config,
        {"checkpoint_id": "1", "channel_versions": {"messages": "v1"}},
        {"source": "update", "step": 1},
        {"channel": 1},
    )
    await _aput(saver, 4)
    await saver._ava_nstep_flush("default")

    assert skipped_result is input_config
    assert [call[2]["step"] for call in saver.aput_calls] == [4]


async def test_interval_keeps_skipped_tails_isolated_by_thread() -> None:
    """A shared hosted saver must never flush one agent's tail for another."""
    saver = _StubSaver()
    _wrap(saver, interval=4)

    await _aput(saver, 1, thread_id="agent-a")
    await _aput(saver, 1, thread_id="agent-b")
    await saver._ava_nstep_flush("agent-a")

    assert _stored_thread_ids(saver) == ["agent-a"]

    await saver._ava_nstep_flush("agent-b")

    assert _stored_thread_ids(saver) == ["agent-a", "agent-b"]


async def test_callable_interval_uses_the_current_turn_config() -> None:
    """A shared saver resolves each hosted agent's own interval."""
    current_interval = [4]
    saver = _StubSaver()
    _wrap(saver, lambda: current_interval[0])

    await _aput(saver, 1, thread_id="agent-a")
    current_interval[0] = 1
    await _aput(saver, 1, thread_id="agent-b")

    assert _stored_thread_ids(saver) == ["agent-b"]

    await saver._ava_nstep_flush("agent-a")

    assert _stored_thread_ids(saver) == ["agent-b", "agent-a"]


def test_checkpoint_interval_config_is_per_agent_and_defaults_to_four() -> None:
    field = AgentRuntimeSettings.model_fields["checkpoint_interval"]
    extra = field.json_schema_extra

    assert field.alias == "AVA_CHECKPOINT_INTERVAL"
    assert field.default == 4
    assert isinstance(extra, dict)
    assert extra["per_agent"] is True
    assert (
        AgentRuntimeSettings.model_validate({"AVA_CHECKPOINT_INTERVAL": 4}).checkpoint_interval == 4
    )


async def test_retained_checkpoint_persists_blobs_for_current_channel_versions() -> None:
    """A retained aput must request blobs for EVERY current channel version.

    Versions born on skipped super-steps have no blob row of their own; the
    retained checkpoint still references them, so the wrapper merges the full
    channel_versions map into new_versions — otherwise the saver writes no
    blob for those channels and readers (timeline cold load, recovery) see
    the messages channel missing.
    """
    saver = _StubSaver()
    _wrap(saver, interval=4)

    await _aput(saver, 1, source="loop")
    await _aput(saver, 2, source="loop")
    await _aput(saver, 3, source="loop")
    await _aput(saver, 4, source="loop")

    assert [call[2]["step"] for call in saver.aput_calls] == [4]
    retained_versions = saver.aput_calls[-1][3]
    assert "messages" in retained_versions
    assert retained_versions["messages"] == "v4"


async def test_final_flush_persists_blobs_for_current_channel_versions() -> None:
    """The turn-end flush requests blobs for every current channel version."""
    saver = _StubSaver()
    _wrap(saver, interval=4)

    await _aput(saver, 1, source="loop")
    await saver._ava_nstep_flush("default")

    assert [call[2]["step"] for call in saver.aput_calls] == [1]
    flush_versions = saver.aput_calls[-1][3]
    assert "messages" in flush_versions
    assert flush_versions["messages"] == "v1"


async def test_delta_thread_retires_the_throttle_after_first_evidence() -> None:
    """Once a checkpoint reveals a DeltaChannel, nothing is skipped or re-homed."""
    saver = _StubSaver()
    _wrap(saver, interval=4)

    assert await _aput_delta(saver, 0, counters={"messages": (0, 1)}) == {"stored_step": 0}
    for step in (1, 2, 3, 4):
        assert await _aput_delta(saver, step, counters={"messages": (step, step)}) == {
            "stored_step": step
        }

    # Every super-step reached the saver, including the ones the throttle would skip.
    assert [call[2]["step"] for call in saver.aput_calls] == [0, 1, 2, 3, 4]
    # Pass-through: the loop's own config and the unmerged new_versions are intact.
    for call in saver.aput_calls:
        step = call[2]["step"]
        assert call[0] == {"configurable": {"thread_id": "default"}, "input_step": step}
        assert call[3] == {"channel": step}

    # Writes keep their original config too: the retained batch, never remounted.
    write_config: dict[str, object] = {"configurable": {"thread_id": "default"}, "input_step": 5}
    writes: list[tuple[str, object]] = [("messages", ["m5"])]
    await saver.aput_writes(write_config, writes, "task-5")
    assert saver.aput_writes_calls == [(write_config, writes, "task-5", "")]


async def test_delta_detection_is_sticky_without_further_evidence() -> None:
    """A control-only checkpoint on a delta thread must not re-enable the throttle."""
    saver = _StubSaver()
    _wrap(saver, interval=4)

    await _aput_delta(saver, 1, counters={"messages": (1, 1)})
    # No counters, no marker — but the thread was already judged delta.
    assert await _aput_delta(saver, 2) == {"stored_step": 2}
    assert await _aput_delta(saver, 3) == {"stored_step": 3}

    assert [call[2]["step"] for call in saver.aput_calls] == [1, 2, 3]
    write_config: dict[str, object] = {"configurable": {"thread_id": "default"}, "input_step": 4}
    writes: list[tuple[str, object]] = [("messages", ["m4"])]
    await saver.aput_writes(write_config, writes, "task-4")
    assert saver.aput_writes_calls == [(write_config, writes, "task-4", "")]


async def test_delta_snapshot_marker_alone_retires_the_throttle() -> None:
    """The snapshot super-step resets the counters; its marker still exempts the thread."""
    saver = _StubSaver()
    _wrap(saver, interval=4)

    assert await _aput_delta(saver, 2, snapshot=True) == {"stored_step": 2}
    assert await _aput_delta(saver, 3) == {"stored_step": 3}

    assert [call[2]["step"] for call in saver.aput_calls] == [2, 3]


async def test_vanilla_full_snapshot_checkpoints_do_not_retire_the_throttle() -> None:
    """Plain channel values and absent counters must not be read as delta evidence."""
    saver = _StubSaver()
    _wrap(saver, interval=4)

    # A full-snapshot messages value (the vanilla model) is not a delta marker.
    await saver.aput(
        {"configurable": {"thread_id": "default"}, "input_step": 0},
        {
            "checkpoint_id": "0",
            "channel_versions": {"messages": "v0"},
            "channel_values": {"messages": ["m0"]},
        },
        {"source": "loop", "step": 0},
        {"channel": 0},
    )
    for step in (1, 2, 3, 4):
        await _aput(saver, step, source="loop")

    assert [call[2]["step"] for call in saver.aput_calls] == [0, 4]

    # And its writes keep the vanilla skip/re-home behavior.
    dropped: dict[str, object] = {"configurable": {"thread_id": "default"}, "input_step": 5}
    dropped_writes: list[tuple[str, object]] = [("messages", ["m5"])]
    await saver.aput_writes(dropped, dropped_writes, "task-dropped")
    assert saver.aput_writes_calls == []
    kept: dict[str, object] = {"configurable": {"thread_id": "default"}, "input_step": 6}
    kept_writes: list[tuple[str, object]] = [(PUSH, "pushed")]
    await saver.aput_writes(kept, kept_writes, "task-kept")
    assert saver.aput_writes_calls == [({"stored_step": 4}, kept_writes, "task-kept", "")]


async def test_delta_thread_flush_has_no_throttled_tail() -> None:
    """Every delta super-step is already durable, so the flush adds nothing."""
    saver = _StubSaver()
    _wrap(saver, interval=4)

    await _aput_delta(saver, 1, counters={"messages": (1, 1)})
    await _aput_delta(saver, 2)
    await saver._ava_nstep_flush("default")

    assert [call[2]["step"] for call in saver.aput_calls] == [1, 2]


class _BlockedSaver(_StubSaver):
    """Hold one actual save until the test releases it, before recording success."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def aput(
        self,
        config: dict[str, object],
        checkpoint: dict[str, object],
        metadata: dict[str, object],
        new_versions: dict[str, object],
    ) -> dict[str, object]:
        if metadata["step"] == 1:
            self.entered.set()
            await self.release.wait()
        return await super().aput(config, checkpoint, metadata, new_versions)


async def test_concurrent_flush_cannot_ack_before_the_inflight_save() -> None:
    saver = _BlockedSaver()
    _wrap(saver, interval=4)
    await _aput(saver, 1)
    first = asyncio.ensure_future(saver._ava_nstep_flush("default"))
    await asyncio.wait_for(saver.entered.wait(), timeout=2)
    started = asyncio.Event()

    async def second_flush() -> None:
        started.set()
        await saver._ava_nstep_flush("default")

    second = asyncio.create_task(second_flush())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        # The second task ran until its first suspension, so this is a real
        # in-flight-save boundary, not a timing assumption about PostgreSQL.
        assert not second.done(), "flush reported success before any save completed"
        assert saver.aput_calls == []
    finally:
        saver.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
    assert [call[2]["step"] for call in saver.aput_calls] == [1]


async def test_cancelled_flush_retains_the_tail_for_retry() -> None:
    saver = _BlockedSaver()
    _wrap(saver, interval=4)
    await _aput(saver, 1)
    pending = asyncio.ensure_future(saver._ava_nstep_flush("default"))
    await asyncio.wait_for(saver.entered.wait(), timeout=2)
    pending.cancel()
    results = await asyncio.gather(pending, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert saver.aput_calls == []

    saver.release.set()
    await saver._ava_nstep_flush("default")
    await saver._ava_nstep_flush("default")
    assert [call[2]["step"] for call in saver.aput_calls] == [1]


async def test_flush_does_not_discard_a_newer_pending_checkpoint() -> None:
    saver = _BlockedSaver()
    _wrap(saver, interval=4)
    await _aput(saver, 1)
    pending = asyncio.ensure_future(saver._ava_nstep_flush("default"))
    await asyncio.wait_for(saver.entered.wait(), timeout=2)
    newer = asyncio.create_task(_aput(saver, 2))
    try:
        await asyncio.sleep(0)
    finally:
        saver.release.set()
        await asyncio.wait_for(asyncio.gather(pending, newer), timeout=2)
    await saver._ava_nstep_flush("default")
    assert [call[2]["step"] for call in saver.aput_calls] == [1, 2]


async def test_one_threads_flush_does_not_block_another_threads_save() -> None:
    saver = _BlockedSaver()
    _wrap(saver, interval=4)
    await _aput(saver, 1, thread_id="agent-a")
    pending = asyncio.ensure_future(saver._ava_nstep_flush("agent-a"))
    await asyncio.wait_for(saver.entered.wait(), timeout=2)
    try:
        await asyncio.wait_for(_aput(saver, 0, thread_id="agent-b"), timeout=2)
        assert _stored_thread_ids(saver) == ["agent-b"]
    finally:
        saver.release.set()
        await asyncio.wait_for(pending, timeout=2)
    assert _stored_thread_ids(saver) == ["agent-b", "agent-a"]


async def test_nstep_wrapper_delta_readback_matches_unwrapped_control(
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    """B1 regression: a wrapped delta run must read back exactly like the control.

    Before the throttle retirement, the wrapper dropped and re-homed delta
    writes: on this exact 12-superstep graph it read back 8 of 24 messages,
    scrambled, with no error. Now the wrapped run and the unwrapped control
    must persist the same delta log and reconstruct identical message ids.
    """

    def build(saver: AsyncPostgresSaver) -> Any:
        def step(state: _DeltaState) -> dict[str, Any]:
            n = state["n"]
            return {
                "messages": [
                    HumanMessage(content=f"u{n}", id=f"u{n}"),
                    AIMessage(content=f"a{n}", id=f"a{n}"),
                ],
                "n": n + 1,
            }

        def route(state: _DeltaState) -> str:
            return "step" if state["n"] < state["target"] else END

        graph = StateGraph(_DeltaState)
        graph.add_node("step", step)  # pyright: ignore[reportUnknownMemberType]
        graph.add_edge(START, "step")  # pyright: ignore[reportUnknownMemberType]
        graph.add_conditional_edges("step", route)  # pyright: ignore[reportUnknownMemberType]
        return graph.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]

    control_config: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
    wrapped_config: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}

    control = build(AsyncPostgresSaver(aops_pool))
    await control.ainvoke(  # pyright: ignore[reportUnknownMemberType]
        {"messages": [], "n": 0, "target": 12}, control_config, recursion_limit=200
    )

    wrapped_saver = AsyncPostgresSaver(aops_pool)
    _wrap_saver_writes_with_nstep_interval(wrapped_saver, 4)
    wrapped = build(wrapped_saver)
    await wrapped.ainvoke(  # pyright: ignore[reportUnknownMemberType]
        {"messages": [], "n": 0, "target": 12}, wrapped_config, recursion_limit=200
    )
    await wrapped_saver._ava_nstep_flush(  # type: ignore[attr-defined]
        str(wrapped_config["configurable"]["thread_id"])
    )

    async def read_message_ids(config: RunnableConfig) -> list[str | None]:
        reader = build(AsyncPostgresSaver(aops_pool))
        state = await reader.aget_state(config)  # pyright: ignore[reportUnknownMemberType]
        return [getattr(message, "id", None) for message in state.values["messages"]]

    expected = [f"{part}{n}" for n in range(12) for part in ("u", "a")]
    assert await read_message_ids(control_config) == expected
    assert await read_message_ids(wrapped_config) == expected

    # No super-step may be skipped on a delta thread: the wrapped run persists
    # the same checkpoint and write rows as upstream would.
    counts: dict[str, tuple[int, int]] = {}
    async with aops_pool.connection() as conn:
        for label, config in (("control", control_config), ("wrapped", wrapped_config)):
            thread_id = str(config["configurable"]["thread_id"])
            cursor = await conn.execute(
                "SELECT (SELECT count(*) FROM checkpoints WHERE thread_id = %s),"
                " (SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s)",
                (thread_id, thread_id),
            )
            row = await cursor.fetchone()
            assert row is not None
            counts[label] = (int(row[0]), int(row[1]))
    assert counts["wrapped"] == counts["control"]
